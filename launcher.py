"""
NbmSearch launcher — tkinter control window + embedded uvicorn server.
PyInstaller entry point.
"""
import sys
import os
import asyncio
import threading
import webbrowser
from pathlib import Path

# Redirect stdout/stderr BEFORE any other imports (windowed PyInstaller mode)
if getattr(sys, "frozen", False):
    _log_path = Path(sys.executable).parent / "nbmsearch.log"
    _log_file = open(_log_path, "a", encoding="utf-8", buffering=1)
    sys.stdout = _log_file
    sys.stderr = _log_file

import tkinter as tk
from tkinter import ttk
import uvicorn

from app.settings import PORT
from app.main import app
from app import database as db
from app import indexer

# ── Server management ─────────────────────────────────────────────────────────

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

        # Set window icon
        self._set_icon()

        w, h = 360, 240
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")

        self._build()
        self._poll_status()
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
            pass  # PIL not available — no icon, that's fine

    def _build(self):
        # ── Header bar
        hdr = tk.Frame(self.root, bg=self.SURFACE, height=56)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        # Logo icon
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

        # ── Buttons
        btn_frame = tk.Frame(body, bg=self.BG)
        btn_frame.pack(fill="x")

        def make_btn(parent, text, bg, fg, cmd, hover_bg=None):
            b = tk.Button(parent, text=text, bg=bg, fg=fg, activebackground=hover_bg or bg,
                          activeforeground=fg, command=cmd, relief="flat", bd=0,
                          font=("Segoe UI", 9, "bold"), padx=14, pady=7, cursor="hand2")
            return b

        self._btn_stop  = make_btn(btn_frame, "Стоп",  self.SURFACE2, self.TEXT, self._do_stop,  "#3d3355")
        self._btn_start = make_btn(btn_frame, "Старт", self.ACCENT,   "white",   self._do_start, "#3d4ab0")
        btn_open        = make_btn(btn_frame, "Открыть", self.SURFACE, self.DIM, lambda: webbrowser.open(f"http://localhost:{PORT}"), self.SURFACE2)

        self._btn_stop.pack(side="left", padx=(0, 6))
        self._btn_start.pack(side="left", padx=(0, 6))
        btn_open.pack(side="left")

    def _do_start(self):
        server_start()
        self._set_status(self.ORANGE, "Запускается…")
        self.root.after(1500, self._poll_status)

    def _do_stop(self):
        server_stop()
        self._set_status(self.ORANGE, "Останавливается…")
        self.root.after(1500, self._poll_status)

    def _set_status(self, color, text):
        self._dot.config(fg=color)
        self._status_lbl.config(text=text)

    def _poll_status(self):
        if server_running():
            self._set_status(self.GREEN, "Запущен")
            self._btn_start.config(state="disabled", bg=self.SURFACE2, fg=self.DIM)
            self._btn_stop.config(state="normal", bg=self.SURFACE2, fg=self.TEXT)
        else:
            self._set_status(self.RED, "Остановлен")
            self._btn_start.config(state="normal", bg=self.ACCENT, fg="white")
            self._btn_stop.config(state="disabled", bg="#1a1d27", fg=self.DIM)
        self.root.after(2000, self._poll_status)

    def _on_close(self):
        server_stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    LauncherWindow().run()
