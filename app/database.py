import sqlite3
from app.config import DB_PATH


def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.executescript("""
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
            content=files,
            content_rowid=id,
            tokenize='unicode61'
        );
    """)
    # Add folder_name column to existing DBs that don't have it
    try:
        cur.execute("ALTER TABLE files ADD COLUMN folder_name TEXT NOT NULL DEFAULT ''")
        conn.commit()
    except Exception:
        pass
    conn.commit()
    conn.close()


def upsert_file(path: str, name: str, folder_name: str, size: int, modified_at: float, content: str, indexed_at: float):
    conn = get_conn()
    cur = conn.cursor()
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
            "INSERT INTO files (path, name, folder_name, size, modified_at, indexed_at) VALUES (?,?,?,?,?,?)",
            (path, name, folder_name, size, modified_at, indexed_at),
        )
        file_id = cur.lastrowid
        cur.execute("INSERT INTO fts_index(rowid, content) VALUES (?, ?)", (file_id, content))
    conn.commit()
    conn.close()


def delete_file(path: str):
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


def search(query: str, folder_names: list[str] | None = None, limit: int = 50):
    conn = get_conn()
    cur = conn.cursor()

    if folder_names:
        placeholders = ",".join("?" * len(folder_names))
        sql = f"""
            SELECT
                f.path,
                f.name,
                f.folder_name,
                f.modified_at,
                snippet(fts_index, 0, '<mark>', '</mark>', '…', 32) AS snippet
            FROM fts_index
            JOIN files f ON fts_index.rowid = f.id
            WHERE fts_index MATCH ?
              AND f.folder_name IN ({placeholders})
            ORDER BY rank
            LIMIT ?
        """
        params = [query] + folder_names + [limit]
    else:
        sql = """
            SELECT
                f.path,
                f.name,
                f.folder_name,
                f.modified_at,
                snippet(fts_index, 0, '<mark>', '</mark>', '…', 32) AS snippet
            FROM fts_index
            JOIN files f ON fts_index.rowid = f.id
            WHERE fts_index MATCH ?
            ORDER BY rank
            LIMIT ?
        """
        params = [query, limit]

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
        FROM files
        GROUP BY folder_name
        ORDER BY folder_name
    """)
    by_folder = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT path, name, folder_name, indexed_at FROM files ORDER BY indexed_at DESC LIMIT 20")
    recent = [dict(r) for r in cur.fetchall()]
    conn.close()
    return count, by_folder, recent
