import re
import sqlite3
import threading
from app.config import DB_PATH

# Single write lock — SQLite supports only one writer at a time.
# All functions that modify the DB acquire this lock before opening a connection.
_write_lock = threading.Lock()


def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # readers don't block writers
    conn.execute("PRAGMA synchronous=NORMAL") # safe + faster than FULL
    return conn


def init_db():
    with _write_lock:
        conn = get_conn()
        cur = conn.cursor()
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS folders (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT    UNIQUE NOT NULL,
                path       TEXT    NOT NULL,
                created_at REAL    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS files (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                path        TEXT    UNIQUE NOT NULL,
                name        TEXT    NOT NULL,
                folder_name TEXT    NOT NULL DEFAULT '',
                size        INTEGER NOT NULL,
                modified_at REAL    NOT NULL,
                indexed_at  REAL    NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS fts_index USING fts5(
                content,
                tokenize='unicode61'
            );
        """)
        try:
            cur.execute("ALTER TABLE files ADD COLUMN folder_name TEXT NOT NULL DEFAULT ''")
            conn.commit()
        except Exception:
            pass

        # Migrate: if fts_index was created with content=files it breaks snippet().
        # Detect by checking sqlite_master for the old definition and recreate.
        cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='fts_index'")
        row = cur.fetchone()
        if row and "content=files" in (row[0] or ""):
            cur.execute("DROP TABLE IF EXISTS fts_index")
            cur.execute("""
                CREATE VIRTUAL TABLE fts_index USING fts5(
                    content,
                    tokenize='unicode61'
                )
            """)
            conn.commit()

        conn.commit()
        conn.close()


# ── FTS5 query sanitizer ──────────────────────────────────────────────────────

def _fts_query(raw: str) -> str:
    """Convert a plain user query into a safe FTS5 MATCH expression.

    Each whitespace-separated token is wrapped in double quotes so FTS5
    treats it as a literal phrase rather than a syntax expression.
    This prevents SQL logic errors from special chars like - * ( ) " ^
    """
    tokens = raw.split()
    if not tokens:
        return '""'
    # Escape any double-quotes inside a token by doubling them
    safe = " ".join(f'"{t.replace(chr(34), chr(34)+chr(34))}"' for t in tokens)
    return safe


# ── Folders CRUD ─────────────────────────────────────────────────────────────

def get_folders() -> list[dict]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, name, path, created_at FROM folders ORDER BY created_at")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def add_folder(name: str, path: str) -> dict:
    import time
    with _write_lock:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO folders (name, path, created_at) VALUES (?, ?, ?)",
            (name, path, time.time()),
        )
        conn.commit()
        row_id = cur.lastrowid
        conn.close()
    return {"id": row_id, "name": name, "path": path}


def delete_folder(folder_id: int) -> str | None:
    with _write_lock:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT name FROM folders WHERE id = ?", (folder_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            return None
        name = row["name"]
        cur.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
        conn.commit()
        conn.close()
    return name


# ── Files ────────────────────────────────────────────────────────────────────

def upsert_file(path: str, name: str, folder_name: str, size: int,
                modified_at: float, content: str, indexed_at: float):
    with _write_lock:
        conn = get_conn()
        cur = conn.cursor()
        try:
            cur.execute("SELECT id FROM files WHERE path = ?", (path,))
            row = cur.fetchone()
            if row:
                file_id = row["id"]
                cur.execute(
                    "UPDATE files SET name=?, folder_name=?, size=?, modified_at=?, indexed_at=? WHERE id=?",
                    (name, folder_name, size, modified_at, indexed_at, file_id),
                )
                cur.execute(
                    "INSERT INTO fts_index(fts_index, rowid, content) VALUES ('delete', ?, ?)",
                    (file_id, content),
                )
                cur.execute("INSERT INTO fts_index(rowid, content) VALUES (?, ?)", (file_id, content))
            else:
                cur.execute(
                    "INSERT INTO files (path, name, folder_name, size, modified_at, indexed_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (path, name, folder_name, size, modified_at, indexed_at),
                )
                file_id = cur.lastrowid
                cur.execute("INSERT INTO fts_index(rowid, content) VALUES (?, ?)", (file_id, content))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def delete_file(path: str):
    with _write_lock:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT id FROM files WHERE path = ?", (path,))
        row = cur.fetchone()
        if row:
            file_id = row["id"]
            cur.execute(
                "INSERT INTO fts_index(fts_index, rowid, content) VALUES ('delete', ?, '')",
                (file_id,),
            )
            cur.execute("DELETE FROM files WHERE id = ?", (file_id,))
            conn.commit()
        conn.close()


def delete_files_by_folder(folder_name: str):
    with _write_lock:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT id FROM files WHERE folder_name = ?", (folder_name,))
        ids = [r["id"] for r in cur.fetchall()]
        for fid in ids:
            cur.execute(
                "INSERT INTO fts_index(fts_index, rowid, content) VALUES ('delete', ?, '')",
                (fid,),
            )
        cur.execute("DELETE FROM files WHERE folder_name = ?", (folder_name,))
        conn.commit()
        conn.close()


def search(query: str, folder_names: list[str] | None = None, limit: int = 50):
    fts_q = _fts_query(query)
    conn = get_conn()
    cur = conn.cursor()
    if folder_names:
        placeholders = ",".join("?" * len(folder_names))
        sql = f"""
            SELECT f.path, f.name, f.folder_name, f.modified_at,
                   snippet(fts_index, 0, '<mark>', '</mark>', '…', 32) AS snippet
            FROM fts_index
            JOIN files f ON fts_index.rowid = f.id
            WHERE fts_index MATCH ?
              AND f.folder_name IN ({placeholders})
            ORDER BY rank
            LIMIT ?
        """
        params = [fts_q] + folder_names + [limit]
    else:
        sql = """
            SELECT f.path, f.name, f.folder_name, f.modified_at,
                   snippet(fts_index, 0, '<mark>', '</mark>', '…', 32) AS snippet
            FROM fts_index
            JOIN files f ON fts_index.rowid = f.id
            WHERE fts_index MATCH ?
            ORDER BY rank
            LIMIT ?
        """
        params = [fts_q, limit]
    cur.execute(sql, params)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def stats():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as cnt FROM files")
    count = cur.fetchone()["cnt"]
    cur.execute("""
        SELECT folder_name, COUNT(*) as cnt
        FROM files GROUP BY folder_name ORDER BY folder_name
    """)
    by_folder = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT path, name, folder_name, indexed_at FROM files ORDER BY indexed_at DESC LIMIT 20")
    recent = [dict(r) for r in cur.fetchall()]
    conn.close()
    return count, by_folder, recent
