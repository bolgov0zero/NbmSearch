"""
NbmSearch Windows Service entry point.
Runs uvicorn without GUI — controlled by Windows Service Control Manager.

Started by SCM as: NbmSearch.exe --service
"""
import sys
import asyncio
import logging
from pathlib import Path

# Log to file (no console in service mode)
_log_path = (
    Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
) / "nbmsearch.log"
logging.basicConfig(
    filename=str(_log_path),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

import win32serviceutil
import win32service
import win32event
import servicemanager
import uvicorn

from app.settings import PORT
from app.main import app

SERVICE_NAME    = "NbmSearch"
SERVICE_DISPLAY = "NbmSearch"
SERVICE_DESC    = "NbmSearch — поиск по файлам"


class NbmSearchService(win32serviceutil.ServiceFramework):
    _svc_name_         = SERVICE_NAME
    _svc_display_name_ = SERVICE_DISPLAY
    _svc_description_  = SERVICE_DESC

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self._stop_event = win32event.CreateEvent(None, 0, 0, None)
        self._server: uvicorn.Server | None = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        if self._server:
            self._server.should_exit = True
        win32event.SetEvent(self._stop_event)

    def SvcDoRun(self):
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (SERVICE_NAME, ""),
        )
        try:
            config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_config=None)
            self._server = uvicorn.Server(config)
            asyncio.run(self._server.serve())
        except Exception as e:
            servicemanager.LogErrorMsg(f"NbmSearch service error: {e}")
