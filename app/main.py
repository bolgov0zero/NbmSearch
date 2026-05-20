import sys
import os
import threading
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import List, Optional

if getattr(sys, "frozen", False):
    # _MEIPASS — временная папка куда PyInstaller распаковывает бандл (шаблоны, иконка)
    BUNDLE_DIR = Path(sys._MEIPASS)
    # рядом с exe — пользовательские данные (БД, настройки)
    BASE_DIR = Path(sys.executable).parent
else:
    BUNDLE_DIR = Path(__file__).resolve().parent.parent
    BASE_DIR = BUNDLE_DIR

from fastapi import FastAPI, Request, Query, Response, Cookie
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import uvicorn

from app.settings import PORT, verify_password
from app import database as db
from app import indexer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI):
    db.init_db()
    threading.Thread(target=_startup, daemon=True).start()
    try:
        indexer.restart_watchdog()
    except Exception as e:
        logger.error("Watchdog failed: %s", e)
    yield


def _startup():
    indexer.full_reindex()
    indexer.reindex_scheduler()


app = FastAPI(title="NbmSearch", lifespan=lifespan)
templates = Jinja2Templates(directory=str(BUNDLE_DIR / "templates"))

_SCHED_LABELS = {30:"каждые 30 мин",60:"каждый час",180:"каждые 3 часа",
                 360:"каждые 6 часов",720:"каждые 12 часов",1440:"раз в сутки"}
templates.env.globals["schedLabel"] = lambda m: _SCHED_LABELS.get(m, f"{m} мин")


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _is_auth(request: Request) -> bool:
    token = request.cookies.get("nbm_session")
    return db.session_valid(token)


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


# ── Admin auth ────────────────────────────────────────────────────────────────

@app.get("/admin/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if _is_auth(request):
        return RedirectResponse("/admin")
    return templates.TemplateResponse("login.html", {"request": request, "error": ""})


@app.post("/admin/login")
async def do_login(request: Request):
    form = await request.form()
    password = form.get("password", "")
    if verify_password(password):
        token = db.create_session()
        response = RedirectResponse("/admin", status_code=303)
        response.set_cookie("nbm_session", token, httponly=True, samesite="lax", max_age=86400 * 30)
        return response
    return templates.TemplateResponse("login.html", {"request": request, "error": "Неверный пароль"})


@app.post("/admin/logout")
async def do_logout(request: Request):
    token = request.cookies.get("nbm_session")
    if token:
        db.delete_session(token)
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie("nbm_session")
    return response


# ── Admin ─────────────────────────────────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request):
    if not _is_auth(request):
        return RedirectResponse("/admin/login")
    count, by_folder = db.stats()
    folders = db.get_folders()
    counts_map = {b["folder_name"]: b["cnt"] for b in by_folder}
    for f in folders:
        f["file_count"] = counts_map.get(f["name"], 0)
        f["progress"] = indexer.get_progress(f["id"])
    schedules = db.get_schedules()
    for s in schedules:
        s["last_run_str"] = datetime.fromtimestamp(s["last_run_at"]).strftime("%d.%m.%Y %H:%M") if s.get("last_run_at") else "—"
        s["next_run_str"] = datetime.fromtimestamp(s["next_run_at"]).strftime("%d.%m.%Y %H:%M") if s.get("next_run_at") else "—"
    host = request.headers.get("host", f"localhost:{PORT}")
    scheme = "https" if request.headers.get("x-forwarded-proto") == "https" else "http"
    server_url = f"{scheme}://{host}"
    setup_script = f"""#Requires -RunAsAdministrator
$ErrorActionPreference = "Stop"
$exe = "$env:ProgramData\\NbmSearch\\NbmSearchOpen.exe"

New-Item -ItemType Directory -Force -Path (Split-Path $exe) | Out-Null
Invoke-WebRequest -Uri "{server_url}/client/NbmSearchOpen.exe" -OutFile $exe -UseBasicParsing

$reg = "HKLM:\\Software\\Classes\\nbmsearch"
New-Item -Path $reg -Force | Out-Null
Set-ItemProperty -Path $reg -Name "(Default)" -Value "URL:NbmSearch Protocol"
Set-ItemProperty -Path $reg -Name "URL Protocol" -Value ""
New-Item -Path "$reg\\shell\\open\\command" -Force | Out-Null
Set-ItemProperty -Path "$reg\\shell\\open\\command" -Name "(Default)" -Value "`"$exe`" `"%1`""

foreach ($p in @(
    "HKLM:\\SOFTWARE\\Policies\\Google\\Chrome\\AutoOpenProtocolHandlerAllowlist",
    "HKLM:\\SOFTWARE\\Policies\\Microsoft\\Edge\\AutoOpenProtocolHandlerAllowlist"
)) {{
    try {{
        New-Item -Path $p -Force | Out-Null
        $i = ((Get-Item $p).Property.Count + 1).ToString()
        Set-ItemProperty -Path $p -Name $i -Value "nbmsearch"
    }} catch {{}}
}}

try {{
    New-Item -Path "HKLM:\\SOFTWARE\\Policies\\YandexBrowser" -Force | Out-Null
    Set-ItemProperty -Path "HKLM:\\SOFTWARE\\Policies\\YandexBrowser" `
        -Name "AutoLaunchProtocolsFromOrigins" `
        -Value '[{{"protocol":"nbmsearch","allowed_origins":["*"]}}]'
}} catch {{}}

Write-Host "Done! Restart your browser." -ForegroundColor Green"""
    return templates.TemplateResponse("admin.html", {
        "request": request,
        "count": count,
        "folders": folders,
        "schedules": schedules,
        "setup_script": setup_script,
    })


@app.get("/admin/stats")
async def admin_stats(request: Request):
    if not _is_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    count, by_folder = db.stats()
    folders = db.get_folders()
    counts_map = {b["folder_name"]: b["cnt"] for b in by_folder}
    result = []
    for f in folders:
        prog = indexer.get_progress(f["id"])
        nxt = indexer.get_next_reindex(f["id"])
        lrt = f.get("last_reindex_at")
        result.append({
            "id": f["id"],
            "name": f["name"],
            "file_count": counts_map.get(f["name"], 0),
            "progress": indexer.get_progress(f["id"]),
            "next_reindex": datetime.fromtimestamp(nxt).strftime("%d.%m.%Y %H:%M") if nxt else "—",
            "last_reindex": datetime.fromtimestamp(lrt).strftime("%d.%m.%Y %H:%M") if lrt else "—",
        })
    return {"count": count, "folders": result}


@app.post("/admin/reindex/{folder_id}")
async def trigger_reindex(folder_id: int, request: Request):
    if not _is_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    folders = db.get_folders()
    folder = next((f for f in folders if f["id"] == folder_id), None)
    if not folder:
        return JSONResponse({"error": "not found"}, status_code=404)
    threading.Thread(target=indexer.index_folder, args=(folder,), daemon=True).start()
    return {"status": "started"}


# ── Folders API ───────────────────────────────────────────────────────────────

class FolderIn(BaseModel):
    name: str
    path: str


class ScheduleIn(BaseModel):
    folder_id: int
    reindex_minutes: int


@app.get("/api/folders")
async def api_get_folders():
    return db.get_folders()


@app.post("/api/folders")
async def api_add_folder(data: FolderIn, request: Request):
    if not _is_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
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
        indexer.index_folder(folder)
        indexer.restart_watchdog()

    threading.Thread(target=_init, daemon=True).start()
    return folder


@app.get("/api/schedules")
async def api_get_schedules():
    from datetime import datetime
    rows = db.get_schedules()
    for r in rows:
        r["last_run_str"] = datetime.fromtimestamp(r["last_run_at"]).strftime("%d.%m.%Y %H:%M") if r.get("last_run_at") else "—"
        r["next_run_str"] = datetime.fromtimestamp(r["next_run_at"]).strftime("%d.%m.%Y %H:%M") if r.get("next_run_at") else "—"
    return rows


@app.post("/api/schedules")
async def api_add_schedule(data: ScheduleIn, request: Request):
    if not _is_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    result = db.add_schedule(data.folder_id, data.reindex_minutes)
    from datetime import datetime
    result["next_run_str"] = datetime.fromtimestamp(result["next_run_at"]).strftime("%d.%m.%Y %H:%M")
    result["last_run_str"] = "—"
    return result


@app.delete("/api/schedules/{schedule_id}")
async def api_delete_schedule(schedule_id: int, request: Request):
    if not _is_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    db.delete_schedule(schedule_id)
    return {"status": "deleted"}


@app.delete("/api/folders/{folder_id}")
async def api_delete_folder(folder_id: int, request: Request):
    if not _is_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    folder_name = db.delete_folder(folder_id)
    if folder_name is None:
        return JSONResponse({"error": "Папка не найдена"}, status_code=404)
    threading.Thread(target=indexer.restart_watchdog, daemon=True).start()
    return {"status": "deleted", "name": folder_name}


@app.get("/api/folders/{folder_id}/progress")
async def api_folder_progress(folder_id: int):
    return indexer.get_progress(folder_id)


# ── Client setup ──────────────────────────────────────────────────────────────

@app.get("/client/NbmSearchOpen.exe")
async def client_exe():
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
    script = f"""# NbmSearch - nbmsearch:// protocol handler setup
# Installs to ProgramData - works for all users on this machine.
# Run as Administrator!

#Requires -RunAsAdministrator
$ErrorActionPreference = "Stop"

$serverUrl  = "{server_url}"
$exeName    = "NbmSearchOpen.exe"
$installDir = "$env:ProgramData\\NbmSearch"
$exePath    = "$installDir\\$exeName"

Write-Host "Creating folder $installDir..."
New-Item -ItemType Directory -Force -Path $installDir | Out-Null

Write-Host "Downloading $exeName from $serverUrl/client/$exeName ..."
Invoke-WebRequest -Uri "$serverUrl/client/$exeName" -OutFile $exePath -UseBasicParsing

Write-Host "Registering nbmsearch:// protocol for all users (HKLM)..."
$regBase = "HKLM:\\Software\\Classes\\nbmsearch"
New-Item -Path $regBase -Force | Out-Null
Set-ItemProperty -Path $regBase -Name "(Default)"    -Value "URL:NbmSearch Protocol"
Set-ItemProperty -Path $regBase -Name "URL Protocol" -Value ""
New-Item -Path "$regBase\\shell\\open\\command" -Force | Out-Null
Set-ItemProperty -Path "$regBase\\shell\\open\\command" -Name "(Default)" `
    -Value "`"$exePath`" `"%1`""

Write-Host "Disabling protocol confirmation dialog in Chrome and Edge..."
foreach ($browser in @(
    "HKLM:\\SOFTWARE\\Policies\\Google\\Chrome\\AutoOpenProtocolHandlerAllowlist",
    "HKLM:\\SOFTWARE\\Policies\\Microsoft\\Edge\\AutoOpenProtocolHandlerAllowlist"
)) {{
    try {{
        New-Item -Path $browser -Force | Out-Null
        $idx = ((Get-Item $browser).Property.Count + 1).ToString()
        Set-ItemProperty -Path $browser -Name $idx -Value "nbmsearch"
    }} catch {{
        Write-Host "  Skipped: $browser" -ForegroundColor Yellow
    }}
}}

Write-Host ""
Write-Host "Done! Protocol nbmsearch:// registered for all users." -ForegroundColor Green
Write-Host "Please restart your browser."
"""
    return Response(
        content=b"\xef\xbb\xbf" + script.encode("utf-8"),
        media_type="text/plain; charset=utf-8",
    )


# ── Favicon / icon ────────────────────────────────────────────────────────────

@app.get("/favicon.ico")
async def favicon():
    icon = BUNDLE_DIR / "icon.png"
    if icon.exists():
        return FileResponse(str(icon), media_type="image/png")
    return Response(status_code=204)


@app.get("/icon.png")
async def icon_png():
    icon = BUNDLE_DIR / "icon.png"
    if icon.exists():
        return FileResponse(str(icon), media_type="image/png")
    return Response(status_code=204)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, reload=False, log_config=None)
