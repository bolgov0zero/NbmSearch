"""
Перезапуск сервера: вручную, по расписанию (дни недели + время) и по потреблению памяти.

Настройки и состояние хранятся в основной БД (таблица app_settings), поэтому переживают
перезапуск и меняются на лету из админки — без правки файлов и без рестарта.

Как принимается решение о перезапуске (Monitor.tick, вызывается раз в TICK_SECONDS):
  • По расписанию: в выбранные дни в заданное время. Если в этот момент идёт индексация,
    перезапуск откладывается до её окончания, но не дольше wait_indexing_min минут.
    Слот, который уже отработан (или пришёлся на время, когда процесс ещё не работал),
    повторно не срабатывает.
  • По памяти, мягкий порог: RSS выше порога несколько проверок подряд → перезапуск, но
    только когда нет индексации и (если включено) внутри разрешённого окна времени.
  • По памяти, аварийный порог: перезапуск сразу, независимо от окна и индексации
    (индексация продолжится с места остановки — она инкрементальная).
  • Защита от циклов: не чаще одного автоматического перезапуска в COOLDOWN, не раньше
    MIN_UPTIME после старта, не более LOOP_MAX перезапусков по памяти за LOOP_WINDOW.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from app import database as db
from app.settings import PORT

logger = logging.getLogger(__name__)

SETTINGS_KEY = "restart_settings"
STATE_KEY = "restart_state"

DEFAULTS: dict = {
    "schedule_enabled": False,
    "days": [],                       # 0 = понедельник … 6 = воскресенье
    "time": "03:00",
    "wait_indexing_min": 60,          # сколько ждать окончания индексации (0 — не ждать)
    "memory_enabled": False,
    "memory_limit_mb": 400,           # мягкий порог
    "memory_window_enabled": False,   # мягкий порог срабатывает только в окне времени
    "memory_window_from": "01:00",
    "memory_window_to": "06:00",
    "memory_hard_mb": 1000,           # аварийный порог, 0 — выключен
}

TICK_SECONDS = 20
MIN_UPTIME = 120            # не перезапускаемся раньше, чем через 2 мин после старта
COOLDOWN = 15 * 60          # между автоматическими перезапусками
SCHEDULE_GRACE = 15 * 60    # слот расписания «живёт» 15 минут после наступления
MEM_SOFT_HITS = 3           # подряд проверок выше мягкого порога (≈ 1 мин)
MEM_HARD_HITS = 2           # подряд проверок выше аварийного порога (≈ 40 с)
LOOP_WINDOW = 3600
LOOP_MAX = 3
HISTORY_MAX = 20

_PROC_START = time.time()
_HHMM = re.compile(r"^(\d{1,2}):(\d{2})$")


# ── Settings ──────────────────────────────────────────────────────────────────

def _parse_hhmm(value, field: str) -> tuple[int, int]:
    m = _HHMM.match(str(value or "").strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError(f"Некорректное время ({field}): ожидается ЧЧ:ММ")
    return int(m.group(1)), int(m.group(2))


def _fmt_hhmm(h: int, m: int) -> str:
    return f"{h:02d}:{m:02d}"


def _to_int(value, field: str, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"Некорректное число ({field})")
    if not lo <= n <= hi:
        raise ValueError(f"Значение «{field}» должно быть от {lo} до {hi}")
    return n


def normalize_settings(raw: dict | None) -> dict:
    """Проверяет и приводит настройки к каноническому виду. ValueError — понятное сообщение."""
    s = dict(DEFAULTS)
    for key in DEFAULTS:
        if raw and key in raw:
            s[key] = raw[key]

    for key in ("schedule_enabled", "memory_enabled", "memory_window_enabled"):
        s[key] = bool(s[key])

    days = s["days"]
    if not isinstance(days, (list, tuple)):
        raise ValueError("Некорректный список дней")
    s["days"] = sorted({_to_int(d, "день недели", 0, 6) for d in days})

    s["time"] = _fmt_hhmm(*_parse_hhmm(s["time"], "время перезапуска"))
    s["memory_window_from"] = _fmt_hhmm(*_parse_hhmm(s["memory_window_from"], "начало окна"))
    s["memory_window_to"] = _fmt_hhmm(*_parse_hhmm(s["memory_window_to"], "конец окна"))

    s["wait_indexing_min"] = _to_int(s["wait_indexing_min"], "ожидание индексации, мин", 0, 720)
    s["memory_limit_mb"] = _to_int(s["memory_limit_mb"], "порог памяти, МБ", 100, 65536)
    s["memory_hard_mb"] = _to_int(s["memory_hard_mb"], "аварийный порог, МБ", 0, 65536)

    if s["schedule_enabled"] and not s["days"]:
        raise ValueError("Выберите хотя бы один день недели")
    if s["memory_hard_mb"] and s["memory_hard_mb"] <= s["memory_limit_mb"]:
        raise ValueError("Аварийный порог должен быть больше основного (или 0 — выключен)")
    if s["memory_window_enabled"] and s["memory_window_from"] == s["memory_window_to"]:
        raise ValueError("Начало и конец окна не должны совпадать")
    return s


def load_settings() -> dict:
    raw = db.get_setting(SETTINGS_KEY, {})
    try:
        return normalize_settings(raw)
    except ValueError:
        # Повреждённые значения не должны ломать планировщик — используем значения по умолчанию
        return dict(DEFAULTS)


def save_settings(raw: dict) -> dict:
    s = normalize_settings(raw)
    db.set_setting(SETTINGS_KEY, s)
    return s


# ── State (последние перезапуски, отработанный слот расписания) ───────────────

def load_state() -> dict:
    st = db.get_setting(STATE_KEY, {})
    if not isinstance(st, dict):
        st = {}
    st.setdefault("last_restart_at", None)
    st.setdefault("handled_slot", None)
    st.setdefault("history", [])
    return st


def save_state(st: dict) -> None:
    try:
        db.set_setting(STATE_KEY, st)
    except Exception as e:
        logger.error("Cannot persist restart state: %s", e)


def _record_restart(reason: str, label: str, rss_mb: Optional[float]) -> None:
    st = load_state()
    now = time.time()
    st["last_restart_at"] = now
    st["history"] = (st.get("history") or [])[-(HISTORY_MAX - 1):] + [
        {"at": now, "reason": reason, "label": label,
         "rss_mb": round(rss_mb, 1) if rss_mb is not None else None}
    ]
    save_state(st)


# ── Pure decision logic ───────────────────────────────────────────────────────

def in_window(now: datetime, start: str, end: str) -> bool:
    """Попадает ли время в окно [start, end) — окно может переходить через полночь."""
    sh, sm = _parse_hhmm(start, "начало окна")
    eh, em = _parse_hhmm(end, "конец окна")
    cur = now.hour * 60 + now.minute
    a, b = sh * 60 + sm, eh * 60 + em
    return a <= cur < b if a < b else (cur >= a or cur < b)


def next_scheduled(s: dict, now: datetime) -> Optional[datetime]:
    if not s["schedule_enabled"] or not s["days"]:
        return None
    h, m = _parse_hhmm(s["time"], "время перезапуска")
    for offset in range(0, 8):
        day = now + timedelta(days=offset)
        cand = day.replace(hour=h, minute=m, second=0, microsecond=0)
        if day.weekday() in s["days"] and cand > now:
            return cand
    return None


@dataclass
class Decision:
    reason: str          # schedule | memory | memory_hard
    label: str
    rss_mb: Optional[float]


class Monitor:
    """Состояние между проверками + решение «пора ли перезапускаться». Не обращается ни к БД,
    ни к процессу — всё приходит параметрами, поэтому логика проверяется без запуска сервера."""

    def __init__(self, proc_start_ts: float):
        self.proc_start_ts = proc_start_ts
        self.pending: Optional[dict] = None    # слот расписания, ожидающий окончания индексации
        self.soft_hits = 0
        self.hard_hits = 0
        self.mem_state = "ok"                  # ok | over_limit | waiting_window | waiting_indexing | cooldown | loop_guard

    def _due_slot(self, now: datetime, s: dict) -> Optional[datetime]:
        h, m = _parse_hhmm(s["time"], "время перезапуска")
        for back in (0, 1):  # сегодняшний и вчерашний слот (на случай перехода через полночь)
            day = now - timedelta(days=back)
            if day.weekday() not in s["days"]:
                continue
            slot = day.replace(hour=h, minute=m, second=0, microsecond=0)
            if slot <= now < slot + timedelta(seconds=SCHEDULE_GRACE):
                return slot
        return None

    @staticmethod
    def _loop_guard(st: dict, now_ts: float) -> bool:
        recent = [h for h in st.get("history", [])
                  if str(h.get("reason", "")).startswith("memory") and h.get("at", 0) >= now_ts - LOOP_WINDOW]
        return len(recent) >= LOOP_MAX

    def tick(self, now: datetime, s: dict, st: dict, rss_mb: Optional[float],
             indexing: bool) -> tuple[Optional[Decision], bool]:
        """Возвращает (решение, изменилось_ли_состояние_st)."""
        now_ts = now.timestamp()
        changed = False
        if now_ts - self.proc_start_ts < MIN_UPTIME:
            return None, False

        last = st.get("last_restart_at")
        cooldown = bool(last) and now_ts - last < COOLDOWN

        # 1. Новый слот расписания
        if not s["schedule_enabled"]:
            self.pending = None
        elif self.pending is None and s["days"]:
            slot = self._due_slot(now, s)
            if slot is not None:
                key = slot.strftime("%Y-%m-%d %H:%M")
                slot_ts = slot.timestamp()
                if (key != st.get("handled_slot")
                        and self.proc_start_ts < slot_ts          # процесс работал в момент слота
                        and not (last and last >= slot_ts)):      # после слота уже был перезапуск
                    st["handled_slot"] = key
                    changed = True
                    self.pending = {"label": f"по расписанию ({key})", "since": now_ts}

        # 2. Слот ждёт окончания индексации
        if self.pending is not None:
            if cooldown:
                self.pending = None
            elif indexing and now_ts - self.pending["since"] < s["wait_indexing_min"] * 60:
                return None, changed
            else:
                label = self.pending["label"]
                if indexing:
                    label += ", индексация не закончилась за отведённое время"
                self.pending = None
                return Decision("schedule", label, rss_mb), changed

        # 3. Память
        self.mem_state = "ok"
        if not s["memory_enabled"] or rss_mb is None:
            self.soft_hits = self.hard_hits = 0
            return None, changed

        hard, limit = s["memory_hard_mb"], s["memory_limit_mb"]
        self.hard_hits = self.hard_hits + 1 if (hard and rss_mb >= hard) else 0
        self.soft_hits = self.soft_hits + 1 if rss_mb >= limit else 0
        if rss_mb >= limit:
            self.mem_state = "over_limit"

        allowed = True
        if cooldown:
            allowed, self.mem_state = False, ("cooldown" if rss_mb >= limit else "ok")
        elif self._loop_guard(st, now_ts):
            allowed, self.mem_state = False, ("loop_guard" if rss_mb >= limit else "ok")

        if self.hard_hits >= MEM_HARD_HITS and allowed:
            return Decision("memory_hard",
                            f"память {rss_mb:.0f} МБ достигла аварийного порога {hard} МБ", rss_mb), changed
        if self.soft_hits >= MEM_SOFT_HITS and allowed:
            if s["memory_window_enabled"] and not in_window(now, s["memory_window_from"], s["memory_window_to"]):
                self.mem_state = "waiting_window"
            elif indexing:
                self.mem_state = "waiting_indexing"
            else:
                return Decision("memory",
                                f"память {rss_mb:.0f} МБ превысила порог {limit} МБ", rss_mb), changed
        return None, changed


# ── Actually restarting ───────────────────────────────────────────────────────

def can_restart() -> tuple[bool, str]:
    if not getattr(sys, "frozen", False):
        return False, "Доступно только в собранном exe (сейчас запуск из исходников)"
    updater = Path(sys.executable).parent / "NbmSearchUpdater.exe"
    if not updater.exists():
        return False, "Рядом с NbmSearch.exe нет NbmSearchUpdater.exe"
    return True, ""


def _exec_restart(delay: float, legacy: bool) -> None:
    time.sleep(delay)
    if getattr(sys, "frozen", False):
        exe_path = str(Path(sys.executable))
        updater_path = str(Path(sys.executable).parent / "NbmSearchUpdater.exe")
        if os.path.exists(updater_path):
            flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            subprocess.Popen([updater_path, "--restart", str(os.getpid()), exe_path, str(PORT)],
                             creationflags=flags)
        elif not legacy:
            logger.error("Restart aborted: NbmSearchUpdater.exe not found")
            return
    elif not legacy:
        logger.error("Restart aborted: not running as exe")
        return
    # В режиме службы Updater сам делает stop+start через sc; os._exit завершает процесс
    # службы, чтобы SCM отметил её остановленной.
    try:
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(0)


def request_restart(reason: str, label: str, rss_mb: Optional[float] = None,
                    delay: float = 1.0, legacy: bool = False) -> tuple[bool, str]:
    """Запускает перезапуск через Updater. legacy=True сохраняет поведение прежнего
    эндпоинта панели управления (процесс завершается, даже если Updater не найден)."""
    if not legacy:
        ok, why = can_restart()
        if not ok:
            return False, why
    logger.warning("Restarting server: %s", label)
    _record_restart(reason, label, rss_mb)
    threading.Thread(target=_exec_restart, args=(delay, legacy), daemon=True).start()
    return True, ""


# ── Background loop + status ──────────────────────────────────────────────────

_monitor = Monitor(_PROC_START)
_is_indexing: Callable[[], bool] = lambda: False
_current_rss_mb: Callable[[], Optional[float]] = lambda: None


def configure(is_indexing: Callable[[], bool], current_rss_mb: Callable[[], Optional[float]]) -> None:
    global _is_indexing, _current_rss_mb
    _is_indexing = is_indexing
    _current_rss_mb = current_rss_mb


_loop_lock = threading.Lock()
_loop_running = False


def scheduler_loop() -> None:
    global _loop_running
    with _loop_lock:
        if _loop_running:     # сервер перезапущен внутри процесса — второй цикл не нужен
            return
        _loop_running = True
    warned = False
    while True:
        time.sleep(TICK_SECONDS)
        try:
            s = load_settings()
            if not (s["schedule_enabled"] or s["memory_enabled"]):
                _monitor.pending = None
                _monitor.soft_hits = _monitor.hard_hits = 0
                _monitor.mem_state = "ok"
                continue
            ok, why = can_restart()
            if not ok:
                if not warned:
                    logger.warning("Автоперезапуск включён, но недоступен: %s", why)
                    warned = True
                continue
            st = load_state()
            decision, changed = _monitor.tick(datetime.now(), s, st, _current_rss_mb(), _is_indexing())
            if changed:
                save_state(st)
            if decision:
                request_restart(decision.reason, decision.label, decision.rss_mb)
        except Exception as e:
            logger.error("Restart scheduler error: %s", e)


def _fmt(ts: Optional[float]) -> Optional[str]:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M") if ts else None


_REASON_TITLES = {
    "schedule": "по расписанию",
    "memory": "по памяти",
    "memory_hard": "по памяти (аварийно)",
    "manual": "вручную",
}


def get_status() -> dict:
    s = load_settings()
    st = load_state()
    now = datetime.now()
    ok, why = can_restart()
    nxt = next_scheduled(s, now)
    history = [
        {"at": _fmt(h.get("at")), "reason": _REASON_TITLES.get(h.get("reason"), h.get("reason", "")),
         "label": h.get("label", ""), "rss_mb": h.get("rss_mb")}
        for h in reversed(st.get("history", [])[-5:])
    ]
    return {
        "settings": s,
        "can_restart": ok,
        "can_restart_reason": why,
        "memory_mb": _current_rss_mb(),
        "uptime_s": int(time.time() - _PROC_START),
        "indexing": _is_indexing(),
        "next_scheduled": nxt.strftime("%d.%m.%Y %H:%M") if nxt else None,
        "pending": _monitor.pending["label"] if _monitor.pending else None,
        "memory_state": _monitor.mem_state,
        "last_restart": _fmt(st.get("last_restart_at")),
        "history": history,
    }
