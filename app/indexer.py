import os
import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from app.config import SEARCH_FOLDERS, REINDEX_INTERVAL_MINUTES, MAX_WORKERS
from app import database as db

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".docx", ".xlsx", ".pdf", ".txt"}

last_reindex_time: float = 0.0
next_reindex_time: float = 0.0
_lock = threading.Lock()


def extract_text(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".txt":
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
        elif ext == ".docx":
            import docx
            doc = docx.Document(path)
            return "\n".join(p.text for p in doc.paragraphs)
        elif ext == ".xlsx":
            import openpyxl
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            parts = []
            for ws in wb.worksheets:
                for row in ws.iter_rows(values_only=True):
                    parts.append(" ".join(str(c) for c in row if c is not None))
            return "\n".join(parts)
        elif ext == ".pdf":
            import pdfplumber
            with pdfplumber.open(path) as pdf:
                return "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception as e:
        logger.warning("Cannot extract text from %s: %s", path, e)
    return ""


def _folder_name_for_path(path: str) -> str:
    for folder in SEARCH_FOLDERS:
        folder_path = os.path.normpath(folder["path"])
        if os.path.normpath(path).startswith(folder_path):
            return folder["name"]
    return ""


def index_file(path: str):
    ext = os.path.splitext(path)[1].lower()
    if ext not in SUPPORTED_EXTENSIONS:
        return
    try:
        stat = os.stat(path)
        content = extract_text(path)
        name = os.path.basename(path)
        folder_name = _folder_name_for_path(path)
        db.upsert_file(
            path=path,
            name=name,
            folder_name=folder_name,
            size=stat.st_size,
            modified_at=stat.st_mtime,
            content=f"{name}\n{content}",
            indexed_at=time.time(),
        )
        logger.debug("Indexed: %s", path)
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.error("Error indexing %s: %s", path, e)


def full_reindex():
    global last_reindex_time, next_reindex_time
    logger.info("Starting full reindex of %d folders", len(SEARCH_FOLDERS))
    with _lock:
        last_reindex_time = time.time()
        next_reindex_time = last_reindex_time + REINDEX_INTERVAL_MINUTES * 60

    all_paths = set()
    files_to_process = []

    for folder in SEARCH_FOLDERS:
        folder_path = folder["path"]
        if not os.path.isdir(folder_path):
            logger.warning("Folder does not exist: %s", folder_path)
            continue
        for root, _, filenames in os.walk(folder_path):
            for fname in filenames:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in SUPPORTED_EXTENSIONS:
                    continue
                full_path = os.path.join(root, fname)
                all_paths.add(full_path)
                files_to_process.append(full_path)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        executor.map(index_file, files_to_process)

    import sqlite3
    from app.config import DB_PATH
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT path FROM files")
    db_paths = {row[0] for row in cur.fetchall()}
    conn.close()

    for path in db_paths - all_paths:
        db.delete_file(path)
        logger.debug("Removed from index: %s", path)

    logger.info("Full reindex complete. %d files processed.", len(files_to_process))


def reindex_scheduler():
    while True:
        time.sleep(REINDEX_INTERVAL_MINUTES * 60)
        full_reindex()


class FileEventHandler(FileSystemEventHandler):
    def _handle(self, path: str, deleted: bool = False):
        if deleted:
            db.delete_file(path)
        else:
            index_file(path)

    def on_created(self, event):
        if not event.is_directory:
            self._handle(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._handle(event.src_path)

    def on_deleted(self, event):
        if not event.is_directory:
            self._handle(event.src_path, deleted=True)

    def on_moved(self, event):
        if not event.is_directory:
            self._handle(event.src_path, deleted=True)
            self._handle(event.dest_path)


def start_watchdog():
    observer = Observer()
    handler = FileEventHandler()
    for folder in SEARCH_FOLDERS:
        folder_path = folder["path"]
        if os.path.isdir(folder_path):
            observer.schedule(handler, folder_path, recursive=True)
            logger.info("Watchdog watching: %s", folder_path)
        else:
            logger.warning("Watchdog skipped (not found): %s", folder_path)
    observer.start()
    return observer
