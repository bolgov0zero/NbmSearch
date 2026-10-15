"""
Лог-файл сервера с ограничением по возрасту записей.

В frozen-режиме (exe/служба) stdout и stderr процесса перенаправляются в один файл
nbmsearch.log. Здесь этот файл оборачивается в потокобезопасный поток, который:
  • при старте обрезает записи старше LOG_KEEP_DAYS (уже существующий файл урезается);
  • затем раз в сутки делает то же самое, пока процесс работает.

Все записи (logging, print, traceback, предупреждения библиотек) идут через один
дескриптор под одной блокировкой, поэтому обрезка не конфликтует с записью.
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

LOG_KEEP_DAYS = 30
_TRIM_INTERVAL = 24 * 3600          # как часто повторять обрезку во время работы
_CHUNK = 1024 * 1024

# Строки лога начинаются с "YYYY-MM-DD HH:MM:SS" (дальше ",mmm" или пробел).
# Строки без метки (traceback, вывод библиотек) принадлежат предыдущей записи.
_TS_RE = re.compile(rb"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def _find_cut_offset(path: Path, cutoff_str: str) -> int:
    """Смещение первой строки, которую нужно оставить.

    0      — обрезать нечего (или в файле нет строк с датой);
    size   — все датированные записи старше границы (файл можно очистить целиком).
    Лог хронологический, поэтому останавливаемся на первой «свежей» записи —
    стоимость пропорциональна размеру удаляемой части, а не всего файла.
    """
    offset = 0
    seen_dated = False
    with open(path, "rb") as f:
        for line in f:
            m = _TS_RE.match(line)
            if m:
                if m.group(1).decode("ascii") >= cutoff_str:
                    return offset if seen_dated else 0
                seen_dated = True
            offset += len(line)
    return offset if seen_dated else 0


def _copy_tail_atomic(path: Path, cut: int) -> None:
    """Записывает хвост файла (начиная с cut) во временный файл и подменяет им оригинал."""
    tmp = path.with_name(path.name + ".trim")
    try:
        with open(path, "rb") as src, open(tmp, "wb") as dst:
            src.seek(cut)
            while True:
                chunk = src.read(_CHUNK)
                if not chunk:
                    break
                dst.write(chunk)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _copy_tail_inplace(path: Path, cut: int) -> None:
    """Запасной путь, если подмена файла запрещена (например, файл открыт другой программой):
    сдвигаем хвост в начало того же файла и усекаем его."""
    with open(path, "r+b") as f:
        read_pos = cut
        write_pos = 0
        while True:
            f.seek(read_pos)
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            f.seek(write_pos)
            f.write(chunk)
            read_pos += len(chunk)
            write_pos += len(chunk)
        f.truncate(write_pos)


def trim_log_file(path: Path, keep_days: int = LOG_KEEP_DAYS, now: float | None = None) -> int:
    """Удаляет из файла записи старше keep_days. Возвращает число удалённых байт.

    Вызывать нужно, когда файл не пишется (или под блокировкой записи)."""
    path = Path(path)
    if not path.exists():
        return 0
    ref = now if now is not None else time.time()
    cutoff_str = datetime.fromtimestamp(ref - keep_days * 86400).strftime("%Y-%m-%d %H:%M:%S")
    cut = _find_cut_offset(path, cutoff_str)
    if cut <= 0:
        return 0
    try:
        _copy_tail_atomic(path, cut)
    except OSError:
        _copy_tail_inplace(path, cut)
    return cut


class TrimmedLogStream:
    """Файлоподобный объект для sys.stdout / sys.stderr и обработчиков logging."""

    def __init__(self, path: Path, keep_days: int = LOG_KEEP_DAYS):
        self.path = Path(path)
        self.keep_days = keep_days
        self._lock = threading.RLock()
        self._f = None
        self._last_trim = time.monotonic()
        self.trimmed_bytes = 0
        with self._lock:
            self._trim_locked()
            self._open_locked()

    # ── internals ────────────────────────────────────────────────────────────
    def _open_locked(self) -> None:
        self._f = open(self.path, "a", encoding="utf-8", errors="replace", buffering=1)

    def _trim_locked(self) -> None:
        try:
            if self._f is not None:
                self._f.flush()
                self._f.close()
                self._f = None
            self.trimmed_bytes += trim_log_file(self.path, self.keep_days)
        except Exception as e:  # обрезка не должна ронять сервер
            try:
                sys.__stderr__ and sys.__stderr__.write(f"log trim failed: {e}\n")
            except Exception:
                pass
        finally:
            self._last_trim = time.monotonic()

    def _maybe_trim_locked(self) -> None:
        if time.monotonic() - self._last_trim < _TRIM_INTERVAL:
            return
        self._trim_locked()
        self._open_locked()

    # ── file-like API ────────────────────────────────────────────────────────
    def write(self, s) -> int:
        if not s:
            return 0
        with self._lock:
            self._maybe_trim_locked()
            if self._f is None:
                self._open_locked()
            self._f.write(s)
        return len(s)

    def flush(self) -> None:
        with self._lock:
            if self._f is not None:
                self._f.flush()

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def fileno(self) -> int:
        with self._lock:
            if self._f is None:
                self._open_locked()
            return self._f.fileno()

    @property
    def encoding(self) -> str:
        return "utf-8"

    @property
    def errors(self) -> str:
        return "replace"

    def close(self) -> None:
        with self._lock:
            if self._f is not None:
                self._f.close()
                self._f = None


_installed: TrimmedLogStream | None = None


def install(path: Path, keep_days: int = LOG_KEEP_DAYS) -> TrimmedLogStream:
    """Подменяет sys.stdout/sys.stderr потоком с обрезкой. Повторный вызов безопасен."""
    global _installed
    if _installed is not None and _installed.path == Path(path):
        return _installed
    stream = TrimmedLogStream(Path(path), keep_days)
    sys.stdout = stream
    sys.stderr = stream
    _installed = stream
    return stream
