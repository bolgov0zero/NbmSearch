"""
NbmSearch launcher — tkinter control window + embedded uvicorn server.
PyInstaller entry point.

Startup modes:
  NbmSearch.exe              — GUI launcher (embedded server or service panel)
  NbmSearch.exe --service    — run as Windows Service (called by SCM)
  NbmSearch.exe install      — install service (must be elevated)
  NbmSearch.exe remove       — remove service (must be elevated)
"""
import sys
import os
import asyncio
import threading
import subprocess
import webbrowser
import ctypes
import time
from pathlib import Path

# Redirect stdout/stderr BEFORE any other imports (windowed PyInstaller mode)
if getattr(sys, "frozen", False):
    _log_path = Path(sys.executable).parent / "nbmsearch.log"
    _log_file = open(_log_path, "a", encoding="utf-8", buffering=1)
    sys.stdout = _log_file
    sys.stderr = _log_file

import tkinter as tk
from tkinter import messagebox
import uvicorn

from app.settings import PORT
from app.main import app
from app import database as db
from app import indexer

SERVICE_NAME = "NbmSearch"

# ── Service helpers ───────────────────────────────────────────────────────────

_NO_WINDOW = subprocess.CREATE_NO_WINDOW  # suppress sc.exe console flash


def _svc_query() -> tuple[bool, str]:
    """Return (installed, state_string). State: RUNNING | STOPPED | START_PENDING | ..."""
    try:
        r = subprocess.run(["sc", "query", SERVICE_NAME],
                           capture_output=True, text=True, timeout=5,
                           creationflags=_NO_WINDOW)
        if r.returncode != 0:
            return False, ""
        out = r.stdout
        # Extract STATE line, e.g. "STATE              : 4  RUNNING"
        for line in out.splitlines():
            if "STATE" in line and ":" in line:
                state = line.split(":", 1)[1].strip()   # "4  RUNNING"
                state = state.split()[-1] if state.split() else ""
                return True, state
        return True, ""
    except Exception:
        return False, ""


def _svc_installed() -> bool:
    installed, _ = _svc_query()
    return installed


def _svc_running() -> bool:
    _, state = _svc_query()
    return state == "RUNNING"


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _run_elevated(arg: str):
    """Re-launch this exe with admin rights, passing arg."""
    exe = str(Path(sys.executable)) if getattr(sys, "frozen", False) else str(Path(sys.argv[0]).resolve())
    ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, arg, None, 1)


# ── Service install / remove (run elevated, sc.exe) ───────────────────────────

def _sc(cmd: str) -> subprocess.CompletedProcess:
    """Run sc.exe command via shell (most reliable quoting for binPath)."""
    return subprocess.run(cmd, shell=True, capture_output=True,
                          text=True, creationflags=_NO_WINDOW, timeout=15)


def _install_service():
    """Called when running elevated with 'install' arg."""
    exe = str(Path(sys.executable)) if getattr(sys, "frozen", False) else str(Path(sys.argv[0]).resolve())

    # sc.exe binPath= with shell=True: inner quotes escaped with \"
    # Result on command line: sc create NbmSearch binPath= "\"C:\path\exe.exe\" --service" ...
    exe_esc = exe.replace('"', '\\"')
    ret = _sc(
        f'sc create {SERVICE_NAME} '
        f'binPath= "\\"{exe_esc}\\" --service" '
        f'DisplayName= "{SERVICE_DISPLAY}" '
        f'start= auto type= own'
    )
    if ret.returncode != 0:
        _alert(f"Ошибка установки службы:\n{ret.stdout or ret.stderr}")
        sys.exit(1)

    _sc(f'sc description {SERVICE_NAME} "{SERVICE_DESC}"')
    _sc(f'sc start {SERVICE_NAME}')


def _remove_service():
    """Called when running elevated with 'remove' arg."""
    _sc(f"sc stop {SERVICE_NAME}")

    # Wait up to 10s for graceful stop
    for _ in range(10):
        _, state = _svc_query()
        if state in ("STOPPED", ""):
            break
        time.sleep(1)
    else:
        # Graceful stop failed — force-kill the service process by image name
        subprocess.run(
            f"taskkill /F /IM {SERVICE_NAME}.exe",
            shell=True, capture_output=True, creationflags=_NO_WINDOW,
        )
        time.sleep(1)

    ret = _sc(f"sc delete {SERVICE_NAME}")
    if ret.returncode != 0:
        _alert(f"Ошибка удаления службы:\n{ret.stdout or ret.stderr}")
        sys.exit(1)


def _alert(msg: str):
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror("NbmSearch", msg)
    root.destroy()


SERVICE_DISPLAY = "NbmSearch"
SERVICE_DESC    = "NbmSearch — поиск по файлам"

# ── Embedded server management ────────────────────────────────────────────────

_server: uvicorn.Server | None = None
_server_thread: threading.Thread | None = None


def _run_server():
    global _server
    config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_config=None)
    _server = uvicorn.Server(config)
    asyncio.run(_server.serve())


def server_start():
    global _server_thread, _server
    if _server_thread and _server_thread.is_alive():
        return
    _server = None
    _server_thread = threading.Thread(target=_run_server, daemon=True)
    _server_thread.start()


def server_stop():
    global _server
    if _server:
        _server.should_exit = True


def server_running() -> bool:
    return bool(_server_thread and _server_thread.is_alive()
                and _server and not _server.should_exit)


# ── Tkinter UI ────────────────────────────────────────────────────────────────

class LauncherWindow:
    BG       = "#0f1117"
    SURFACE  = "#1a1d27"
    SURFACE2 = "#22263a"
    BORDER   = "#2d3148"
    ACCENT   = "#5b6af0"
    TEXT     = "#e8eaf6"
    DIM      = "#8b90b8"
    GREEN    = "#2ecc71"
    RED      = "#e74c3c"
    ORANGE   = "#f39c12"

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("NbmSearch")
        self.root.resizable(False, False)
        self.root.configure(bg=self.BG)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._set_icon()

        w, h = 360, 290
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")

        self._build()
        self._poll_status()

        # Only start embedded server if not running as a service
        if not _svc_installed():
            server_start()

    def _set_icon(self):
        try:
            if getattr(sys, "frozen", False):
                icon_path = Path(sys.executable).parent / "icon.png"
            else:
                icon_path = Path(__file__).parent / "icon.png"
            if icon_path.exists():
                from PIL import Image, ImageTk
                img = Image.open(icon_path).resize((32, 32), Image.LANCZOS)
                self._icon_img = ImageTk.PhotoImage(img)
                self.root.iconphoto(True, self._icon_img)
        except Exception:
            pass

    def _build(self):
        # ── Header bar
        hdr = tk.Frame(self.root, bg=self.SURFACE, height=56)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        try:
            if getattr(sys, "frozen", False):
                icon_path = Path(sys.executable).parent / "icon.png"
            else:
                icon_path = Path(__file__).parent / "icon.png"
            if icon_path.exists():
                from PIL import Image, ImageTk
                img = Image.open(icon_path).resize((28, 28), Image.LANCZOS)
                self._header_icon = ImageTk.PhotoImage(img)
                tk.Label(hdr, image=self._header_icon, bg=self.SURFACE).pack(side="left", padx=(16, 8), pady=14)
            else:
                raise FileNotFoundError
        except Exception:
            box = tk.Frame(hdr, bg=self.ACCENT, width=28, height=28)
            box.pack(side="left", padx=(16, 8), pady=14)
            box.pack_propagate(False)
            tk.Label(box, text="⌕", bg=self.ACCENT, fg="white", font=("Segoe UI", 12)).place(relx=.5, rely=.5, anchor="center")

        tk.Label(hdr, text="NbmSearch", bg=self.SURFACE, fg=self.TEXT,
                 font=("Segoe UI", 13, "bold")).pack(side="left", pady=14)

        # ── Body
        body = tk.Frame(self.root, bg=self.BG)
        body.pack(fill="both", expand=True, padx=22, pady=18)

        def row(label_text):
            f = tk.Frame(body, bg=self.BG)
            f.pack(fill="x", pady=4)
            tk.Label(f, text=label_text, bg=self.BG, fg=self.DIM,
                     font=("Segoe UI", 9), width=9, anchor="w").pack(side="left")
            return f

        # Status
        r1 = row("Статус")
        self._dot = tk.Label(r1, text="●", bg=self.BG, font=("Segoe UI", 12))
        self._dot.pack(side="left", padx=(0, 5))
        self._status_lbl = tk.Label(r1, bg=self.BG, fg=self.TEXT, font=("Segoe UI", 9, "bold"))
        self._status_lbl.pack(side="left")

        # Port
        r2 = row("Порт")
        tk.Label(r2, text=str(PORT), bg=self.BG, fg=self.TEXT,
                 font=("Segoe UI", 9, "bold")).pack(side="left")

        # URL
        r3 = row("Адрес")
        url = f"http://localhost:{PORT}"
        lnk = tk.Label(r3, text=url, bg=self.BG, fg=self.ACCENT,
                        font=("Segoe UI", 9, "underline"), cursor="hand2")
        lnk.pack(side="left")
        lnk.bind("<Button-1>", lambda e: webbrowser.open(url))

        # Divider
        tk.Frame(body, bg=self.BORDER, height=1).pack(fill="x", pady=(14, 10))

        # ── Main buttons
        btn_frame = tk.Frame(body, bg=self.BG)
        btn_frame.pack(fill="x")

        def make_btn(parent, text, bg, fg, cmd, hover_bg=None):
            b = tk.Button(parent, text=text, bg=bg, fg=fg,
                          activebackground=hover_bg or bg, activeforeground=fg,
                          command=cmd, relief="flat", bd=0,
                          font=("Segoe UI", 9, "bold"), padx=14, pady=7, cursor="hand2")
            return b

        self._btn_stop  = make_btn(btn_frame, "Стоп",    self.SURFACE2, self.TEXT, self._do_stop,  "#3d3355")
        self._btn_start = make_btn(btn_frame, "Старт",   self.ACCENT,   "white",   self._do_start, "#3d4ab0")
        btn_open        = make_btn(btn_frame, "Открыть", self.SURFACE,  self.DIM,  lambda: webbrowser.open(f"http://localhost:{PORT}"), self.SURFACE2)

        self._btn_stop.pack(side="left", padx=(0, 6))
        self._btn_start.pack(side="left", padx=(0, 6))
        btn_open.pack(side="left")

        # ── Service divider + button
        tk.Frame(body, bg=self.BORDER, height=1).pack(fill="x", pady=(12, 8))

        svc_frame = tk.Frame(body, bg=self.BG)
        svc_frame.pack(fill="x")

        self._btn_svc = tk.Button(
            svc_frame, text="Установить службу",
            bg=self.SURFACE2, fg=self.DIM,
            activebackground="#2a2d40", activeforeground=self.TEXT,
            command=self._do_svc_toggle,
            relief="flat", bd=0,
            font=("Segoe UI", 9), padx=14, pady=6, cursor="hand2",
        )
        self._btn_svc.pack(fill="x")

    # ── Button handlers ───────────────────────────────────────────────────────

    def _do_start(self):
        if _svc_installed():
            _sc(f"sc start {SERVICE_NAME}")
        else:
            server_start()
        self._set_status(self.ORANGE, "Запускается…")
        self.root.after(1500, self._poll_status)

    def _do_stop(self):
        if _svc_installed():
            _sc(f"sc stop {SERVICE_NAME}")
        else:
            server_stop()
        self._set_status(self.ORANGE, "Останавливается…")
        self.root.after(1500, self._poll_status)

    def _do_svc_toggle(self):
        if _svc_installed():
            self._do_remove_service()
        else:
            self._do_install_service()

    def _do_install_service(self):
        # Stop embedded server to free the port before service starts
        server_stop()
        self._set_status(self.ORANGE, "Установка службы…")
        self._btn_svc.config(state="disabled")
        # Re-launch elevated to install
        _run_elevated("install")
        # Poll a few times to detect the service appearing
        for delay in (3000, 5000, 8000):
            self.root.after(delay, self._poll_status)

    def _do_remove_service(self):
        self._set_status(self.ORANGE, "Удаление службы…")
        self._btn_svc.config(state="disabled")
        _run_elevated("remove")
        # After removal restart embedded server and poll
        self.root.after(4000, self._after_remove)

    def _after_remove(self):
        if not _svc_installed():
            server_start()
        self._poll_status()

    # ── Status polling ────────────────────────────────────────────────────────

    def _set_status(self, color, text):
        self._dot.config(fg=color)
        self._status_lbl.config(text=text)

    def _poll_status(self):
        installed, state = _svc_query()

        if installed:
            # Service mode: reflect service state
            running = (state == "RUNNING")
            pending = state in ("START_PENDING", "STOP_PENDING")

            if running:
                self._set_status(self.GREEN, "Запущен (служба)")
            elif pending:
                self._set_status(self.ORANGE, "Переходное состояние…")
            else:
                self._set_status(self.RED, "Остановлен (служба)")

            self._btn_start.config(state="disabled" if (running or pending) else "normal",
                                   bg=self.SURFACE2 if (running or pending) else self.ACCENT,
                                   fg=self.DIM if (running or pending) else "white")
            self._btn_stop.config(state="normal" if running else "disabled",
                                  bg=self.SURFACE2, fg=self.TEXT if running else self.DIM)
            self._btn_svc.config(text="Удалить службу", state="normal",
                                 fg=self.RED, bg=self.SURFACE2)
        else:
            # Embedded server mode
            running = server_running()
            if running:
                self._set_status(self.GREEN, "Запущен")
            else:
                self._set_status(self.RED, "Остановлен")

            self._btn_start.config(state="disabled" if running else "normal",
                                   bg=self.SURFACE2 if running else self.ACCENT,
                                   fg=self.DIM if running else "white")
            self._btn_stop.config(state="normal" if running else "disabled",
                                  bg=self.SURFACE2, fg=self.TEXT if running else self.DIM)
            self._btn_svc.config(text="Установить службу", state="normal",
                                 fg=self.DIM, bg=self.SURFACE2)

        self.root.after(2000, self._poll_status)

    def _on_close(self):
        # Only stop embedded server — service continues running without the window
        if not _svc_installed():
            server_stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""

    if arg == "--service":
        # Called by SCM: run as Windows Service (pure ctypes, no pywin32)
        from service import run_service
        run_service()

    elif arg == "install":
        # Running elevated: install and start the service
        _install_service()

    elif arg == "remove":
        # Running elevated: stop and remove the service
        _remove_service()

    else:
        # Normal GUI launch
        LauncherWindow().run()
