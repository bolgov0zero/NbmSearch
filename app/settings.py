"""
Runtime settings — loaded from settings.json next to the executable.
Falls back to defaults when the file doesn't exist.
"""
import json
import hashlib
import sys
from pathlib import Path


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _base_dir()
SETTINGS_PATH = BASE_DIR / "settings.json"
DATA_DIR = BASE_DIR / "data"
DB_PATH = str(BASE_DIR / "nbmsearch.db")

_DEFAULTS: dict = {
    "port": 8080,
    "max_workers": 4,
    "heavy_workers": 1,   # concurrent limit for memory-heavy formats (pdf, xls, xlsx)
    "max_file_mb": 64,    # файлы больше (кроме txt/csv) индексируются только по имени; 0 — без лимита
    "max_text_chars": 20_000_000,  # предел извлекаемого текста из одного файла
    "admin_password_hash": hashlib.sha256(b"Gjgeufq4hfpf!").hexdigest(),
}


def load() -> dict:
    if SETTINGS_PATH.exists():
        try:
            with open(SETTINGS_PATH, encoding="utf-8") as f:
                data = json.load(f)
            for k, v in _DEFAULTS.items():
                data.setdefault(k, v)
            return data
        except Exception:
            pass
    return dict(_DEFAULTS)


def save(data: dict) -> None:
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def verify_password(password: str) -> bool:
    h = hashlib.sha256(password.encode()).hexdigest()
    return h == load().get("admin_password_hash", "")


# Module-level constants (read once at startup)
_s = load()
PORT: int         = int(_s.get("port", 8080))
MAX_WORKERS: int  = int(_s.get("max_workers", 4))
HEAVY_WORKERS: int = int(_s.get("heavy_workers", 1))
MAX_FILE_BYTES: int = max(0, int(_s.get("max_file_mb", 64))) * 1024 * 1024
MAX_TEXT_CHARS: int = max(100_000, int(_s.get("max_text_chars", 20_000_000)))

VERSION = "1.5.88"
GITHUB_REPO = "bolgov0zero/NbmSearch"
