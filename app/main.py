import sys
import os
import time
import threading
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import List, Optional

_start_time = time.time()

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

from app.settings import PORT, verify_password, VERSION, GITHUB_REPO
import app.settings as settings
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
    threading.Thread(target=db.log_search, args=(q.strip(),), daemon=True).start()
    try:
        results = db.search(q.strip(), folder_names=folders or None)
    except Exception as e:
        logger.error("Search error: %s", e)
        return {"results": [], "error": str(e)}
    return {"results": results}


@app.get("/api/search/stats")
async def api_search_stats(request: Request, period: str = "day"):
    if not _is_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        return db.get_search_stats(period)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


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
$exe = "$env:ProgramData\\NbmSearch\\NbmSearchHelper.exe"

New-Item -ItemType Directory -Force -Path (Split-Path $exe) | Out-Null
Invoke-WebRequest -Uri "{server_url}/client/NbmSearchHelper.exe" -OutFile $exe -UseBasicParsing

$reg = "HKLM:\\Software\\Classes\\nbmsearch"
New-Item -Path $reg -Force | Out-Null
Set-ItemProperty -Path $reg -Name "(Default)" -Value "URL:NbmSearch Protocol"
Set-ItemProperty -Path $reg -Name "URL Protocol" -Value ""
New-Item -Path "$reg\\shell\\open\\command" -Force | Out-Null
Set-ItemProperty -Path "$reg\\shell\\open\\command" -Name "(Default)" -Value "`"$exe`" `"%1`""

$json = '[{{"protocol":"nbmsearch","allowed_origins":["*"]}}]'
foreach ($p in @(
    "HKLM:\\SOFTWARE\\Policies\\Google\\Chrome",
    "HKLM:\\SOFTWARE\\Policies\\Microsoft\\Edge",
    "HKLM:\\SOFTWARE\\Policies\\YandexBrowser",
    "HKLM:\\SOFTWARE\\Policies\\Chromium"
)) {{
    try {{
        New-Item -Path $p -Force | Out-Null
        Set-ItemProperty -Path $p -Name "AutoLaunchProtocolsFromOrigins" -Value $json
    }} catch {{}}
}}

Write-Host "Done! Restart your browser." -ForegroundColor Green"""
    return templates.TemplateResponse("admin.html", {
        "request": request,
        "count": count,
        "folders": folders,
        "schedules": schedules,
        "setup_script": setup_script,
        "version": VERSION,
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


@app.post("/admin/reindex/{folder_id}/full")
async def trigger_full_reindex(folder_id: int, request: Request):
    if not _is_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    folders = db.get_folders()
    folder = next((f for f in folders if f["id"] == folder_id), None)
    if not folder:
        return JSONResponse({"error": "not found"}, status_code=404)
    threading.Thread(target=indexer.index_folder, args=(folder,), kwargs={"full": True}, daemon=True).start()
    return {"status": "started"}


@app.post("/api/folders/reorder")
async def api_reorder_folders(request: Request):
    if not _is_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    ids = body.get("ids", [])
    if not isinstance(ids, list):
        return JSONResponse({"error": "ids must be a list"}, status_code=400)
    try:
        db.reorder_folders([int(i) for i in ids])
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return {"status": "ok"}


@app.patch("/api/folders/{folder_id}")
async def api_rename_folder(folder_id: int, request: Request):
    if not _is_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    new_name = body.get("name", "").strip()
    if not new_name:
        return JSONResponse({"error": "Имя не может быть пустым"}, status_code=400)
    try:
        db.rename_folder(folder_id, new_name)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"status": "ok", "name": new_name}


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


@app.post("/api/folders/{folder_id}/watchdog")
async def api_toggle_watchdog(folder_id: int, request: Request):
    if not _is_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    enabled = bool(body.get("enabled", False))
    db.set_folder_watchdog(folder_id, enabled)
    threading.Thread(target=indexer.restart_watchdog, daemon=True).start()
    return {"folder_id": folder_id, "watchdog_enabled": enabled}


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


@app.get("/api/folders/{folder_id}/watchdog-log")
async def api_watchdog_log(folder_id: int, request: Request):
    if not _is_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    entries = indexer.get_watchdog_log(folder_id)
    return {"entries": entries}


@app.delete("/api/folders/{folder_id}/watchdog-log")
async def api_clear_watchdog_log(folder_id: int, request: Request):
    if not _is_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    indexer.clear_watchdog_log(folder_id)
    return {"status": "cleared"}


@app.get("/api/folders/{folder_id}/stats")
async def api_folder_stats(folder_id: int, request: Request, period: str = "month"):
    if not _is_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        return db.get_folder_stats(folder_id, period)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Management API ───────────────────────────────────────────────────────────

def _check_mgmt_token(request: Request) -> bool:
    token = request.headers.get("X-Management-Token", "").strip()
    if not token:
        return False
    stored = settings.load().get("management_token", "")
    return bool(stored) and token == stored


@app.post("/api/management/token/generate")
async def api_mgmt_generate(request: Request):
    if not _is_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    import secrets as _sec
    token = _sec.token_hex(32)
    s = settings.load()
    s["management_token"] = token
    s.pop("management_server", None)
    settings.save(s)
    return {"token": token}


@app.delete("/api/management/token")
async def api_mgmt_delete_token(request: Request):
    if not _is_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    s = settings.load()
    s.pop("management_token", None)
    s.pop("management_server", None)
    settings.save(s)
    return {"status": "ok"}


@app.get("/api/management/status")
async def api_mgmt_status(request: Request):
    if not _is_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    s = settings.load()
    return {
        "has_token": bool(s.get("management_token")),
        "management_server": s.get("management_server", ""),
    }


@app.post("/api/management/register")
async def api_mgmt_register(request: Request):
    if not _check_mgmt_token(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    panel_url = body.get("panel_url", "").strip()
    s = settings.load()
    s["management_server"] = panel_url
    settings.save(s)
    return {"status": "ok"}


@app.get("/api/management/info")
async def api_mgmt_info(request: Request):
    if not _check_mgmt_token(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    count, by_folder = db.stats()
    folders = db.get_folders()
    try:
        search_stats = db.get_search_stats("day")
        summary = search_stats.get("summary", {})
    except Exception:
        summary = {}
    schedules = db.get_schedules()
    return {
        "version": VERSION,
        "file_count": count,
        "folder_count": len(folders),
        "uptime": int(time.time() - _start_time),
        "folders": [
            {
                "id": f["id"], "name": f["name"], "path": f["path"],
                "file_count": f.get("file_count", 0),
                "watchdog_enabled": f.get("watchdog_enabled", 0),
                "last_reindex_at": f.get("last_reindex_at"),
            } for f in folders
        ],
        "schedules": [
            {
                "id": s["id"], "folder_name": s["folder_name"],
                "reindex_minutes": s["reindex_minutes"],
                "last_run_at": s.get("last_run_at"),
                "next_run_at": s.get("next_run_at"),
            } for s in schedules
        ],
        "search_summary": summary,
    }


@app.get("/api/management/update-check")
async def api_mgmt_update_check(request: Request):
    if not _check_mgmt_token(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, _fetch_latest_release)
        latest_tag = data.get("tag_name", "").lstrip("v")
        assets = data.get("assets", [])
        download_url = next(
            (a["browser_download_url"] for a in assets
             if a["name"].lower() == "nbmsearch.exe"),
            None,
        )
        return {
            "current": VERSION,
            "latest": latest_tag,
            "update_available": _version_gt(latest_tag, VERSION),
            "download_url": download_url,
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/management/update-start")
async def api_mgmt_update_start(request: Request):
    if not _check_mgmt_token(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not getattr(sys, "frozen", False):
        return JSONResponse({"error": "Только в собранном приложении"}, status_code=400)
    body = await request.json()
    download_url = body.get("download_url", "").strip()
    if not download_url:
        return JSONResponse({"error": "Нет ссылки"}, status_code=400)
    exe_path     = str(Path(sys.executable))
    updater_path = str(Path(sys.executable).parent / "NbmSearchUpdater.exe")
    if not os.path.exists(updater_path):
        return JSONResponse({"error": "NbmSearchUpdater.exe не найден"}, status_code=500)
    try:
        (BASE_DIR / "update_status.json").unlink(missing_ok=True)
    except Exception:
        pass
    import subprocess as _sp
    _sp.Popen(
        [updater_path, str(os.getpid()), download_url, exe_path, str(PORT)],
        creationflags=_sp.DETACHED_PROCESS | _sp.CREATE_NEW_PROCESS_GROUP,
    )
    return {"status": "started"}


@app.get("/api/management/update-status")
async def api_mgmt_update_status(request: Request):
    if not _check_mgmt_token(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    import json as _json
    status_path = BASE_DIR / "update_status.json"
    if not status_path.exists():
        return {"stage": "idle", "progress": 0, "message": "", "error": None}
    try:
        with open(status_path, encoding="utf-8") as f:
            return _json.load(f)
    except Exception:
        return {"stage": "idle", "progress": 0, "message": "", "error": None}


@app.get("/api/management/search-stats")
async def api_mgmt_search_stats(request: Request, period: str = "day"):
    if not _check_mgmt_token(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        return db.get_search_stats(period)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/management/restart")
async def api_mgmt_restart(request: Request):
    if not _check_mgmt_token(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    def _do_restart():
        time.sleep(1)
        if getattr(sys, "frozen", False):
            import subprocess as _sp
            exe_path     = str(Path(sys.executable))
            updater_path = str(Path(sys.executable).parent / "NbmSearchUpdater.exe")
            pid = os.getpid()
            if os.path.exists(updater_path):
                _sp.Popen(
                    [updater_path, '--restart', str(pid), exe_path, str(PORT)],
                    creationflags=_sp.DETACHED_PROCESS | _sp.CREATE_NEW_PROCESS_GROUP,
                )
        # In service mode the updater handles stop+start via sc;
        # os._exit stops the service process so SCM marks it stopped.
        os._exit(0)

    threading.Thread(target=_do_restart, daemon=True).start()
    return {"status": "restarting"}


# ── Update ────────────────────────────────────────────────────────────────────

def _version_gt(a: str, b: str) -> bool:
    """Return True if semver a > b."""
    try:
        return tuple(int(x) for x in a.split(".")) > tuple(int(x) for x in b.split("."))
    except Exception:
        return False


def _fetch_latest_release() -> dict:
    """Blocking GitHub API call — run in thread executor."""
    import urllib.request as _req
    import json as _json
    import ssl as _ssl
    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    req = _req.Request(url, headers={"User-Agent": "NbmSearch"})
    with _req.urlopen(req, timeout=15, context=ctx) as r:
        return _json.loads(r.read())


@app.get("/api/update/check")
async def api_update_check(request: Request):
    if not _is_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, _fetch_latest_release)
        latest_tag = data.get("tag_name", "").lstrip("v")
        assets = data.get("assets", [])
        download_url = next(
            (a["browser_download_url"] for a in assets
             if a["name"].lower().startswith("nbmsearch")
             and not a["name"].lower().startswith("nbmsearchopen")
             and a["name"].lower().endswith(".exe")),
            None,
        )
        return {
            "current": VERSION,
            "latest": latest_tag,
            "update_available": _version_gt(latest_tag, VERSION),
            "download_url": download_url,
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/update/status")
async def api_update_status(request: Request):
    if not _is_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    import json as _json
    status_path = BASE_DIR / "update_status.json"
    if not status_path.exists():
        return {"stage": "idle", "progress": 0, "message": "", "error": None}
    try:
        with open(status_path, encoding="utf-8") as f:
            return _json.load(f)
    except Exception:
        return {"stage": "idle", "progress": 0, "message": "", "error": None}


@app.post("/api/update/start")
async def api_update_start(request: Request):
    if not _is_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not getattr(sys, "frozen", False):
        return JSONResponse({"error": "Обновление доступно только в собранном приложении"}, status_code=400)
    body = await request.json()
    download_url = body.get("download_url", "").strip()
    if not download_url:
        return JSONResponse({"error": "Нет ссылки на обновление"}, status_code=400)
    exe_path = str(Path(sys.executable))
    updater_path = str(Path(sys.executable).parent / "NbmSearchUpdater.exe")
    if not os.path.exists(updater_path):
        return JSONResponse({"error": "NbmSearchUpdater.exe не найден рядом с приложением"}, status_code=500)
    # Clear previous status
    try:
        (BASE_DIR / "update_status.json").unlink(missing_ok=True)
    except Exception:
        pass
    import subprocess as _sp
    pid = os.getpid()
    _sp.Popen(
        [updater_path, str(pid), download_url, exe_path, str(PORT)],
        creationflags=_sp.DETACHED_PROCESS | _sp.CREATE_NEW_PROCESS_GROUP,
    )
    # Updater will kill us after download completes — no need for os._exit here
    return {"status": "started"}


# ── Client setup ──────────────────────────────────────────────────────────────

@app.get("/client/NbmSearchHelper.exe")
async def client_exe():
    if getattr(sys, "frozen", False):
        exe_path = Path(sys.executable).parent / "NbmSearchHelper.exe"
    else:
        exe_path = BASE_DIR / "NbmSearchHelper.exe"
    if not exe_path.exists():
        return JSONResponse({"error": "NbmSearchHelper.exe not found on server"}, status_code=404)
    return FileResponse(str(exe_path), media_type="application/octet-stream", filename="NbmSearchHelper.exe")


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
$exeName    = "NbmSearchHelper.exe"
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
