"""
Блокировка «владельца фоновых задач».

Индексация по расписанию, watchdog и запись статистики должны работать только в одном
процессе. Если по ошибке запущены два экземпляра сервера (зависший старый процесс, ручной
запуск рядом со службой, перезапуск, наложившийся на остановку), они начинают
параллельно индексировать одни и те же файлы и писать в одни и те же БД — отсюда
дублирующиеся записи, ошибки записи и кратный рост памяти.

Блокировка — файл nbmsearch.lock рядом с БД. ОС снимает её сама при завершении процесса
(в том числе аварийном), поэтому «залипнуть» она не может. Второй экземпляр ждёт, пока
первый завершится (так же работает перезапуск), и только потом берёт фоновые задачи на себя.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_handle = None


def _try_lock(fh) -> bool:
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def try_acquire(path: Path) -> bool:
    """Одна попытка взять блокировку. True — мы владелец фоновых задач."""
    global _handle
    if _handle is not None:
        return True
    fh = open(path, "a+b")
    if not _try_lock(fh):
        fh.close()
        return False
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()).encode("ascii"))
        fh.flush()
    except OSError:
        pass
    _handle = fh   # держим открытым до конца жизни процесса
    return True


def acquire(path: Path, wait: float | None = None, poll: float = 2.0) -> bool:
    """Ждёт освобождения блокировки (старый процесс ещё может завершаться во время
    перезапуска). wait=None — ждать сколько потребуется; иначе не дольше wait секунд.

    Возвращает True, если блокировка получена; False — если за wait секунд не удалось.
    Если блокировка занята другим живым процессом, этот не запускает индексацию и
    watchdog вообще — второй экземпляр не должен дублировать работу первого.
    Любая неожиданная ошибка самого механизма блокировки трактуется как «получена»:
    сбой этой защиты никогда не должен отключать индексацию и watchdog."""
    started = time.monotonic()
    deadline = None if wait is None else started + wait
    next_warn = started + 30.0
    try:
        while True:
            if try_acquire(path):
                return True
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                logger.warning("Фоновые задачи уже выполняет другой процесс (%s)", path.name)
                return False
            if now >= next_warn:
                logger.warning("Ожидание блокировки %s: фоновые задачи выполняет другой "
                               "процесс NbmSearch (проверьте, нет ли второго экземпляра)", path.name)
                next_warn = now + 600.0
            time.sleep(poll)
    except Exception as e:
        logger.error("Instance lock unavailable (%s) — continuing without it", e)
        return True
