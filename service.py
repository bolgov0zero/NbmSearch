"""
NbmSearch Windows Service — pure ctypes, no pywin32 required.
Entry point: NbmSearch.exe --service  (called by SCM)
"""
import sys
import ctypes
import ctypes.wintypes as wt
import asyncio
import logging
from pathlib import Path

# Log to file (no console in service mode)
_log_path = (
    Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
) / "nbmsearch.log"
logging.basicConfig(
    filename=str(_log_path), level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

# ── Win32 constants ───────────────────────────────────────────────────────────
_SVC_WIN32_OWN  = 0x10
_SVC_STOPPED    = 1
_SVC_START_PEND = 2
_SVC_STOP_PEND  = 3
_SVC_RUNNING    = 4
_CTRL_STOP      = 1
_ACCEPT_STOP    = 1

SERVICE_NAME = "NbmSearch"


# ── Win32 structures & prototypes ─────────────────────────────────────────────
class _SVC_STATUS(ctypes.Structure):
    _fields_ = [
        ("dwServiceType",             wt.DWORD),
        ("dwCurrentState",            wt.DWORD),
        ("dwControlsAccepted",        wt.DWORD),
        ("dwWin32ExitCode",           wt.DWORD),
        ("dwServiceSpecificExitCode", wt.DWORD),
        ("dwCheckPoint",              wt.DWORD),
        ("dwWaitHint",                wt.DWORD),
    ]

_HandlerProc     = ctypes.WINFUNCTYPE(None, wt.DWORD)
_ServiceMainProc = ctypes.WINFUNCTYPE(None, wt.DWORD, ctypes.POINTER(ctypes.c_wchar_p))

class _SVC_TABLE_ENTRY(ctypes.Structure):
    _fields_ = [
        ("lpServiceName", ctypes.c_wchar_p),
        ("lpServiceProc", _ServiceMainProc),
    ]


# ── Service state (module-level so callbacks can access it) ───────────────────
_status_handle = None
_status        = _SVC_STATUS()
_server        = None

# Hold ctypes callbacks alive to prevent garbage collection
_cb_handler  = None
_cb_svc_main = None


def _report(state: int, controls: int = 0, wait_hint: int = 0):
    _status.dwServiceType             = _SVC_WIN32_OWN
    _status.dwCurrentState            = state
    _status.dwControlsAccepted        = controls
    _status.dwWin32ExitCode           = 0
    _status.dwServiceSpecificExitCode = 0
    _status.dwCheckPoint              = 0
    _status.dwWaitHint                = wait_hint
    if _status_handle:
        _advapi32.SetServiceStatus(_status_handle, ctypes.byref(_status))


def _handler(control: int):
    if control == _CTRL_STOP:
        _report(_SVC_STOP_PEND, wait_hint=10000)
        if _server:
            _server.should_exit = True


def _svc_main(argc: int, argv):
    global _status_handle, _cb_handler, _server

    _cb_handler    = _HandlerProc(_handler)
    _status_handle = _advapi32.RegisterServiceCtrlHandlerW(SERVICE_NAME, _cb_handler)
    if not _status_handle:
        logging.error("RegisterServiceCtrlHandlerW failed: %d", ctypes.get_last_error())
        return

    _report(_SVC_START_PEND, wait_hint=30000)
    logging.info("Service starting")

    try:
        import os

        # Services run in C:\Windows\System32 by default — switch to exe dir
        # so relative imports (app.*) and data files are found correctly.
        exe_dir = str(Path(sys.executable).parent) if getattr(sys, "frozen", False) \
                  else str(Path(__file__).resolve().parent)
        os.chdir(exe_dir)
        if exe_dir not in sys.path:
            sys.path.insert(0, exe_dir)

        from app.settings import PORT
        from app.main import app
        import uvicorn

        _report(_SVC_RUNNING, controls=_ACCEPT_STOP)
        logging.info("Service running on port %d", PORT)

        config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_config=None)
        _server = uvicorn.Server(config)
        asyncio.run(_server.serve())

    except Exception:
        logging.exception("Service error")
    finally:
        _report(_SVC_STOPPED)
        logging.info("Service stopped")


def run_service():
    """Dispatch to SCM. Blocks until service stops. Called with --service arg."""
    global _cb_svc_main

    _cb_svc_main = _ServiceMainProc(_svc_main)
    table = (_SVC_TABLE_ENTRY * 2)(
        _SVC_TABLE_ENTRY(SERVICE_NAME, _cb_svc_main),
        _SVC_TABLE_ENTRY(None, ctypes.cast(None, _ServiceMainProc)),
    )
    if not _advapi32.StartServiceCtrlDispatcherW(table):
        logging.error("StartServiceCtrlDispatcherW failed: %d", ctypes.get_last_error())
