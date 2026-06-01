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
PORT: int = int(_s.get("port", 8080))
MAX_WORKERS: int = int(_s.get("max_workers", 4))

VERSION = "1.5.2"
GITHUB_REPO = "bolgov0zero/NbmSearch"
