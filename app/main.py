from __future__ import annotations

import asyncio
import json
import time
import zipfile
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.config import BASE_DIR, choose_model, load_topics, settings, total_memory_gb
from app.db import Database
from app.services.analyzer import Analyzer
from app.services.baseline import BaselineStore
from app.services.fetch import Fetcher
from app.services.knowledge import KnowledgePipeline
from app.services.ollama import OllamaClient
from app.services.refresh import RefreshEngine
from app.services.search import Searcher
from app.services.taskqueue import TaskWorker
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
    extract_model=settings.ollama_extract_model,
    num_ctx=settings.ollama_num_ctx,
    num_gpu=settings.ollama_num_gpu,
    extract_num_gpu=settings.ollama_extract_num_gpu,
    num_thread=settings.ollama_num_thread,
    extract_num_thread=settings.ollama_extract_num_thread,
    final_think=settings.ollama_final_think,
    keep_alive=settings.ollama_keep_alive,
    mock=settings.mock_ai,
    embed_model=settings.embedding_model,
    metrics_sink=db.add_llm_call,
)
analyzer = Analyzer(
    ollama,
    chunk_tokens=settings.ai_chunk_tokens,
    extract_predict=settings.ai_extract_predict,
    reduce_tokens=settings.ai_reduce_tokens,
    reduce_predict=settings.ai_reduce_predict,
    final_tokens=settings.ai_final_tokens,
    final_predict=settings.ai_final_predict,
)
knowledge_pipeline = KnowledgePipeline(db, analyzer, settings)
engine = RefreshEngine(db, topics_cfg, searcher, fetcher, analyzer, baseline, knowledge_pipeline, settings)
worker = TaskWorker(db, engine, poll_seconds=settings.queue_poll_seconds)


def _cfg(slug: str) -> dict:
    for t in topics_cfg:
        if t["slug"] == slug:
            return t
    raise KeyError(slug)


def _create_backup(prune: bool = True):
    """Create a consistent SQLite backup without copying the live WAL files."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    zip_path = settings.backup_dir / f"internetboard-backup-{stamp}.zip"
    db_copy = settings.backup_dir / f"internetboard-{stamp}.db"
    with db.connect() as con:
        dest = __import__("sqlite3").connect(db_copy)
        try:
            con.backup(dest)
        finally:
            dest.close()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(db_copy, "internetboard.db")
        z.write(settings.topics_file, "config/topics.yml")
        z.write(settings.baseline_file, "seed/baseline-v2.8.md")
    db_copy.unlink(missing_ok=True)
    if prune:
        keep = max(1, int(settings.auto_backup_keep))
        backups = sorted(settings.backup_dir.glob("internetboard-backup-*.zip"), key=lambda x: x.stat().st_mtime, reverse=True)
        for old in backups[keep:]:
            old.unlink(missing_ok=True)
    return zip_path


async def scheduled_refresh():
    # Daily pre-run backup is intentionally best-effort: a backup error must not stop monitoring.
    try:
        await asyncio.to_thread(_create_backup, True)
    except Exception:
        pass
    day = datetime.now(ZoneInfo(settings.timezone)).date().isoformat()
    db.enqueue_task(
        "refresh-all", payload={"mode": "scheduled"}, priority=40,
        unique_key=f"scheduled-refresh:{day}", max_attempts=settings.queue_max_attempts,
    )


scheduler = DailyScheduler(settings.timezone, settings.schedule_hour, settings.schedule_minute, scheduled_refresh)


@asynccontextmanager
async def lifespan(app: FastAPI):
    worker.start()
    scheduler.start()
    if settings.auto_pull_model and not settings.mock_ai:
        # Never block the panel while models download. Embedding model is intentionally on-demand.
        asyncio.create_task(ollama.ensure_required_models())
    yield
    await scheduler.shutdown()
    await worker.shutdown()


app = FastAPI(title="InternetBoard", version="0.4.0", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    max_age=60 * 60 * 24 * 30,
    same_site="lax",
    https_only=settings.session_https_only,
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Small in-memory brute-force limiter. Reverse proxy users are keyed by X-Forwarded-For.
_login_attempts: dict[str, deque[float]] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return forwarded or (request.client.host if request.client else "unknown")


def _login_allowed(request: Request) -> bool:
    key = _client_ip(request)
    now = time.time()
    q = _login_attempts[key]
    while q and now - q[0] > settings.login_window_seconds:
        q.popleft()
    return len(q) < settings.login_max_attempts


def _login_failed(request: Request) -> None:
    _login_attempts[_client_ip(request)].append(time.time())


def _login_succeeded(request: Request) -> None:
    _login_attempts.pop(_client_ip(request), None)


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
        "version": "0.4.0",
        "time": datetime.now(ZoneInfo(settings.timezone)).isoformat(timespec="seconds"),
        "db": str(settings.db_path),
        "selected_model": ollama.model,
        "extract_model": ollama.extract_model,
        "schedule": f"{settings.schedule_hour:02d}:{settings.schedule_minute:02d}",
        "timezone": settings.timezone,
        "busy": engine.busy() or db.queue_busy(),
        "queue": db.list_tasks(5),
    }


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if logged_in(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html")


@app.post("/login")
async def login(request: Request, password: str = Form(...)):
    if not _login_allowed(request):
        return templates.TemplateResponse(request, "login.html", {"error": "尝试次数过多，请稍后再试"}, status_code=429)
    if password == settings.admin_password:
        _login_succeeded(request)
        request.session["auth"] = True
        return RedirectResponse("/", status_code=303)
    _login_failed(request)
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
    runs = db.list_runs(20)
    next_run = scheduler.next_run_time.isoformat(timespec="minutes") if scheduler.next_run_time else "-"
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "topics": topics,
            "runs": runs,
            "next_run": next_run,
            "schedule": f"{settings.schedule_hour:02d}:{settings.schedule_minute:02d}",
            "busy": engine.busy() or db.queue_busy(),
            "knowledge": db.knowledge_stats(),
            "tasks": db.list_tasks(20),
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
    return templates.TemplateResponse(
        request,
        "topic.html",
        {
            "topic": topic,
            "analysis": analysis,
            "evidence": db.list_evidence(slug, 100),
            "claims": db.list_claims(slug, 120),
            "manual_sources": db.list_manual_sources(slug, 50),
            "searches": db.list_search_attempts(slug, 40),
            "snapshots": db.list_snapshots(slug, 30),
            "watches": db.list_watch_nodes(slug),
            "custom_queries": db.list_custom_queries(slug),
            "cfg": _cfg(slug),
            "busy": engine.busy() or db.queue_busy(),
            "queued_manual": request.query_params.get("knowledge") == "queued",
            "relations": db.list_claim_relations(slug, 50),
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
            "extract_model": settings.ollama_extract_model,
            "requested_model": settings.ollama_model,
            "next_run": next_run,
            "timezone": settings.timezone,
            "admin_default": settings.admin_password == "change-me-now",
            "knowledge": db.knowledge_stats(),
            "num_ctx": settings.ollama_num_ctx,
            "num_gpu": settings.ollama_num_gpu,
            "extract_num_gpu": settings.ollama_extract_num_gpu,
            "chunk_tokens": settings.ai_chunk_tokens,
            "final_tokens": settings.ai_final_tokens,
            "searxng": settings.searxng_url or "未配置（使用内置联网搜索）",
            "embedding_model": settings.embedding_model,
            "embeddings_enabled": settings.enable_embeddings,
            "semantic_min_claims": settings.semantic_rag_min_claims,
            "metrics": db.metrics_summary(),
            "tasks": db.list_tasks(20),
        },
    )


@app.post("/api/refresh/all")
async def refresh_all(request: Request):
    if not logged_in(request):
        raise HTTPException(401)
    task_id = db.enqueue_task(
        "refresh-all", payload={"mode": "manual-all"}, priority=80, unique_key="manual-refresh-all", max_attempts=settings.queue_max_attempts
    )
    return JSONResponse({"ok": True, "queued": True, "task_id": task_id, "message": "已加入任务队列"})


@app.post("/api/refresh/{slug}")
async def refresh_topic(request: Request, slug: str):
    if not logged_in(request):
        raise HTTPException(401)
    if not db.get_topic(slug):
        raise HTTPException(404)
    task_id = db.enqueue_task(
        "refresh-topic", slug, {"mode": "manual"}, priority=90, unique_key=f"refresh-topic:{slug}", max_attempts=settings.queue_max_attempts
    )
    return JSONResponse({"ok": True, "queued": True, "task_id": task_id, "message": f"{slug} 已加入任务队列"})


def _runtime_payload(run_limit: int = 30, task_limit: int = 30) -> dict:
    runs = db.list_runs(run_limit)
    steps = []
    if runs:
        # Show the latest run's durable stage ledger so the UI can say what the NAS is doing now.
        steps = db.list_run_steps(int(runs[0]["id"]))
    return {
        "busy": engine.busy() or db.queue_busy(),
        "runs": runs,
        "tasks": db.list_tasks(task_limit),
        "steps": steps,
    }


@app.get("/api/runs")
async def api_runs(request: Request):
    if not logged_in(request):
        raise HTTPException(401)
    return _runtime_payload()


@app.get("/api/runs/{run_id}/steps")
async def api_run_steps(request: Request, run_id: int):
    if not logged_in(request):
        raise HTTPException(401)
    return {"steps": db.list_run_steps(run_id)}


@app.get("/api/events")
async def api_events(request: Request):
    if not logged_in(request):
        raise HTTPException(401)

    async def stream():
        last = None
        while True:
            if await request.is_disconnected():
                break
            payload = _runtime_payload(8, 8)
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            if encoded != last:
                yield f"data: {encoded}\n\n"
                last = encoded
            await asyncio.sleep(2)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/evidence/{evidence_id}/raw", response_class=HTMLResponse)
async def raw_evidence(request: Request, evidence_id: int):
    if r := protect(request):
        return r
    evidence = db.get_evidence(evidence_id)
    if not evidence:
        raise HTTPException(404)
    text = knowledge_pipeline.read_archived_text(evidence)
    safe_title = evidence.get("title") or f"Evidence #{evidence_id}"
    return templates.TemplateResponse(request, "raw_evidence.html", {"evidence": evidence, "raw_text": text, "safe_title": safe_title})


@app.post("/api/evidence/{evidence_id}/review")
async def review_evidence(request: Request, evidence_id: int, status: str = Form(...), note: str = Form("")):
    if not logged_in(request):
        raise HTTPException(401)
    if status not in {"approved", "rejected", "unreviewed"}:
        raise HTTPException(400, "invalid status")
    db.set_review_status(evidence_id, status, note)
    return RedirectResponse(request.headers.get("referer") or "/", status_code=303)


@app.post("/topics/{slug}/knowledge")
async def add_manual_knowledge(
    request: Request,
    slug: str,
    source_type: str = Form(...),
    title: str = Form(""),
    raw_content: str = Form(...),
    source_url: str = Form(""),
    info_date: str = Form(""),
    confidence_label: str = Form("medium"),
):
    if not logged_in(request):
        raise HTTPException(401)
    if not db.get_topic(slug):
        raise HTTPException(404)
    if source_type not in {"news", "intel"}:
        raise HTTPException(400, "invalid source_type")
    if confidence_label not in {"high", "medium", "low", "unknown"}:
        confidence_label = "medium"
    source_id = db.add_manual_source(slug, source_type, title, source_url, info_date or None, confidence_label, raw_content)
    db.enqueue_task(
        "manual-knowledge", slug, {"source_id": source_id}, priority=100, unique_key=f"manual-knowledge:{source_id}", max_attempts=settings.queue_max_attempts
    )
    return RedirectResponse(f"/topics/{slug}?knowledge=queued", status_code=303)


@app.post("/manual/{source_id}/retry")
async def retry_manual(request: Request, source_id: int):
    if not logged_in(request):
        raise HTTPException(401)
    item = db.get_manual_source(source_id)
    if not item:
        raise HTTPException(404)
    db.enqueue_task(
        "manual-knowledge", item["topic_slug"], {"source_id": source_id}, priority=100, unique_key=f"manual-knowledge:{source_id}", max_attempts=settings.queue_max_attempts
    )
    return RedirectResponse(f"/topics/{item['topic_slug']}?knowledge=queued", status_code=303)


@app.post("/claims/{claim_id}/edit")
async def edit_claim(
    request: Request,
    claim_id: int,
    statement: str = Form(...),
    event_date: str = Form(""),
    certainty: str = Form("unknown"),
    confidence: float = Form(0.7),
    entities: str = Form(""),
    note: str = Form(""),
):
    if not logged_in(request):
        raise HTTPException(401)
    claim = db.get_claim(claim_id)
    if not claim:
        raise HTTPException(404)
    entity_list = [x.strip() for x in entities.replace("，", ",").split(",") if x.strip()]
    db.update_claim_human(claim_id, statement, event_date or None, certainty, confidence, entity_list, note)
    return RedirectResponse(f"/topics/{claim['topic_slug']}#knowledge-base", status_code=303)


@app.post("/claims/{claim_id}/delete")
async def delete_claim(request: Request, claim_id: int):
    if not logged_in(request):
        raise HTTPException(401)
    claim = db.get_claim(claim_id)
    if not claim:
        raise HTTPException(404)
    db.delete_claim(claim_id)
    return RedirectResponse(f"/topics/{claim['topic_slug']}#knowledge-base", status_code=303)


@app.get("/api/claims/{claim_id}/versions")
async def claim_versions(request: Request, claim_id: int):
    if not logged_in(request):
        raise HTTPException(401)
    if not db.get_claim(claim_id):
        raise HTTPException(404)
    return {"versions": db.list_claim_versions(claim_id)}


@app.get("/api/backup")
async def backup(request: Request):
    if not logged_in(request):
        raise HTTPException(401)
    zip_path = await asyncio.to_thread(_create_backup, True)
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
