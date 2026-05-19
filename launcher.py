"""
NbmSearch launcher — tkinter control window + embedded uvicorn server.
This is the PyInstaller entry point.
"""
import sys
import os
import asyncio
import threading
import time
import webbrowser
from pathlib import Path

# Redirect stdout/stderr BEFORE any other imports that use logging
if getattr(sys, "frozen", False):
    _log_path = Path(sys.executable).parent / "nbmsearch.log"
    _log_file = open(_log_path, "a", encoding="utf-8", buffering=1)
    sys.stdout = _log_file
    sys.stderr = _log_file

import tkinter as tk
from tkinter import font as tkfont
import uvicorn

from app.config import PORT
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
    BG        = "#0f1117"
    SURFACE   = "#1a1d27"
    BORDER    = "#2d3148"
    ACCENT    = "#5b6af0"
    TEXT      = "#e8eaf6"
    TEXT_DIM  = "#8b90b8"
    GREEN     = "#2ecc71"
    RED       = "#e74c3c"
    ORANGE    = "#f39c12"

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("NbmSearch")
        self.root.resizable(False, False)
        self.root.configure(bg=self.BG)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Center on screen
        w, h = 340, 220
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")

        self._build_ui()
        self._update_status()

        # Auto-start server
        server_start()

    def _build_ui(self):
        pad = dict(padx=20)

        # ── Header
        hdr = tk.Frame(self.root, bg=self.SURFACE, height=52)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        logo_box = tk.Frame(hdr, bg=self.ACCENT, width=28, height=28)
        logo_box.pack(side="left", padx=(16, 8), pady=12)
        logo_box.pack_propagate(False)
        tk.Label(logo_box, text="⌕", bg=self.ACCENT, fg="white",
                 font=("Segoe UI", 12)).place(relx=.5, rely=.5, anchor="center")

        tk.Label(hdr, text="NbmSearch", bg=self.SURFACE, fg=self.TEXT,
                 font=("Segoe UI", 13, "bold")).pack(side="left", pady=12)

        # ── Body
        body = tk.Frame(self.root, bg=self.BG)
        body.pack(fill="both", expand=True, padx=20, pady=16)

        # Status row
        row1 = tk.Frame(body, bg=self.BG)
        row1.pack(fill="x", pady=(0, 8))
        tk.Label(row1, text="Статус", bg=self.BG, fg=self.TEXT_DIM,
                 font=("Segoe UI", 9), width=8, anchor="w").pack(side="left")
        self._status_dot = tk.Label(row1, text="●", bg=self.BG,
                                    font=("Segoe UI", 11))
        self._status_dot.pack(side="left", padx=(0, 6))
        self._status_label = tk.Label(row1, bg=self.BG, fg=self.TEXT,
                                      font=("Segoe UI", 9))
        self._status_label.pack(side="left")

        # Port row
        row2 = tk.Frame(body, bg=self.BG)
        row2.pack(fill="x", pady=(0, 8))
        tk.Label(row2, text="Порт", bg=self.BG, fg=self.TEXT_DIM,
                 font=("Segoe UI", 9), width=8, anchor="w").pack(side="left")
        tk.Label(row2, text=str(PORT), bg=self.BG, fg=self.TEXT,
                 font=("Segoe UI", 9, "bold")).pack(side="left")

        # URL row
        row3 = tk.Frame(body, bg=self.BG)
        row3.pack(fill="x", pady=(0, 16))
        tk.Label(row3, text="Адрес", bg=self.BG, fg=self.TEXT_DIM,
                 font=("Segoe UI", 9), width=8, anchor="w").pack(side="left")
        url_lbl = tk.Label(row3, text=f"http://localhost:{PORT}",
                           bg=self.BG, fg=self.ACCENT,
                           font=("Segoe UI", 9, "underline"), cursor="hand2")
        url_lbl.pack(side="left")
        url_lbl.bind("<Button-1>", lambda e: webbrowser.open(f"http://localhost:{PORT}"))

        # ── Buttons
        btn_frame = tk.Frame(body, bg=self.BG)
        btn_frame.pack(fill="x")

        btn_cfg = dict(font=("Segoe UI", 9, "bold"), relief="flat",
                       bd=0, padx=14, pady=7, cursor="hand2")

        self._btn_stop = tk.Button(
            btn_frame, text="Стоп", bg="#2d3148", fg=self.TEXT,
            activebackground=self.RED, activeforeground="white",
            command=self._do_stop, **btn_cfg)
        self._btn_stop.pack(side="left", padx=(0, 8))

        self._btn_start = tk.Button(
            btn_frame, text="Старт", bg=self.ACCENT, fg="white",
            activebackground="#3d4ab0", activeforeground="white",
            command=self._do_start, **btn_cfg)
        self._btn_start.pack(side="left", padx=(0, 8))

        tk.Button(
            btn_frame, text="Открыть в браузере",
            bg=self.SURFACE, fg=self.TEXT_DIM,
            activebackground="#22263a", activeforeground=self.TEXT,
            command=lambda: webbrowser.open(f"http://localhost:{PORT}"),
            **btn_cfg).pack(side="left")

    def _do_start(self):
        server_start()
        self._status_dot.config(fg=self.ORANGE)
        self._status_label.config(text="Запускается…")
        self.root.after(1500, self._update_status)

    def _do_stop(self):
        server_stop()
        self._status_dot.config(fg=self.ORANGE)
        self._status_label.config(text="Останавливается…")
        self.root.after(1500, self._update_status)

    def _update_status(self):
        if server_running():
            self._status_dot.config(fg=self.GREEN)
            self._status_label.config(text="Запущен")
            self._btn_start.config(state="disabled", bg="#22263a", fg=self.TEXT_DIM)
            self._btn_stop.config(state="normal", bg="#2d3148", fg=self.TEXT)
        else:
            self._status_dot.config(fg=self.RED)
            self._status_label.config(text="Остановлен")
            self._btn_start.config(state="normal", bg=self.ACCENT, fg="white")
            self._btn_stop.config(state="disabled", bg="#1a1d27", fg=self.TEXT_DIM)
        self.root.after(2000, self._update_status)

    def _on_close(self):
        server_stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    LauncherWindow().run()
