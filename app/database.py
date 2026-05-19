import re
import zlib
import sqlite3
import threading
from app.config import DB_PATH

_write_lock = threading.Lock()


def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _compress(text: str) -> bytes:
    return zlib.compress(text.encode("utf-8"), level=6)


def _decompress(blob) -> str:
    if not blob:
        return ""
    try:
        return zlib.decompress(bytes(blob)).decode("utf-8")
    except Exception:
        return ""


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
                indexed_at  REAL    NOT NULL,
                content     BLOB
            );
        """)

        # Migrations for older schemas
        for ddl in (
            "ALTER TABLE files ADD COLUMN folder_name TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE files ADD COLUMN content BLOB",
        ):
            try:
                cur.execute(ddl)
                conn.commit()
            except Exception:
                pass

        # Recreate fts_index as contentless if needed — stores only inverted index,
        # no text duplication. Snippet is generated from files.content (compressed).
        cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='fts_index'")
        row = cur.fetchone()
        fts_sql = row[0] if row else ""
        need_recreate = not fts_sql or "content=''" not in fts_sql

        if need_recreate:
            cur.execute("DROP TABLE IF EXISTS fts_index")
            cur.execute("""
                CREATE VIRTUAL TABLE fts_index USING fts5(
                    content,
                    content='',
                    tokenize='unicode61'
                )
            """)
            # Rebuild from existing files (content may be text or compressed blob)
            cur.execute("SELECT id, content FROM files")
            for r in cur.fetchall():
                text = _decompress(r["content"]) if r["content"] else ""
                if text:
                    cur.execute(
                        "INSERT INTO fts_index(rowid, content) VALUES (?, ?)",
                        (r["id"], text),
                    )
            conn.commit()

        conn.commit()
        conn.close()


# ── FTS5 query sanitizer ──────────────────────────────────────────────────────

def _fts_query(raw: str) -> str:
    stripped = raw.strip()
    if not stripped:
        return '""'
    if stripped.startswith('"') and stripped.endswith('"') and len(stripped) > 2:
        inner = stripped[1:-1].replace('"', '""')
        return f'"{inner}"'
    tokens = stripped.split()
    return " ".join(f'"{t.replace(chr(34), chr(34)*2)}"' for t in tokens)


# ── Snippet ───────────────────────────────────────────────────────────────────

def _make_snippet(text: str, query: str, radius: int = 120) -> str:
    """Find the first query token in text and return a marked-up excerpt."""
    if not text:
        return ""
    # Strip surrounding quotes for phrase search
    q = query.strip().strip('"')
    tokens = q.split()
    if not tokens:
        return ""

    lower = text.lower()
    pos = -1
    for token in tokens:
        p = lower.find(token.lower())
        if p != -1:
            pos = p
            break

    if pos == -1:
        excerpt = text[:radius * 2]
        prefix = ""
    else:
        start = max(0, pos - radius)
        end = min(len(text), pos + radius)
        excerpt = text[start:end]
        prefix = "…" if start > 0 else ""

    # Highlight all tokens
    def highlight(s: str) -> str:
        for t in tokens:
            s = re.sub(
                f"({re.escape(t)})",
                r"<mark>\1</mark>",
                s,
                flags=re.IGNORECASE,
            )
        return s

    suffix = "…" if (pos + radius) < len(text) else ""
    return prefix + highlight(excerpt) + suffix


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
    compressed = _compress(content)
    with _write_lock:
        conn = get_conn()
        cur = conn.cursor()
        try:
            cur.execute("SELECT id FROM files WHERE path = ?", (path,))
            row = cur.fetchone()
            if row:
                file_id = row["id"]
                cur.execute(
                    "UPDATE files SET name=?, folder_name=?, size=?, modified_at=?, "
                    "indexed_at=?, content=? WHERE id=?",
                    (name, folder_name, size, modified_at, indexed_at, compressed, file_id),
                )
                # Contentless FTS delete doesn't need old content
                cur.execute(
                    "INSERT INTO fts_index(fts_index, rowid, content) VALUES ('delete', ?, ?)",
                    (file_id, content),
                )
            else:
                cur.execute(
                    "INSERT INTO files (path, name, folder_name, size, modified_at, indexed_at, content) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (path, name, folder_name, size, modified_at, indexed_at, compressed),
                )
                file_id = cur.lastrowid
            cur.execute(
                "INSERT INTO fts_index(rowid, content) VALUES (?, ?)",
                (file_id, content),
            )
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
        cur.execute("SELECT id, content FROM files WHERE path = ?", (path,))
        row = cur.fetchone()
        if row:
            text = _decompress(row["content"])
            cur.execute(
                "INSERT INTO fts_index(fts_index, rowid, content) VALUES ('delete', ?, ?)",
                (row["id"], text),
            )
            cur.execute("DELETE FROM files WHERE id = ?", (row["id"],))
            conn.commit()
        conn.close()


def delete_files_by_folder(folder_name: str):
    with _write_lock:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT id, content FROM files WHERE folder_name = ?", (folder_name,))
        rows = cur.fetchall()
        for r in rows:
            text = _decompress(r["content"])
            cur.execute(
                "INSERT INTO fts_index(fts_index, rowid, content) VALUES ('delete', ?, ?)",
                (r["id"], text),
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
            SELECT f.path, f.name, f.folder_name, f.modified_at, f.content
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
            SELECT f.path, f.name, f.folder_name, f.modified_at, f.content
            FROM fts_index
            JOIN files f ON fts_index.rowid = f.id
            WHERE fts_index MATCH ?
            ORDER BY rank
            LIMIT ?
        """
        params = [fts_q, limit]
    cur.execute(sql, params)
    results = []
    for r in cur.fetchall():
        text = _decompress(r["content"])
        results.append({
            "path": r["path"],
            "name": r["name"],
            "folder_name": r["folder_name"],
            "modified_at": r["modified_at"],
            "snippet": _make_snippet(text, query),
        })
    conn.close()
    return results


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
