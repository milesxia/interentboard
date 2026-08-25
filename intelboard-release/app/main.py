from __future__ import annotations

import asyncio
import json
import shutil
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.config import BASE_DIR, choose_model, load_topics, settings, total_memory_gb
from app.db import Database
from app.services.analyzer import Analyzer
from app.services.baseline import BaselineStore
from app.services.fetch import Fetcher
from app.services.ollama import OllamaClient
from app.services.refresh import RefreshEngine
from app.services.search import Searcher
from app.scheduler import DailyScheduler

settings.ensure_dirs()
topics_cfg = load_topics(settings.topics_file)
db = Database(settings.db_path)
db.seed_topics(topics_cfg)
baseline = BaselineStore(settings.baseline_file)
if baseline.text:
    db.seed_knowledge("baseline-v2.8", "综合交接文件 V2.8", baseline.text, baseline.content_hash)
searcher = Searcher(settings.max_search_results, settings.searxng_url)
fetcher = Fetcher(settings.request_timeout)
ollama = OllamaClient(
    settings.ollama_base_url,
    settings.ollama_model,
    num_ctx=settings.ollama_num_ctx,
    mock=settings.mock_ai,
)
analyzer = Analyzer(ollama, settings.ai_context_chars)
engine = RefreshEngine(db, topics_cfg, searcher, fetcher, analyzer, baseline, settings)



def _cfg(slug: str) -> dict:
    for t in topics_cfg:
        if t["slug"] == slug:
            return t
    raise KeyError(slug)


async def scheduled_refresh():
    await engine.refresh_all(mode="scheduled")


scheduler = DailyScheduler(settings.timezone, settings.schedule_hour, settings.schedule_minute, scheduled_refresh)


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.start()
    if settings.auto_pull_model and not settings.mock_ai:
        asyncio.create_task(ollama.ensure_model())
    yield
    await scheduler.shutdown()


app = FastAPI(title="IntelBoard", version="0.2.0", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, max_age=60 * 60 * 24 * 30, same_site="lax")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def logged_in(request: Request) -> bool:
    return request.session.get("auth") is True


def protect(request: Request):
    if not logged_in(request):
        return RedirectResponse("/login", status_code=303)
    return None


@app.get("/healthz")
async def healthz():
    return {
        "ok": True,
        "time": datetime.now(ZoneInfo(settings.timezone)).isoformat(timespec="seconds"),
        "db": str(settings.db_path),
        "selected_model": ollama.model,
        "schedule": f"{settings.schedule_hour:02d}:{settings.schedule_minute:02d}",
        "timezone": settings.timezone,
        "busy": engine.busy(),
    }


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if logged_in(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html")


@app.post("/login")
async def login(request: Request, password: str = Form(...)):
    if password == settings.admin_password:
        request.session["auth"] = True
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": "密码错误"}, status_code=401)


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if r := protect(request):
        return r
    topics = db.list_topics()
    runs = db.list_runs(16)
    next_run = scheduler.next_run_time.isoformat(timespec="minutes") if scheduler.next_run_time else "-"
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "topics": topics,
            "runs": runs,
            "next_run": next_run,
            "schedule": f"{settings.schedule_hour:02d}:{settings.schedule_minute:02d}",
            "busy": engine.busy(),
            "knowledge": db.knowledge_stats(),
        },
    )


@app.get("/topics/{slug}", response_class=HTMLResponse)
async def topic_page(request: Request, slug: str):
    if r := protect(request):
        return r
    topic = db.get_topic(slug)
    if not topic:
        raise HTTPException(404)
    topic["stage_state_obj"] = json.loads(topic.get("stage_state") or "{}")
    try:
        analysis = json.loads(topic.get("last_analysis_json") or "{}")
    except Exception:
        analysis = {}
    evidence = db.list_evidence(slug, 100)
    searches = db.list_search_attempts(slug, 40)
    snapshots = db.list_snapshots(slug, 30)
    watches = db.list_watch_nodes(slug)
    custom_queries = db.list_custom_queries(slug)
    cfg = _cfg(slug)
    return templates.TemplateResponse(
        request,
        "topic.html",
        {
            "topic": topic,
            "analysis": analysis,
            "evidence": evidence,
            "searches": searches,
            "snapshots": snapshots,
            "watches": watches,
            "custom_queries": custom_queries,
            "cfg": cfg,
            "busy": engine.busy(),
        },
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    if r := protect(request):
        return r
    health = await ollama.health()
    next_run = scheduler.next_run_time.isoformat(timespec="minutes") if scheduler.next_run_time else "-"
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "health": health,
            "memory_gb": round(total_memory_gb(), 1),
            "selected_model": choose_model(settings.ollama_model),
            "requested_model": settings.ollama_model,
            "next_run": next_run,
            "timezone": settings.timezone,
            "admin_default": settings.admin_password == "change-me-now",
            "knowledge": db.knowledge_stats(),
            "num_ctx": settings.ollama_num_ctx,
            "searxng": settings.searxng_url or "未配置（使用内置联网搜索）",
        },
    )


@app.post("/api/refresh/all")
async def refresh_all(request: Request):
    if not logged_in(request):
        raise HTTPException(401)
    if engine.busy():
        return JSONResponse({"ok": False, "busy": True, "message": "已有检索任务运行"}, status_code=409)
    asyncio.create_task(engine.refresh_all(mode="manual-all"))
    return JSONResponse({"ok": True, "message": "已开始完整刷新"})


@app.post("/api/refresh/{slug}")
async def refresh_topic(request: Request, slug: str):
    if not logged_in(request):
        raise HTTPException(401)
    if not db.get_topic(slug):
        raise HTTPException(404)
    if engine.busy():
        return JSONResponse({"ok": False, "busy": True, "message": "已有检索任务运行"}, status_code=409)
    asyncio.create_task(engine.refresh_topic(slug, mode="manual"))
    return JSONResponse({"ok": True, "message": f"已开始刷新 {slug}"})


@app.get("/api/runs")
async def api_runs(request: Request):
    if not logged_in(request):
        raise HTTPException(401)
    return {"busy": engine.busy(), "runs": db.list_runs(30)}


@app.post("/api/evidence/{evidence_id}/review")
async def review_evidence(request: Request, evidence_id: int, status: str = Form(...), note: str = Form("")):
    if not logged_in(request):
        raise HTTPException(401)
    if status not in {"approved", "rejected", "unreviewed"}:
        raise HTTPException(400, "invalid status")
    db.set_review_status(evidence_id, status, note)
    return RedirectResponse(request.headers.get("referer") or "/", status_code=303)


@app.get("/api/backup")
async def backup(request: Request):
    if not logged_in(request):
        raise HTTPException(401)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    zip_path = settings.backup_dir / f"intelboard-backup-{stamp}.zip"
    db_copy = settings.backup_dir / f"intelboard-{stamp}.db"
    with db.connect() as con:
        dest = __import__("sqlite3").connect(db_copy)
        try:
            con.backup(dest)
        finally:
            dest.close()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(db_copy, "intelboard.db")
        z.write(settings.topics_file, "config/topics.yml")
        z.write(settings.baseline_file, "seed/baseline-v2.8.md")
    db_copy.unlink(missing_ok=True)
    return FileResponse(zip_path, filename=zip_path.name, media_type="application/zip")


@app.post("/topics/{slug}/queries")
async def add_query(request: Request, slug: str, query: str = Form(...)):
    if not logged_in(request):
        raise HTTPException(401)
    if not db.get_topic(slug):
        raise HTTPException(404)
    db.add_custom_query(slug, query)
    return RedirectResponse(f"/topics/{slug}", status_code=303)


@app.post("/topics/{slug}/queries/{query_id}/delete")
async def delete_query(request: Request, slug: str, query_id: int):
    if not logged_in(request):
        raise HTTPException(401)
    db.delete_custom_query(query_id, slug)
    return RedirectResponse(f"/topics/{slug}", status_code=303)
