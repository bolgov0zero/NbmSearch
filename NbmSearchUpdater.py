"""
NbmSearch Updater v2

New flow: download FIRST (old server keeps running), then stop old,
replace file, start new, verify. Status written to update_status.json.

Usage: NbmSearchUpdater.exe <pid> <download_url> <exe_path> [port]
"""
import sys
import os
import time
import json
import ssl
import urllib.request
import subprocess
from pathlib import Path


# ── Status helpers ────────────────────────────────────────────────────────────

def _write_status(path: str, stage: str, progress: int = 0,
                  message: str = "", error: str = None):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "stage":    stage,
                "progress": progress,
                "message":  message,
                "error":    error,
                "ts":       time.time(),
            }, f, ensure_ascii=False)
    except Exception:
        pass


# ── Process helpers ───────────────────────────────────────────────────────────

def _kill_pid(pid: int, timeout: int = 10) -> bool:
    """Kill process, return True when it's gone."""
    try:
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
        if handle and handle != -1:
            ctypes.windll.kernel32.TerminateProcess(handle, 0)
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        pass

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
            time.sleep(0.3)
        except (OSError, SystemError):
            return True
        except Exception:
            return True
    return False


def _verify_started(port: int, timeout: int = 30) -> bool:
    """Poll http://localhost:{port}/ until server responds."""
    url = f"http://localhost:{port}/"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status < 500:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 3:
        sys.exit(1)

    # ── Restart-only mode: NbmSearchUpdater.exe --restart <pid> <exe_path> [port] ──
    if sys.argv[1] == '--restart':
        pid      = int(sys.argv[2])
        exe_path = sys.argv[3]
        port     = int(sys.argv[4]) if len(sys.argv) > 4 else 8080
        _kill_pid(pid, timeout=15)
        time.sleep(0.5)
        try:
            subprocess.Popen([exe_path])
        except Exception as e:
            sys.exit(1)
        sys.exit(0)

    if len(sys.argv) < 4:
        sys.exit(1)

    pid          = int(sys.argv[1])
    download_url = sys.argv[2]
    exe_path     = sys.argv[3]
    port         = int(sys.argv[4]) if len(sys.argv) > 4 else 8080

    status_path = str(Path(exe_path).parent / "update_status.json")
    tmp_path    = exe_path + ".new"
    old_path    = exe_path + ".old"

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    # ── Stage 1: Download (old server still running) ──────────────────────────
    _write_status(status_path, "downloading", 0, "Подключение…")

    try:
        req = urllib.request.Request(
            download_url, headers={"User-Agent": "NbmSearchUpdater/2"}
        )
        with urllib.request.urlopen(req, context=ctx, timeout=120) as r:
            total = int(r.headers.get("Content-Length", 0))
            downloaded = 0
            chunk = 65536

            with open(tmp_path, "wb") as f:
                while True:
                    buf = r.read(chunk)
                    if not buf:
                        break
                    f.write(buf)
                    downloaded += len(buf)
                    if total:
                        pct = int(downloaded / total * 88)
                        mb  = downloaded / 1048576
                        tmb = total / 1048576
                        _write_status(status_path, "downloading", pct,
                                      f"Скачивание: {mb:.1f} / {tmb:.1f} МБ")
                    else:
                        _write_status(status_path, "downloading", 44,
                                      f"Скачивание: {downloaded/1048576:.1f} МБ")

    except Exception as e:
        _write_status(status_path, "error", 0, "", f"Ошибка скачивания: {e}")
        _safe_remove(tmp_path)
        sys.exit(1)

    # ── Stage 2: Stop old process ─────────────────────────────────────────────
    _write_status(status_path, "replacing", 90, "Остановка сервера…")
    _kill_pid(pid, timeout=10)
    time.sleep(0.8)

    # ── Stage 3: Replace file ─────────────────────────────────────────────────
    _write_status(status_path, "replacing", 93, "Замена файла…")
    try:
        _safe_remove(old_path)
        os.rename(exe_path, old_path)
        os.rename(tmp_path, exe_path)
    except Exception as e:
        # Rollback: restore old exe and restart
        _rollback(exe_path, old_path, tmp_path)
        subprocess.Popen([exe_path])
        _write_status(status_path, "error", 0, "",
                      f"Ошибка замены файла: {e}. Восстановлена предыдущая версия.")
        sys.exit(1)

    # ── Stage 4: Launch new exe ───────────────────────────────────────────────
    _write_status(status_path, "restarting", 95, "Запуск новой версии…")
    try:
        subprocess.Popen([exe_path])
    except Exception as e:
        _rollback(exe_path, old_path, tmp_path)
        subprocess.Popen([exe_path])
        _write_status(status_path, "error", 0, "",
                      f"Ошибка запуска: {e}. Восстановлена предыдущая версия.")
        sys.exit(1)

    # ── Stage 5: Verify new server started ───────────────────────────────────
    _write_status(status_path, "restarting", 97, "Ожидание запуска сервера…")
    ok = _verify_started(port, timeout=40)

    if ok:
        _safe_remove(old_path)
        _write_status(status_path, "done", 100, "Обновление завершено успешно")
    else:
        # New server didn't start — rollback
        _write_status(status_path, "restarting", 98, "Новая версия не отвечает, откат…")
        _rollback(exe_path, old_path, exe_path + ".failed")
        subprocess.Popen([exe_path])
        time.sleep(5)
        if _verify_started(port, timeout=20):
            _write_status(status_path, "error", 0, "",
                          "Новая версия не запустилась — восстановлена предыдущая версия")
        else:
            _write_status(status_path, "error", 0, "",
                          "Откат не удался — требуется ручное вмешательство")


def _safe_remove(path: str):
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def _rollback(exe_path: str, old_path: str, failed_path: str):
    try:
        if os.path.exists(exe_path):
            _safe_remove(failed_path)
            os.rename(exe_path, failed_path)
        if os.path.exists(old_path):
            os.rename(old_path, exe_path)
    except Exception:
        pass


if __name__ == "__main__":
    main()
