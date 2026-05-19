import sys
import os
import threading
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Optional

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys._MEIPASS)
else:
    BASE_DIR = Path(__file__).resolve().parent.parent

from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import uvicorn

from app.config import PORT, SEARCH_FOLDERS
from app import database as db
from app import indexer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="NbmSearch")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app.on_event("startup")
def startup():
    db.init_db()
    threading.Thread(target=_initial_and_scheduler, daemon=True).start()
    try:
        indexer.start_watchdog()
    except Exception as e:
        logger.error("Watchdog failed to start: %s", e)


def _initial_and_scheduler():
    indexer.full_reindex()
    indexer.reindex_scheduler()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    folders = [{"name": f["name"]} for f in SEARCH_FOLDERS]
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


@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request):
    count, by_folder, recent = db.stats()
    last_ts = indexer.last_reindex_time
    next_ts = indexer.next_reindex_time
    last_str = datetime.fromtimestamp(last_ts).strftime("%Y-%m-%d %H:%M:%S") if last_ts else "—"
    next_str = datetime.fromtimestamp(next_ts).strftime("%Y-%m-%d %H:%M:%S") if next_ts else "—"
    for r in recent:
        r["indexed_at_str"] = datetime.fromtimestamp(r["indexed_at"]).strftime("%Y-%m-%d %H:%M:%S")
    folders_config = [{"name": f["name"], "path": f["path"]} for f in SEARCH_FOLDERS]
    return templates.TemplateResponse("admin.html", {
        "request": request,
        "count": count,
        "by_folder": by_folder,
        "last_reindex": last_str,
        "next_reindex": next_str,
        "recent": recent,
        "folders_config": folders_config,
    })


@app.post("/admin/reindex")
async def trigger_reindex():
    threading.Thread(target=indexer.full_reindex, daemon=True).start()
    return {"status": "started"}


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


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=PORT, reload=False)
