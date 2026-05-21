"""
Database layer.

Main DB (nbmsearch.db):
  - folders  — list of indexed folders
  - sessions — admin auth tokens

Per-folder DB (data/folder_{id}.db):
  - files     — file metadata + compressed text
  - fts_index — contentless FTS5 (inverted index only, no text duplication)
"""
import re
import zlib
import sqlite3
import threading
import secrets
import time
from pathlib import Path

from app.settings import DB_PATH, DATA_DIR

# One lock per folder DB — parallel indexing of different folders works fully concurrently
_main_lock = threading.Lock()
_folder_locks: dict[int, threading.Lock] = {}
_folder_locks_mu = threading.Lock()
DATA_DIR.mkdir(parents=True, exist_ok=True)


def _folder_lock(folder_id: int) -> threading.Lock:
    with _folder_locks_mu:
        if folder_id not in _folder_locks:
            _folder_locks[folder_id] = threading.Lock()
        return _folder_locks[folder_id]


# ── Connections ───────────────────────────────────────────────────────────────

def _get_main_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _folder_db_path(folder_id: int) -> Path:
    return DATA_DIR / f"folder_{folder_id}.db"


def _get_folder_conn(folder_id: int):
    path = _folder_db_path(folder_id)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


# ── Compression ───────────────────────────────────────────────────────────────

def _compress(text: str) -> bytes:
    return zlib.compress(text.encode("utf-8"), level=6)


def _decompress(blob) -> str:
    if not blob:
        return ""
    try:
        return zlib.decompress(bytes(blob)).decode("utf-8")
    except Exception:
        return ""


# ── Init ─────────────────────────────────────────────────────────────────────

def init_db():
    with _main_lock:
        conn = _get_main_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS folders (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT    UNIQUE NOT NULL,
                path        TEXT    NOT NULL,
                created_at  REAL    NOT NULL
            );
            CREATE TABLE IF NOT EXISTS schedules (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                folder_id       INTEGER NOT NULL REFERENCES folders(id),
                reindex_minutes INTEGER NOT NULL DEFAULT 60,
                last_run_at     REAL,
                next_run_at     REAL,
                created_at      REAL    NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token      TEXT PRIMARY KEY,
                created_at REAL NOT NULL
            );
        """)
        # migrations — keep old columns harmless
        for ddl in (
            "ALTER TABLE folders ADD COLUMN reindex_minutes INTEGER NOT NULL DEFAULT 60",
            "ALTER TABLE folders ADD COLUMN last_reindex_at REAL",
            "ALTER TABLE folders ADD COLUMN watchdog_enabled INTEGER NOT NULL DEFAULT 0",
        ):
            try:
                conn.execute(ddl)
            except Exception:
                pass
        conn.commit()
        # Migrate all existing per-folder DBs (add created_at if missing)
        folder_ids = [r[0] for r in conn.execute("SELECT id FROM folders").fetchall()]
        conn.close()
    for fid in folder_ids:
        try:
            fconn = _get_folder_conn(fid)
            try:
                fconn.execute("ALTER TABLE files ADD COLUMN created_at REAL")
                fconn.commit()
            except Exception:
                pass
            fconn.close()
        except Exception:
            pass


def _init_folder_db(folder_id: int):
    conn = _get_folder_conn(folder_id)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS files (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            path        TEXT    UNIQUE NOT NULL,
            name        TEXT    NOT NULL,
            size        INTEGER NOT NULL DEFAULT 0,
            modified_at REAL    NOT NULL DEFAULT 0,
            indexed_at  REAL    NOT NULL DEFAULT 0,
            created_at  REAL,
            content     BLOB
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS fts_index USING fts5(
            content,
            content='',
            tokenize='unicode61'
        );
    """)
    # migration for existing DBs
    try:
        conn.execute("ALTER TABLE files ADD COLUMN created_at REAL")
        conn.commit()
    except Exception:
        pass
    conn.commit()
    conn.close()


# ── Query normalization ────────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    """Replace chars that FTS5 unicode61 tokenizer treats as separators with spaces."""
    return re.sub(r"[-/\\.,;:!?()\[\]{}|@#$%^&*+=<>«»]", " ", text)


def _fts_query(raw: str) -> str:
    stripped = raw.strip()
    if not stripped:
        return '""'

    # Exact phrase: "24-111/МО" → normalize inner → FTS5 phrase
    if stripped.startswith('"') and stripped.endswith('"') and len(stripped) > 2:
        inner = _normalize(stripped[1:-1]).strip()
        inner = inner.replace('"', '""')
        return f'"{inner}"'

    # Regular: normalize → split → wrap each token
    normalized = _normalize(stripped)
    tokens = [t for t in normalized.split() if t]
    if not tokens:
        return '""'
    return " ".join(f'"{t.replace(chr(34), chr(34)*2)}"' for t in tokens)


# ── Snippet ───────────────────────────────────────────────────────────────────

def _make_snippet(text: str, query: str, radius: int = 150) -> str:
    if not text:
        return ""
    stripped = query.strip()
    is_phrase = stripped.startswith('"') and stripped.endswith('"') and len(stripped) > 2
    q = stripped.strip('"')
    normalized_q = _normalize(q)
    tokens = [t for t in normalized_q.split() if t]
    if not tokens:
        return ""

    lower = text.lower()
    pos = -1

    if is_phrase:
        # Search for the whole phrase first
        phrase = " ".join(tokens)
        pos = lower.find(phrase.lower())

    if pos == -1:
        # Fall back: find first token
        for token in tokens:
            p = lower.find(token.lower())
            if p != -1:
                pos = p
                break

    if pos == -1:
        start, end, prefix = 0, min(len(text), radius * 2), ""
    else:
        start = max(0, pos - radius)
        end = min(len(text), pos + radius)
        prefix = "…" if start > 0 else ""

    excerpt = text[start:end]
    suffix = "…" if end < len(text) else ""

    def highlight(s: str) -> str:
        # Sort longest first; use word boundaries to avoid partial matches
        pattern = "|".join(
            r"(?<![а-яёa-z\d])" + re.escape(t) + r"(?![а-яёa-z\d])"
            for t in sorted(tokens, key=len, reverse=True)
        )
        return re.sub(pattern, lambda m: f"<mark>{m.group()}</mark>", s, flags=re.IGNORECASE)

    return prefix + highlight(excerpt) + suffix


# ── Folders CRUD ─────────────────────────────────────────────────────────────

def set_folder_watchdog(folder_id: int, enabled: bool):
    with _main_lock:
        conn = _get_main_conn()
        conn.execute("UPDATE folders SET watchdog_enabled=? WHERE id=?", (1 if enabled else 0, folder_id))
        conn.commit()
        conn.close()


def get_folders() -> list[dict]:
    conn = _get_main_conn()
    rows = [dict(r) for r in conn.execute(
        "SELECT id, name, path, reindex_minutes, created_at, last_reindex_at, watchdog_enabled FROM folders ORDER BY created_at"
    ).fetchall()]
    conn.close()
    return rows


def rename_folder(folder_id: int, new_name: str):
    with _main_lock:
        conn = _get_main_conn()
        conn.execute("UPDATE folders SET name=? WHERE id=?", (new_name.strip(), folder_id))
        conn.commit()
        conn.close()


def set_folder_last_reindex(folder_id: int, ts: float):
    with _main_lock:
        conn = _get_main_conn()
        conn.execute("UPDATE folders SET last_reindex_at=? WHERE id=?", (ts, folder_id))
        rows = conn.execute("SELECT id, reindex_minutes FROM schedules WHERE folder_id=?", (folder_id,)).fetchall()
        for r in rows:
            nxt = ts + r["reindex_minutes"] * 60
            conn.execute("UPDATE schedules SET last_run_at=?, next_run_at=? WHERE id=?", (ts, nxt, r["id"]))
        conn.commit()
        conn.close()


def add_folder(name: str, path: str) -> dict:
    with _main_lock:
        conn = _get_main_conn()
        cur = conn.execute(
            "INSERT INTO folders (name, path, created_at) VALUES (?,?,?)",
            (name, path, time.time()),
        )
        folder_id = cur.lastrowid
        conn.commit()
        conn.close()
    _init_folder_db(folder_id)
    return {"id": folder_id, "name": name, "path": path}


# ── Schedules ─────────────────────────────────────────────────────────────────

def get_schedules() -> list[dict]:
    conn = _get_main_conn()
    rows = [dict(r) for r in conn.execute("""
        SELECT s.id, s.folder_id, s.reindex_minutes, s.last_run_at, s.next_run_at, s.created_at,
               f.name AS folder_name
        FROM schedules s
        JOIN folders f ON s.folder_id = f.id
        ORDER BY s.created_at
    """).fetchall()]
    conn.close()
    return rows


def add_schedule(folder_id: int, reindex_minutes: int) -> dict:
    nxt = time.time() + reindex_minutes * 60
    with _main_lock:
        conn = _get_main_conn()
        cur = conn.execute(
            "INSERT INTO schedules (folder_id, reindex_minutes, next_run_at, created_at) VALUES (?,?,?,?)",
            (folder_id, reindex_minutes, nxt, time.time()),
        )
        sid = cur.lastrowid
        conn.commit()
        conn.close()
    return {"id": sid, "folder_id": folder_id, "reindex_minutes": reindex_minutes, "next_run_at": nxt}


def delete_schedule(schedule_id: int):
    with _main_lock:
        conn = _get_main_conn()
        conn.execute("DELETE FROM schedules WHERE id=?", (schedule_id,))
        conn.commit()
        conn.close()


def update_schedule_run(schedule_id: int, reindex_minutes: int):
    nxt = time.time() + reindex_minutes * 60
    with _main_lock:
        conn = _get_main_conn()
        conn.execute(
            "UPDATE schedules SET last_run_at=?, next_run_at=? WHERE id=?",
            (time.time(), nxt, schedule_id),
        )
        conn.commit()
        conn.close()


def delete_folder(folder_id: int) -> str | None:
    with _main_lock:
        conn = _get_main_conn()
        row = conn.execute("SELECT name FROM folders WHERE id=?", (folder_id,)).fetchone()
        if not row:
            conn.close()
            return None
        name = row["name"]
        conn.execute("DELETE FROM folders WHERE id=?", (folder_id,))
        conn.commit()
        conn.close()
    # Delete the folder's index DB file
    # Also delete all schedules for this folder
    with _main_lock:
        conn = _get_main_conn()
        conn.execute("DELETE FROM schedules WHERE folder_id=?", (folder_id,))
        conn.commit()
        conn.close()
    db_file = _folder_db_path(folder_id)
    try:
        db_file.unlink(missing_ok=True)
    except Exception:
        pass
    return name


# ── Files (per-folder DB) ─────────────────────────────────────────────────────

def upsert_file(folder_id: int, path: str, name: str, size: int,
                modified_at: float, content: str, indexed_at: float):
    compressed = _compress(content)
    with _folder_lock(folder_id):
        conn = _get_folder_conn(folder_id)
        try:
            row = conn.execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()
            if row:
                fid = row["id"]
                old_blob = conn.execute("SELECT content FROM files WHERE id=?", (fid,)).fetchone()["content"]
                old_text = _decompress(old_blob)
                conn.execute(
                    "INSERT INTO fts_index(fts_index, rowid, content) VALUES ('delete',?,?)",
                    (fid, old_text),
                )
                conn.execute(
                    "UPDATE files SET name=?,size=?,modified_at=?,indexed_at=?,content=? WHERE id=?",
                    (name, size, modified_at, indexed_at, compressed, fid),
                )
            else:
                cur = conn.execute(
                    "INSERT INTO files (path,name,size,modified_at,indexed_at,created_at,content) VALUES (?,?,?,?,?,?,?)",
                    (path, name, size, modified_at, indexed_at, indexed_at, compressed),
                )
                fid = cur.lastrowid
            conn.execute("INSERT INTO fts_index(rowid, content) VALUES (?,?)", (fid, content))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def delete_file_from_folder(folder_id: int, path: str):
    with _folder_lock(folder_id):
        conn = _get_folder_conn(folder_id)
        row = conn.execute("SELECT id, content FROM files WHERE path=?", (path,)).fetchone()
        if row:
            conn.execute(
                "INSERT INTO fts_index(fts_index, rowid, content) VALUES ('delete',?,?)",
                (row["id"], _decompress(row["content"])),
            )
            conn.execute("DELETE FROM files WHERE id=?", (row["id"],))
            conn.commit()
        conn.close()


def get_file_count(folder_id: int) -> int:
    try:
        conn = _get_folder_conn(folder_id)
        n = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        conn.close()
        return n
    except Exception:
        return 0


def get_all_paths_in_folder(folder_id: int) -> set[str]:
    try:
        conn = _get_folder_conn(folder_id)
        rows = conn.execute("SELECT path FROM files").fetchall()
        conn.close()
        return {r[0] for r in rows}
    except Exception:
        return set()


# ── Folder stats ──────────────────────────────────────────────────────────────

_MONTH_NAMES = ["Янв","Фев","Мар","Апр","Май","Июн","Июл","Авг","Сен","Окт","Ноя","Дек"]

def get_folder_stats(folder_id: int, period: str = "day") -> dict:
    """period: 'day' (hourly, today) | 'month' (daily, current month) | 'year' (monthly, current year)"""
    import datetime as _dt
    db_path = _folder_db_path(folder_id)
    if not db_path.exists():
        return {"timeline": [], "extensions": []}
    conn = _get_folder_conn(folder_id)
    try:
        ts_col = "COALESCE(created_at, indexed_at)"
        if period == "day":
            # Hours 00-23 for today (local time)
            rows = conn.execute(
                f"""SELECT strftime('%H', {ts_col}, 'unixepoch', 'localtime') AS period,
                           COUNT(*) AS cnt
                    FROM files
                    WHERE date({ts_col}, 'unixepoch', 'localtime') = date('now', 'localtime')
                      AND {ts_col} IS NOT NULL
                    GROUP BY period ORDER BY period"""
            ).fetchall()
            counts = {r["period"]: r["cnt"] for r in rows}
            timeline = [{"period": f"{h:02d}:00", "cnt": counts.get(f"{h:02d}", 0)} for h in range(24)]
        elif period == "year":
            # Months 01-12 for current year (local time)
            rows = conn.execute(
                f"""SELECT strftime('%m', {ts_col}, 'unixepoch', 'localtime') AS period,
                           COUNT(*) AS cnt
                    FROM files
                    WHERE strftime('%Y', {ts_col}, 'unixepoch', 'localtime')
                          = strftime('%Y', 'now', 'localtime')
                      AND {ts_col} IS NOT NULL
                    GROUP BY period ORDER BY period"""
            ).fetchall()
            counts = {r["period"]: r["cnt"] for r in rows}
            timeline = [{"period": _MONTH_NAMES[m], "cnt": counts.get(f"{m+1:02d}", 0)}
                        for m in range(12)]
        else:
            # Days 01-NN for current month (local time)
            import calendar
            now = _dt.datetime.now()
            days_in_month = calendar.monthrange(now.year, now.month)[1]
            rows = conn.execute(
                f"""SELECT strftime('%d', {ts_col}, 'unixepoch', 'localtime') AS period,
                           COUNT(*) AS cnt
                    FROM files
                    WHERE strftime('%Y-%m', {ts_col}, 'unixepoch', 'localtime')
                          = strftime('%Y-%m', 'now', 'localtime')
                      AND {ts_col} IS NOT NULL
                    GROUP BY period ORDER BY period"""
            ).fetchall()
            counts = {r["period"]: r["cnt"] for r in rows}
            timeline = [{"period": f"{d:02d}", "cnt": counts.get(f"{d:02d}", 0)}
                        for d in range(1, days_in_month + 1)]
        rows = conn.execute("SELECT name FROM files").fetchall()
        ext_counts: dict[str, int] = {}
        for r in rows:
            ext = r["name"].rsplit(".", 1)[-1].lower() if "." in r["name"] else "—"
            ext_counts[ext] = ext_counts.get(ext, 0) + 1
        extensions = sorted([{"ext": k, "cnt": v} for k, v in ext_counts.items()],
                            key=lambda x: x["cnt"], reverse=True)
        return {"timeline": timeline, "extensions": extensions}
    finally:
        conn.close()


# ── Search ────────────────────────────────────────────────────────────────────

def search(query: str, folder_names: list[str] | None = None, limit: int = 50) -> list[dict]:
    fts_q = _fts_query(query)
    folders = get_folders()
    if folder_names:
        folders = [f for f in folders if f["name"] in folder_names]

    results = []
    for folder in folders:
        fid = folder["id"]
        db_path = _folder_db_path(fid)
        if not db_path.exists():
            continue
        try:
            conn = _get_folder_conn(fid)
            rows = conn.execute(
                """SELECT f.path, f.name, f.modified_at, f.content
                   FROM fts_index
                   JOIN files f ON fts_index.rowid = f.id
                   WHERE fts_index MATCH ?
                   ORDER BY rank LIMIT ?""",
                (fts_q, limit),
            ).fetchall()
            conn.close()
        except Exception:
            continue

        for r in rows:
            text = _decompress(r["content"])
            results.append({
                "path": r["path"],
                "name": r["name"],
                "folder_name": folder["name"],
                "modified_at": r["modified_at"],
                "snippet": _make_snippet(text, query),
            })

    # Sort: 1) folder name descending, 2) modified_at descending
    results.sort(key=lambda x: (x["folder_name"], x["modified_at"] or 0), reverse=True)
    return results[:limit]


# ── Stats ─────────────────────────────────────────────────────────────────────

def stats() -> tuple[int, list[dict]]:
    folders = get_folders()
    by_folder = []
    total = 0
    for f in folders:
        cnt = get_file_count(f["id"])
        total += cnt
        by_folder.append({"folder_name": f["name"], "folder_id": f["id"], "cnt": cnt})
    return total, by_folder


# ── Sessions ─────────────────────────────────────────────────────────────────

def create_session() -> str:
    token = secrets.token_hex(32)
    conn = _get_main_conn()
    conn.execute("INSERT INTO sessions (token, created_at) VALUES (?,?)", (token, time.time()))
    conn.commit()
    conn.close()
    return token


def session_valid(token: str | None) -> bool:
    if not token:
        return False
    conn = _get_main_conn()
    row = conn.execute("SELECT token FROM sessions WHERE token=?", (token,)).fetchone()
    conn.close()
    return row is not None


def delete_session(token: str):
    conn = _get_main_conn()
    conn.execute("DELETE FROM sessions WHERE token=?", (token,))
    conn.commit()
    conn.close()
