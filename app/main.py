import sys
import os
import threading
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import List, Optional

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys._MEIPASS)
else:
    BASE_DIR = Path(__file__).resolve().parent.parent

from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import uvicorn

from app.config import PORT
from app import database as db
from app import indexer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI):
    db.init_db()
    threading.Thread(target=_initial_and_scheduler, daemon=True).start()
    try:
        indexer.restart_watchdog()
    except Exception as e:
        logger.error("Watchdog failed to start: %s", e)
    yield


def _initial_and_scheduler():
    indexer.full_reindex()
    indexer.reindex_scheduler()


app = FastAPI(title="NbmSearch", lifespan=lifespan)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# ── Search ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    folders = db.get_folders()
    return templates.TemplateResponse("index.html", {"request": request, "folders": folders})


@app.get("/search")
async def search(
    q: str = "",
    folders: Optional[List[str]] = Query(default=None),
):
    if not q.strip():
        return {"results": []}
    try:
        results = db.search(q.strip(), folder_names=folders or None)
    except Exception as e:
        logger.error("Search error: %s", e)
        return {"results": [], "error": str(e)}
    return {"results": results}


# ── Admin ─────────────────────────────────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request):
    count, by_folder, recent = db.stats()
    last_ts = indexer.last_reindex_time
    next_ts = indexer.next_reindex_time
    last_str = datetime.fromtimestamp(last_ts).strftime("%Y-%m-%d %H:%M:%S") if last_ts else "—"
    next_str = datetime.fromtimestamp(next_ts).strftime("%Y-%m-%d %H:%M:%S") if next_ts else "—"
    for r in recent:
        r["indexed_at_str"] = datetime.fromtimestamp(r["indexed_at"]).strftime("%Y-%m-%d %H:%M:%S")
    folders = db.get_folders()
    return templates.TemplateResponse("admin.html", {
        "request": request,
        "count": count,
        "by_folder": by_folder,
        "last_reindex": last_str,
        "next_reindex": next_str,
        "recent": recent,
        "folders": folders,
    })


@app.get("/admin/stats")
async def admin_stats():
    count, by_folder, _ = db.stats()
    last_ts = indexer.last_reindex_time
    next_ts = indexer.next_reindex_time
    return {
        "count": count,
        "by_folder": by_folder,
        "last_reindex": datetime.fromtimestamp(last_ts).strftime("%Y-%m-%d %H:%M:%S") if last_ts else "—",
        "next_reindex": datetime.fromtimestamp(next_ts).strftime("%Y-%m-%d %H:%M:%S") if next_ts else "—",
    }


@app.post("/admin/reindex")
async def trigger_reindex():
    threading.Thread(target=indexer.full_reindex, daemon=True).start()
    return {"status": "started"}


# ── Folders API ───────────────────────────────────────────────────────────────

class FolderIn(BaseModel):
    name: str
    path: str


@app.get("/api/folders")
async def api_get_folders():
    return db.get_folders()


@app.post("/api/folders")
async def api_add_folder(data: FolderIn):
    name = data.name.strip()
    path = data.path.strip()
    if not name or not path:
        return JSONResponse({"error": "Имя и путь обязательны"}, status_code=400)
    if not os.path.isdir(path):
        return JSONResponse({"error": f"Папка не найдена: {path}"}, status_code=400)
    try:
        folder = db.add_folder(name, path)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    def _init():
        indexer.index_folder({"name": name, "path": path})
        indexer.restart_watchdog()

    threading.Thread(target=_init, daemon=True).start()
    return folder


@app.get("/open")
async def open_file(path: str = ""):
    """Open a file with the default OS application.

    Uses 'start' shell command — more reliable than os.startfile for UNC paths
    (\\\\server\\share\\...) because it doesn't require os.path.exists to work.
    """
    if not path:
        return JSONResponse({"error": "path required"}, status_code=400)
    try:
        import subprocess
        # 'start "" "path"' — first arg is window title (empty), second is path.
        # shell=True required for the start builtin; path is quoted to handle spaces.
        subprocess.Popen(
            f'start "" "{path}"',
            shell=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return {"status": "ok"}
    except Exception as e:
        logger.error("open_file error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Client setup ──────────────────────────────────────────────────────────────

@app.get("/client/NbmSearchOpen.exe")
async def client_exe(request: Request):
    if getattr(sys, "frozen", False):
        exe_path = Path(sys.executable).parent / "NbmSearchOpen.exe"
    else:
        exe_path = BASE_DIR / "NbmSearchOpen.exe"
    if not exe_path.exists():
        return JSONResponse({"error": "NbmSearchOpen.exe not found on server"}, status_code=404)
    return FileResponse(str(exe_path), media_type="application/octet-stream", filename="NbmSearchOpen.exe")


@app.get("/client/setup.ps1")
async def client_setup(request: Request):
    host = request.headers.get("host", f"localhost:{PORT}")
    scheme = "https" if request.headers.get("x-forwarded-proto") == "https" else "http"
    server_url = f"{scheme}://{host}"
    script = f"""# NbmSearch — установка клиентского обработчика протокола nbmsearch://
# Запустите этот скрипт один раз на каждом клиентском компьютере.
# Требуются права администратора для записи в реестр.

$ErrorActionPreference = "Stop"
$serverUrl = "{server_url}"
$exeName   = "NbmSearchOpen.exe"
$installDir = "$env:LOCALAPPDATA\\NbmSearch"
$exePath   = "$installDir\\$exeName"

Write-Host "Создаю папку $installDir..."
New-Item -ItemType Directory -Force -Path $installDir | Out-Null

Write-Host "Загружаю $exeName с $serverUrl/client/$exeName ..."
Invoke-WebRequest -Uri "$serverUrl/client/$exeName" -OutFile $exePath -UseBasicParsing

Write-Host "Регистрирую протокол nbmsearch:// в реестре..."
$regBase = "HKCU:\\Software\\Classes\\nbmsearch"
New-Item -Path $regBase -Force | Out-Null
Set-ItemProperty -Path $regBase -Name "(Default)"     -Value "URL:NbmSearch Protocol"
Set-ItemProperty -Path $regBase -Name "URL Protocol"  -Value ""
New-Item -Path "$regBase\\shell\\open\\command" -Force | Out-Null
Set-ItemProperty -Path "$regBase\\shell\\open\\command" -Name "(Default)" -Value "`"$exePath`" `"%1`""

Write-Host "Отключаю диалог подтверждения в Chrome и Edge..."
# Chrome
$chromePol = "HKLM:\\SOFTWARE\\Policies\\Google\\Chrome\\AutoOpenProtocolHandlerAllowlist"
try {{
    New-Item -Path $chromePol -Force | Out-Null
    $idx = (Get-Item $chromePol -ErrorAction SilentlyContinue).Property.Count + 1
    Set-ItemProperty -Path $chromePol -Name "$idx" -Value "nbmsearch"
}} catch {{
    Write-Host "  Chrome: нет прав на запись политики (нужен администратор)" -ForegroundColor Yellow
}}
# Edge
$edgePol = "HKLM:\\SOFTWARE\\Policies\\Microsoft\\Edge\\AutoOpenProtocolHandlerAllowlist"
try {{
    New-Item -Path $edgePol -Force | Out-Null
    $idx = (Get-Item $edgePol -ErrorAction SilentlyContinue).Property.Count + 1
    Set-ItemProperty -Path $edgePol -Name "$idx" -Value "nbmsearch"
}} catch {{
    Write-Host "  Edge: нет прав на запись политики (нужен администратор)" -ForegroundColor Yellow
}}

Write-Host ""
Write-Host "Готово! Протокол nbmsearch:// зарегистрирован." -ForegroundColor Green
Write-Host "Теперь ссылки 'Открыть файл' и 'Открыть папку' в NbmSearch будут работать на этом компьютере."
Write-Host "Если диалог подтверждения всё ещё появляется — перезапустите браузер."
"""
    return PlainTextResponse(script, media_type="text/plain; charset=utf-8")


@app.delete("/api/folders/{folder_id}")
async def api_delete_folder(folder_id: int):
    folder_name = db.delete_folder(folder_id)
    if folder_name is None:
        return JSONResponse({"error": "Папка не найдена"}, status_code=404)
    db.delete_files_by_folder(folder_name)
    threading.Thread(target=indexer.restart_watchdog, daemon=True).start()
    return {"status": "deleted", "name": folder_name}


if __name__ == "__main__":
    uvicorn.run(
        app,          # передаём объект напрямую — строка "app.main:app" не работает в PyInstaller
        host="0.0.0.0",
        port=PORT,
        reload=False,
        log_config=None,
    )
