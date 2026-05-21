"""
NbmSearch Updater
Waits for NbmSearch.exe to exit, downloads a new version, replaces the exe, and restarts it.

Usage: NbmSearchUpdater.exe <pid> <download_url> <exe_path>
"""
import sys
import os
import time
import urllib.request
import subprocess


def _wait_for_pid(pid: int, timeout: int = 30) -> None:
    """Wait for a Windows process to exit (up to timeout seconds)."""
    try:
        import ctypes
        SYNCHRONIZE = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if handle:
            ctypes.windll.kernel32.WaitForSingleObject(handle, timeout * 1000)
            ctypes.windll.kernel32.CloseHandle(handle)
            return
    except Exception:
        pass
    # Fallback: poll
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
            time.sleep(0.5)
        except OSError:
            return


def main() -> None:
    if len(sys.argv) < 4:
        sys.exit(1)

    pid          = int(sys.argv[1])
    download_url = sys.argv[2]
    exe_path     = sys.argv[3]

    # 1. Wait for NbmSearch.exe to exit
    _wait_for_pid(pid, timeout=30)
    time.sleep(1)  # extra safety margin

    # 2. Download new exe next to target
    tmp_path = exe_path + ".new"
    try:
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(download_url, context=ctx, timeout=120) as r:
            with open(tmp_path, "wb") as f:
                f.write(r.read())
    except Exception:
        sys.exit(1)

    # 3. Replace: rename old → .old, new → target
    old_path = exe_path + ".old"
    try:
        if os.path.exists(old_path):
            os.remove(old_path)
        os.rename(exe_path, old_path)
        os.rename(tmp_path, exe_path)
        try:
            os.remove(old_path)
        except Exception:
            pass
    except Exception:
        # Rollback: restore old exe
        try:
            if os.path.exists(old_path) and not os.path.exists(exe_path):
                os.rename(old_path, exe_path)
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        sys.exit(1)

    # 4. Launch updated exe
    try:
        subprocess.Popen([exe_path])
    except Exception:
        pass


if __name__ == "__main__":
    main()
