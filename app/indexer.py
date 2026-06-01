import os
import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from app.settings import MAX_WORKERS
from app import database as db

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".docx", ".doc", ".xlsx", ".xls", ".pdf", ".rtf", ".txt", ".csv"}

# ── Watchdog event log ────────────────────────────────────────────────────────
# folder_id → list of {"ts": float, "type": str, "name": str, "path": str}
_LOG_TTL = 7 * 24 * 3600   # 7 days in seconds
_LOG_MAX = 1000             # max entries per folder (oldest pruned first)

_watchdog_log: dict[int, list] = {}
_watchdog_log_lock = threading.Lock()


def _log_event(folder_id: int, event_type: str, path: str):
    name = os.path.basename(path)
    entry = {"ts": time.time(), "type": event_type, "name": name, "path": path}
    with _watchdog_log_lock:
        log = _watchdog_log.setdefault(folder_id, [])
        log.insert(0, entry)
        # Prune entries older than TTL and keep max size
        cutoff = time.time() - _LOG_TTL
        _watchdog_log[folder_id] = [e for e in log if e["ts"] >= cutoff][:_LOG_MAX]


def get_watchdog_log(folder_id: int) -> list:
    cutoff = time.time() - _LOG_TTL
    with _watchdog_log_lock:
        return [e for e in _watchdog_log.get(folder_id, []) if e["ts"] >= cutoff]


def clear_watchdog_log(folder_id: int):
    with _watchdog_log_lock:
        _watchdog_log[folder_id] = []


# ── Progress tracking ─────────────────────────────────────────────────────────
# folder_id → {"total": int, "done": int, "status": "idle"|"indexing"|"done"}
_progress: dict[int, dict] = {}
_progress_lock = threading.Lock()


def get_progress(folder_id: int) -> dict:
    with _progress_lock:
        return dict(_progress.get(folder_id, {"total": 0, "done": 0, "status": "idle"}))


def _set_progress(folder_id: int, total: int, done: int, status: str):
    with _progress_lock:
        _progress[folder_id] = {"total": total, "done": done, "status": status}


# ── Per-folder reindex scheduling ─────────────────────────────────────────────
# folder_id → next scheduled reindex timestamp
_next_reindex: dict[int, float] = {}
_scheduler_running = False


def _schedule_next(folder_id: int, reindex_minutes: int):
    _next_reindex[folder_id] = time.time() + reindex_minutes * 60


def get_next_reindex(folder_id: int) -> float | None:
    return _next_reindex.get(folder_id)


def reindex_scheduler():
    while True:
        time.sleep(30)
        now = time.time()
        schedules = db.get_schedules()
        folders = {f["id"]: f for f in db.get_folders()}
        for s in schedules:
            nxt = s.get("next_run_at") or 0
            if now >= nxt:
                folder = folders.get(s["folder_id"])
                if folder:
                    db.update_schedule_run(s["id"], s["reindex_minutes"])
                    threading.Thread(target=index_folder, args=(folder,), daemon=True).start()


# ── Text extraction ───────────────────────────────────────────────────────────

def extract_text(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in (".txt", ".csv"):
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
            try:
                parts = []
                for ws in wb.worksheets:
                    try:
                        for row in ws.iter_rows(values_only=True):
                            try:
                                parts.append(" ".join(str(c) for c in row if c is not None))
                            except Exception:
                                pass
                    except Exception:
                        pass
                return "\n".join(parts)
            finally:
                wb.close()

        elif ext == ".xls":
            import xlrd
            wb = xlrd.open_workbook(path)
            try:
                parts = []
                for sheet in wb.sheets():
                    for row_idx in range(sheet.nrows):
                        parts.append(" ".join(str(sheet.cell_value(row_idx, c))
                                              for c in range(sheet.ncols)))
                return "\n".join(parts)
            finally:
                wb.release_resources()

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
    import re
    import olefile
    try:
        with olefile.OleFileIO(path) as ole:
            if not ole.exists("WordDocument"):
                return ""
            stream = ole.openstream("WordDocument").read()
            text = stream.decode("utf-16-le", errors="ignore")
            text = re.sub(r"[^\x20-\x7eЀ-ӿ\n\r\t]+", " ", text)
            return " ".join(text.split())
    except Exception:
        with open(path, "rb") as f:
            raw = f.read()
        import re
        text = raw.decode("utf-16-le", errors="ignore")
        text = re.sub(r"[^\x20-\x7eЀ-ӿ\n\r\t]+", " ", text)
        return " ".join(text.split())


# ── Indexing ──────────────────────────────────────────────────────────────────

def index_file(folder_id: int, path: str):
    ext = os.path.splitext(path)[1].lower()
    if ext not in SUPPORTED_EXTENSIONS:
        return
    if os.path.basename(path).startswith("~$"):
        return
    try:
        stat = os.stat(path)
        if stat.st_size == 0:
            return  # skip empty files (being written / temp placeholders)
        content = extract_text(path)
        name = os.path.basename(path)
        db.upsert_file(
            folder_id=folder_id,
            path=path,
            name=name,
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


def index_folder(folder: dict, full: bool = False):
    folder_id = folder["id"]
    folder_path = folder["path"]
    folder_name = folder["name"]
    last_reindex_at = None if full else folder.get("last_reindex_at")  # None → full index

    if not os.path.isdir(folder_path):
        logger.warning("Folder does not exist: %s", folder_path)
        return

    all_files = []
    for root, _, filenames in os.walk(folder_path):
        for fname in filenames:
            if os.path.splitext(fname)[1].lower() in SUPPORTED_EXTENSIONS and not fname.startswith("~$"):
                all_files.append(os.path.join(root, fname))

    # Get already-indexed paths: needed for stale detection and new-file detection
    indexed_paths = db.get_all_paths_in_folder(folder_id)

    if last_reindex_at:
        # Incremental: only new files or files modified after last reindex
        to_index = []
        for f in all_files:
            try:
                if os.stat(f).st_mtime > last_reindex_at or f not in indexed_paths:
                    to_index.append(f)
            except OSError:
                pass
        logger.info("Indexing folder '%s': %d changed/new of %d total (since %s)",
                    folder_name, len(to_index), len(all_files),
                    time.strftime("%d.%m.%Y %H:%M", time.localtime(last_reindex_at)))
    else:
        to_index = all_files
        logger.info("Indexing folder '%s': %d files (full)", folder_name, len(to_index))

    total = len(to_index)
    _set_progress(folder_id, total, 0, "indexing")

    done = 0
    def _index_and_count(path):
        nonlocal done
        index_file(folder_id, path)
        done += 1
        _set_progress(folder_id, total, done, "indexing")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        executor.map(_index_and_count, to_index)

    # Remove deleted files
    current_paths = set(all_files)
    stale_paths = indexed_paths - current_paths
    logger.info("Folder '%s' stats: on_disk=%d in_db=%d stale=%d",
                folder_name, len(all_files), len(indexed_paths), len(stale_paths))
    for stale in stale_paths:
        logger.info("Removing stale: %s", stale)
        db.delete_file_from_folder(folder_id, stale)

    _set_progress(folder_id, total, total, "done")
    db.set_folder_last_reindex(folder_id, time.time())
    db.update_folder_file_count(folder_id)
    logger.info("Done indexing folder '%s'", folder_name)


def full_reindex():
    for folder in db.get_folders():
        index_folder(folder)
        _schedule_next(folder["id"], folder["reindex_minutes"])


# ── Watchdog ──────────────────────────────────────────────────────────────────

_observer: Observer | None = None
_observer_lock = threading.Lock()


_WATCHDOG_DEBOUNCE = 3.0  # seconds to wait after last event before indexing


class FileEventHandler(FileSystemEventHandler):
    def __init__(self, folder_id: int):
        self.folder_id = folder_id
        self._timers: dict[str, threading.Timer] = {}
        self._event_types: dict[str, str] = {}   # path → first event type
        self._timers_lock = threading.Lock()

    def _schedule(self, path: str, event_type: str = "modified"):
        """Debounce: index the file only after DEBOUNCE seconds of silence.
        The first event type wins — created stays created even if modified follows."""
        with self._timers_lock:
            existing = self._timers.pop(path, None)
            if existing:
                existing.cancel()
            # Keep the first (most specific) event type
            if path not in self._event_types:
                self._event_types[path] = event_type
            t = threading.Timer(_WATCHDOG_DEBOUNCE, self._do_index,
                                args=(path, self._event_types[path]))
            self._timers[path] = t
            t.daemon = True
            t.start()

    def _do_index(self, path: str, event_type: str = "modified"):
        with self._timers_lock:
            self._timers.pop(path, None)
            self._event_types.pop(path, None)
        # Skip if file disappeared or is still empty (being written)
        try:
            stat = os.stat(path)
            if stat.st_size == 0:
                logger.debug("Watchdog: skipping empty file %s", path)
                return
        except OSError:
            return
        # Skip if mtime and size match what's already indexed (false positive event)
        meta = db.get_file_meta(self.folder_id, path)
        if meta is not None:
            db_mtime, db_size = meta
            if abs(stat.st_mtime - db_mtime) < 1.0 and stat.st_size == db_size:
                logger.debug("Watchdog: skipping unchanged file %s", path)
                return
        index_file(self.folder_id, path)
        db.update_folder_file_count(self.folder_id)
        _log_event(self.folder_id, event_type, path)

    @staticmethod
    def _is_temp(path: str) -> bool:
        return os.path.basename(path).startswith("~$")

    def on_created(self, event):
        if not event.is_directory and not self._is_temp(event.src_path):
            self._schedule(event.src_path, "created")

    def on_modified(self, event):
        if not event.is_directory and not self._is_temp(event.src_path):
            self._schedule(event.src_path, "modified")

    def on_deleted(self, event):
        if not event.is_directory and not self._is_temp(event.src_path):
            db.delete_file_from_folder(self.folder_id, event.src_path)
            db.update_folder_file_count(self.folder_id)
            _log_event(self.folder_id, "deleted", event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            if not self._is_temp(event.src_path):
                db.delete_file_from_folder(self.folder_id, event.src_path)
            if not self._is_temp(event.dest_path):
                self._schedule(event.dest_path, "moved")


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

        folders = [f for f in db.get_folders() if f.get("watchdog_enabled")]
        if not folders:
            return

        observer = Observer()
        for folder in folders:
            if os.path.isdir(folder["path"]):
                handler = FileEventHandler(folder["id"])
                observer.schedule(handler, folder["path"], recursive=True)
                logger.info("Watchdog watching: %s", folder["path"])
        observer.start()
        _observer = observer
