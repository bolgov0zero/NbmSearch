import os
import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from app.config import REINDEX_INTERVAL_MINUTES, MAX_WORKERS
from app import database as db

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".docx", ".doc", ".xlsx", ".xls", ".pdf", ".rtf", ".txt"}

last_reindex_time: float = 0.0
next_reindex_time: float = 0.0
_lock = threading.Lock()

# Watchdog observer — пересоздаётся при изменении списка папок
_observer: Observer | None = None
_observer_lock = threading.Lock()


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

        elif ext == ".doc":
            return _extract_doc(path)

        elif ext == ".xlsx":
            import openpyxl
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            parts = []
            for ws in wb.worksheets:
                for row in ws.iter_rows(values_only=True):
                    parts.append(" ".join(str(c) for c in row if c is not None))
            return "\n".join(parts)

        elif ext == ".xls":
            import xlrd
            wb = xlrd.open_workbook(path)
            parts = []
            for sheet in wb.sheets():
                for row_idx in range(sheet.nrows):
                    parts.append(" ".join(str(sheet.cell_value(row_idx, c))
                                          for c in range(sheet.ncols)))
            return "\n".join(parts)

        elif ext == ".pdf":
            import pdfplumber
            with pdfplumber.open(path) as pdf:
                return "\n".join(page.extract_text() or "" for page in pdf.pages)

        elif ext == ".rtf":
            from striprtf.striprtf import rtf_to_text
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return rtf_to_text(f.read())

    except Exception as e:
        logger.warning("Cannot extract text from %s: %s", path, e)
    return ""


def _extract_doc(path: str) -> str:
    """Extract text from legacy .doc (OLE/Word97) files using olefile."""
    import re
    import olefile
    try:
        with olefile.OleFileIO(path) as ole:
            if not ole.exists("WordDocument"):
                return ""
            stream = ole.openstream("WordDocument").read()
            # The text in Word97 streams is UTF-16-LE starting at offset 0x900+
            text = stream.decode("utf-16-le", errors="ignore")
            # Keep printable ASCII and Cyrillic, collapse garbage
            text = re.sub(r"[^\x20-\x7eЀ-ӿ\n\r\t]+", " ", text)
            return " ".join(text.split())
    except Exception:
        # Fallback: scan raw bytes for UTF-16 strings (handles corrupt/unusual files)
        with open(path, "rb") as f:
            raw = f.read()
        text = raw.decode("utf-16-le", errors="ignore")
        text = re.sub(r"[^\x20-\x7eЀ-ӿ\n\r\t]+", " ", text)
        return " ".join(text.split())


def _folder_name_for_path(path: str, folders: list[dict]) -> str:
    for folder in folders:
        if os.path.normpath(path).startswith(os.path.normpath(folder["path"])):
            return folder["name"]
    return ""


def index_file(path: str, folder_name: str = ""):
    ext = os.path.splitext(path)[1].lower()
    if ext not in SUPPORTED_EXTENSIONS:
        return
    try:
        stat = os.stat(path)
        content = extract_text(path)
        name = os.path.basename(path)
        if not folder_name:
            folder_name = _folder_name_for_path(path, db.get_folders())
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


def index_folder(folder: dict):
    folder_path = folder["path"]
    folder_name = folder["name"]
    if not os.path.isdir(folder_path):
        logger.warning("Folder does not exist: %s", folder_path)
        return
    files = []
    for root, _, filenames in os.walk(folder_path):
        for fname in filenames:
            if os.path.splitext(fname)[1].lower() in SUPPORTED_EXTENSIONS:
                files.append(os.path.join(root, fname))
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        executor.map(lambda p: index_file(p, folder_name), files)
    logger.info("Indexed folder '%s': %d files", folder_name, len(files))


def full_reindex():
    global last_reindex_time, next_reindex_time
    folders = db.get_folders()
    logger.info("Starting full reindex of %d folders", len(folders))
    with _lock:
        last_reindex_time = time.time()
        next_reindex_time = last_reindex_time + REINDEX_INTERVAL_MINUTES * 60

    all_paths: set[str] = set()
    for folder in folders:
        folder_path = folder["path"]
        if not os.path.isdir(folder_path):
            continue
        for root, _, filenames in os.walk(folder_path):
            for fname in filenames:
                if os.path.splitext(fname)[1].lower() in SUPPORTED_EXTENSIONS:
                    full_path = os.path.join(root, fname)
                    all_paths.add(full_path)
                    index_file(full_path, folder["name"])

    import sqlite3
    from app.config import DB_PATH
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT path FROM files")
    db_paths = {row[0] for row in cur.fetchall()}
    conn.close()
    for path in db_paths - all_paths:
        db.delete_file(path)

    logger.info("Full reindex complete.")


def reindex_scheduler():
    while True:
        time.sleep(REINDEX_INTERVAL_MINUTES * 60)
        full_reindex()


# ── Watchdog ──────────────────────────────────────────────────────────────────

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


def restart_watchdog():
    global _observer
    with _observer_lock:
        if _observer is not None:
            try:
                _observer.stop()
                _observer.join(timeout=5)
            except Exception:
                pass
            _observer = None

        folders = db.get_folders()
        if not folders:
            return

        observer = Observer()
        handler = FileEventHandler()
        for folder in folders:
            folder_path = folder["path"]
            if os.path.isdir(folder_path):
                observer.schedule(handler, folder_path, recursive=True)
                logger.info("Watchdog watching: %s", folder_path)
            else:
                logger.warning("Watchdog skipped (not found): %s", folder_path)
        observer.start()
        _observer = observer
