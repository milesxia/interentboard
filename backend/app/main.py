from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, ORJSONResponse
from sqlalchemy import func, select, text

from .bootstrap import bootstrap_defaults_once
from .config import settings
from .db import engine, init_db, session_scope
from .handoff import build_handoff_markdown
from .knowledge import record_version
from .models import (
    Claim,
    ClaimEvidence,
    Conflict,
    Entity,
    ManualNote,
    Relation,
    ResearchRun,
    RunEvidence,
    RunStatus,
    Source,
    Topic,
    WebsiteWatch,
    utcnow,
)
from .ollama_client import ollama
from .schemas import (
    ClaimOut,
    ConflictOut,
    ConflictResolve,
    ManualClaimCreate,
    ManualClaimUpdate,
    ManualNoteCreate,
    RunOut,
    SourceOut,
    TopicCreate,
    TopicOut,
    TopicUpdate,
    WebsiteWatchCreate,
    WebsiteWatchOut,
)
from .runtime import run_runtime_state, runtime_snapshot
from .tasks import enqueue_run, ensure_run_enqueued, run_research_task
from .utils import normalize_space, sha256_text

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("internetboard")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    bootstrap = bootstrap_defaults_once()
    logger.info("Default topic bootstrap: %s", bootstrap)
    logger.info("InternetBoard %s started", settings.app_version)
    yield


app = FastAPI(
    title="InternetBoard API",
    version=settings.app_version,
    default_response_class=ORJSONResponse,
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict:
    db_ok = False
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        pass
    model = ollama.health()
    payload = {
        "ok": db_ok and model.get("ok", False) and model.get("model_ready", False),
        "database": db_ok,
        "ollama": model,
        "version": settings.app_version,
    }
    return ORJSONResponse(status_code=200 if payload["ok"] else 503, content=payload)


@app.get("/api/system/status")
def system_status() -> dict:
    runtime = runtime_snapshot()
    with session_scope() as session:
        topic_count = session.scalar(select(func.count()).select_from(Topic)) or 0
        active_rows = list(
            session.scalars(
                select(ResearchRun).where(
                    ResearchRun.status.not_in([RunStatus.COMPLETED.value, RunStatus.FAILED.value])
                )
            )
        )
        source_count = session.scalar(select(func.count()).select_from(Source)) or 0
        claim_count = session.scalar(select(func.count()).select_from(Claim)) or 0
        conflict_count = session.scalar(
            select(func.count()).select_from(Conflict).where(Conflict.status == "open")
        ) or 0
    runtime_states = {run.id: run_runtime_state(run.id) for run in active_rows}
    runtime["stale_run_ids"] = [run_id for run_id, state in runtime_states.items() if state == "stale"]
    runtime["queued_run_ids"] = [run_id for run_id, state in runtime_states.items() if state == "queued"]
    runtime["running_run_ids"] = [run_id for run_id, state in runtime_states.items() if state == "running"]
    return {
        "app": {"name": settings.app_name, "version": settings.app_version, "timezone": settings.timezone},
        "model": ollama.health(),
        "running_models": ollama.running_models(),
        "model_runtime": ollama.resource_summary(),
        "runtime": runtime,
        "limits": {
            "context_length": settings.ollama_context_length,
            "max_search_rounds": settings.max_search_rounds,
            "max_sources_per_run": settings.max_sources_per_run,
            "max_total_ai_chunks": settings.max_total_ai_chunks,
            "visual_enabled": settings.visual_enabled,
            "max_visual_assets_per_run": settings.visual_max_assets_per_run,
        },
        "counts": {
            "topics": topic_count,
            "active_runs": len(active_rows),
            "sources": source_count,
            "claims": claim_count,
            "open_conflicts": conflict_count,
        },
    }

@app.get("/api/dashboard")
def dashboard() -> dict:
    with session_scope() as session:
        latest_runs = list(session.scalars(select(ResearchRun).order_by(ResearchRun.created_at.desc()).limit(20)))
        latest_claims = list(
            session.scalars(
                select(Claim).where(Claim.status == "active").order_by(Claim.priority.desc(), Claim.importance.desc(), Claim.updated_at.desc()).limit(30)
            )
        )
        conflicts = list(
            session.scalars(select(Conflict).where(Conflict.status == "open").order_by(Conflict.created_at.desc()).limit(20))
        )
        topics = list(session.scalars(select(Topic).order_by(Topic.priority.desc(), Topic.name.asc())))
    return {
        "topics": [TopicOut.model_validate(x).model_dump() for x in topics],
        "runs": [
            {**RunOut.model_validate(x).model_dump(), "runtime_state": run_runtime_state(x.id, terminal=x.status in {RunStatus.COMPLETED.value, RunStatus.FAILED.value})}
            for x in latest_runs
        ],
        "claims": [ClaimOut.model_validate(x).model_dump() for x in latest_claims],
        "conflicts": [ConflictOut.model_validate(x).model_dump() for x in conflicts],
    }


@app.get("/api/topics", response_model=list[TopicOut])
def list_topics() -> list[Topic]:
    with session_scope() as session:
        return list(session.scalars(select(Topic).order_by(Topic.priority.desc(), Topic.id.asc())))


@app.post("/api/topics", response_model=TopicOut)
def create_topic(payload: TopicCreate) -> Topic:
    with session_scope() as session:
        if session.scalar(select(Topic).where(Topic.name == payload.name)):
            raise HTTPException(409, "Topic name already exists")
        topic = Topic(**payload.model_dump())
        session.add(topic)
        session.flush()
        return topic


@app.patch("/api/topics/{topic_id}", response_model=TopicOut)
def update_topic(topic_id: int, payload: TopicUpdate) -> Topic:
    with session_scope() as session:
        topic = session.get(Topic, topic_id)
        if not topic:
            raise HTTPException(404, "Topic not found")
        for key, value in payload.model_dump(exclude_none=True).items():
            setattr(topic, key, value)
        session.flush()
        return topic


@app.post("/api/topics/{topic_id}/run", response_model=RunOut)
def run_topic(topic_id: int) -> ResearchRun:
    with session_scope() as session:
        topic = session.scalar(select(Topic).where(Topic.id == topic_id).with_for_update())
        if not topic:
            raise HTTPException(404, "Topic not found")
        active = session.scalar(
            select(ResearchRun).where(
                ResearchRun.topic_id == topic_id,
                ResearchRun.status.not_in([RunStatus.COMPLETED.value, RunStatus.FAILED.value]),
            )
        )
        if active:
            active_id = active.id
            run_id = None
        else:
            run = ResearchRun(topic_id=topic_id, status=RunStatus.WAITING.value, progress=0, message="Manual refresh queued")
            session.add(run)
            session.flush()
            run_id = run.id
            active_id = None
    if active_id is not None:
        ensure_run_enqueued(active_id, reason="manual refresh")
        with session_scope() as session:
            return session.get(ResearchRun, active_id)
    task_id = enqueue_run(run_id, reason="manual refresh")
    if not task_id:
        raise HTTPException(
            status_code=503,
            detail=f"Run {run_id} was created but could not enter the Celery queue",
        )
    with session_scope() as session:
        return session.get(ResearchRun, run_id)


@app.get("/api/runs", response_model=list[RunOut])
def list_runs(topic_id: int | None = None, limit: int = Query(default=50, ge=1, le=200)) -> list[ResearchRun]:
    with session_scope() as session:
        stmt = select(ResearchRun).order_by(ResearchRun.created_at.desc()).limit(limit)
        if topic_id is not None:
            stmt = stmt.where(ResearchRun.topic_id == topic_id)
        return list(session.scalars(stmt))


@app.get("/api/runs/{run_id}", response_model=RunOut)
def get_run(run_id: int) -> ResearchRun:
    with session_scope() as session:
        run = session.get(ResearchRun, run_id)
        if not run:
            raise HTTPException(404, "Run not found")
        return run


@app.get("/api/runs/{run_id}/evidence", response_model=list[SourceOut])
def get_run_evidence(run_id: int) -> list[Source]:
    with session_scope() as session:
        if not session.get(ResearchRun, run_id):
            raise HTTPException(404, "Run not found")
        stmt = (
            select(Source)
            .join(RunEvidence, RunEvidence.source_id == Source.id)
            .where(RunEvidence.run_id == run_id)
            .order_by(Source.retrieved_at.desc())
        )
        return list(session.scalars(stmt))


@app.post("/api/runs/{run_id}/retry", response_model=RunOut)
def retry_run(run_id: int) -> ResearchRun:
    with session_scope() as session:
        run = session.get(ResearchRun, run_id)
        if not run:
            raise HTTPException(404, "Run not found")
        if run.status != RunStatus.FAILED.value:
            raise HTTPException(409, "Only FAILED runs can be retried")
        run.status = RunStatus.WAITING.value
        run.progress = max(0, min(run.progress, 80))
        run.message = "Manual retry queued; existing evidence and chunk cache retained"
        run.error = ""
        run.finished_at = None
    enqueue_run(run_id, reason="manual failed-run retry")
    with session_scope() as session:
        return session.get(ResearchRun, run_id)


@app.post("/api/runs/{run_id}/recover", response_model=RunOut)
def recover_run(run_id: int) -> ResearchRun:
    with session_scope() as session:
        run = session.get(ResearchRun, run_id)
        if not run:
            raise HTTPException(404, "Run not found")
        if run.status in {RunStatus.COMPLETED.value, RunStatus.FAILED.value}:
            raise HTTPException(409, "Only active/stale runs can be recovered")
    state = ensure_run_enqueued(run_id, reason="manual recovery button")
    if state == "running":
        raise HTTPException(409, "Run is actively processing; recovery is not needed")
    with session_scope() as session:
        return session.get(ResearchRun, run_id)


@app.get("/api/sources", response_model=list[SourceOut])
def list_sources(topic_id: int | None = None, limit: int = Query(default=100, ge=1, le=500)) -> list[Source]:
    with session_scope() as session:
        stmt = select(Source).order_by(Source.retrieved_at.desc()).limit(limit)
        if topic_id is not None:
            stmt = stmt.where(Source.topic_id == topic_id)
        return list(session.scalars(stmt))


@app.get("/api/sources/{source_id}")
def get_source(source_id: int) -> dict:
    with session_scope() as session:
        source = session.get(Source, source_id)
        if not source:
            raise HTTPException(404, "Source not found")
        return {
            "id": source.id,
            "topic_id": source.topic_id,
            "url": source.url,
            "canonical_url": source.canonical_url,
            "title": source.title,
            "content": source.content,
            "source_time": source.source_time,
            "retrieved_at": source.retrieved_at,
            "mime_type": source.mime_type,
            "storage_path": source.storage_path,
            "metadata": source.metadata_json,
        }


@app.get("/api/claims", response_model=list[ClaimOut])
def list_claims(
    topic_id: int | None = None,
    origin: str | None = None,
    status: str = "active",
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[Claim]:
    with session_scope() as session:
        stmt = select(Claim).order_by(Claim.priority.desc(), Claim.importance.desc(), Claim.updated_at.desc()).limit(limit)
        if topic_id is not None:
            stmt = stmt.where(Claim.topic_id == topic_id)
        if origin:
            stmt = stmt.where(Claim.origin == origin)
        if status:
            stmt = stmt.where(Claim.status == status)
        return list(session.scalars(stmt))


@app.get("/api/claims/{claim_id}/evidence", response_model=list[SourceOut])
def get_claim_evidence(claim_id: int) -> list[Source]:
    with session_scope() as session:
        if not session.get(Claim, claim_id):
            raise HTTPException(404, "Claim not found")
        stmt = (
            select(Source)
            .join(ClaimEvidence, ClaimEvidence.source_id == Source.id)
            .where(ClaimEvidence.claim_id == claim_id)
            .order_by(Source.retrieved_at.desc())
        )
        return list(session.scalars(stmt))


@app.post("/api/claims/manual", response_model=ClaimOut)
def create_manual_claim(payload: ManualClaimCreate) -> Claim:
    with session_scope() as session:
        if not session.get(Topic, payload.topic_id):
            raise HTTPException(404, "Topic not found")
        normalized = normalize_space(payload.claim_text)
        content_hash = sha256_text(normalized.casefold())
        existing = session.scalar(
            select(Claim).where(Claim.topic_id == payload.topic_id, Claim.content_hash == content_hash)
        )
        if existing:
            record_version(session, "claim", existing.id, "human", {"before_manual_confirm": existing.claim_text, "priority": existing.priority})
            existing.priority = 100
            existing.origin = "manual"
            existing.confidence = max(existing.confidence, payload.confidence)
            existing.importance = max(existing.importance, payload.importance)
            existing.status = "active"
            return existing
        claim = Claim(
            topic_id=payload.topic_id,
            claim_text=normalized,
            category=payload.category,
            claim_type="fact",
            event_time=payload.event_time,
            confidence=payload.confidence,
            importance=payload.importance,
            priority=100,
            origin="manual",
            content_hash=content_hash,
        )
        session.add(claim)
        session.flush()
        record_version(session, "claim", claim.id, "human", {"created_manual": True, "claim_text": claim.claim_text})
        return claim


@app.patch("/api/claims/{claim_id}", response_model=ClaimOut)
def update_claim(claim_id: int, payload: ManualClaimUpdate) -> Claim:
    with session_scope() as session:
        claim = session.get(Claim, claim_id)
        if not claim:
            raise HTTPException(404, "Claim not found")
        record_version(
            session,
            "claim",
            claim.id,
            "human",
            {
                "claim_text": claim.claim_text,
                "category": claim.category,
                "event_time": claim.event_time,
                "confidence": claim.confidence,
                "importance": claim.importance,
                "priority": claim.priority,
                "origin": claim.origin,
                "status": claim.status,
            },
        )
        for key, value in payload.model_dump(exclude_none=True).items():
            setattr(claim, key, value)
        if payload.claim_text is not None:
            normalized = normalize_space(payload.claim_text)
            new_hash = sha256_text(normalized.casefold())
            duplicate = session.scalar(
                select(Claim).where(Claim.topic_id == claim.topic_id, Claim.content_hash == new_hash, Claim.id != claim.id)
            )
            if duplicate:
                raise HTTPException(409, "Another claim with the same normalized text already exists")
            claim.claim_text = normalized
            claim.content_hash = new_hash
        claim.priority = max(claim.priority, 80)
        claim.origin = "manual"
        return claim


@app.post("/api/manual-notes")
def create_manual_note(payload: ManualNoteCreate) -> dict:
    queued_run_id: int | None = None
    with session_scope() as session:
        topic = session.scalar(select(Topic).where(Topic.id == payload.topic_id).with_for_update())
        if not topic:
            raise HTTPException(404, "Topic not found")
        note = ManualNote(topic_id=payload.topic_id, title=payload.title, content=payload.content, priority=100)
        session.add(note)
        session.flush()
        note_id = note.id
        active = session.scalar(
            select(ResearchRun).where(
                ResearchRun.topic_id == payload.topic_id,
                ResearchRun.status.not_in([RunStatus.COMPLETED.value, RunStatus.FAILED.value]),
            )
        )
        if not active:
            run = ResearchRun(
                topic_id=payload.topic_id,
                status=RunStatus.WAITING.value,
                progress=0,
                message="Manual input queued for AI organization",
            )
            session.add(run)
            session.flush()
            queued_run_id = run.id
    if queued_run_id:
        enqueue_run(queued_run_id, reason="manual note")
    elif active:
        ensure_run_enqueued(active.id, reason="manual note found stale active run")
    return {"id": note_id, "topic_id": payload.topic_id, "title": payload.title, "priority": 100, "queued_run_id": queued_run_id}


@app.get("/api/conflicts", response_model=list[ConflictOut])
def list_conflicts(topic_id: int | None = None, status: str = "open") -> list[Conflict]:
    with session_scope() as session:
        stmt = select(Conflict).order_by(Conflict.created_at.desc())
        if topic_id is not None:
            stmt = stmt.where(Conflict.topic_id == topic_id)
        if status:
            stmt = stmt.where(Conflict.status == status)
        return list(session.scalars(stmt.limit(500)))


@app.post("/api/conflicts/{conflict_id}/resolve", response_model=ConflictOut)
def resolve_conflict(conflict_id: int, payload: ConflictResolve) -> Conflict:
    with session_scope() as session:
        conflict = session.get(Conflict, conflict_id)
        if not conflict:
            raise HTTPException(404, "Conflict not found")
        conflict.status = "resolved"
        conflict.resolution = payload.resolution
        conflict.resolved_at = utcnow()
        if payload.winning_claim_id:
            if payload.winning_claim_id not in {conflict.claim_a_id, conflict.claim_b_id}:
                raise HTTPException(400, "Winning claim must be one of the two claims in this conflict")
            winner = session.get(Claim, payload.winning_claim_id)
            if not winner or winner.topic_id != conflict.topic_id:
                raise HTTPException(400, "Winning claim is invalid for this conflict")
            winner.priority = max(winner.priority, 100)
            winner.origin = "manual"
            losing_id = conflict.claim_b_id if winner.id == conflict.claim_a_id else conflict.claim_a_id
            if losing_id:
                loser = session.get(Claim, losing_id)
                if loser and loser.priority < winner.priority:
                    loser.status = "superseded"
        return conflict


@app.get("/api/watches", response_model=list[WebsiteWatchOut])
def list_watches(topic_id: int | None = None) -> list[WebsiteWatch]:
    with session_scope() as session:
        stmt = select(WebsiteWatch).order_by(WebsiteWatch.id.asc())
        if topic_id is not None:
            stmt = stmt.where(WebsiteWatch.topic_id == topic_id)
        return list(session.scalars(stmt))


@app.post("/api/watches", response_model=WebsiteWatchOut)
def create_watch(payload: WebsiteWatchCreate) -> WebsiteWatch:
    with session_scope() as session:
        if not session.get(Topic, payload.topic_id):
            raise HTTPException(404, "Topic not found")
        existing = session.scalar(
            select(WebsiteWatch).where(WebsiteWatch.topic_id == payload.topic_id, WebsiteWatch.url == payload.url)
        )
        if existing:
            existing.enabled = payload.enabled
            return existing
        watch = WebsiteWatch(**payload.model_dump())
        session.add(watch)
        session.flush()
        return watch


@app.delete("/api/watches/{watch_id}")
def delete_watch(watch_id: int) -> dict:
    with session_scope() as session:
        watch = session.get(WebsiteWatch, watch_id)
        if not watch:
            raise HTTPException(404, "Watch not found")
        session.delete(watch)
        return {"deleted": watch_id}


@app.get("/api/export/handoff")
def export_handoff():
    path = build_handoff_markdown()
    return FileResponse(path, media_type="text/markdown; charset=utf-8", filename=path.name)


@app.get("/api/graph")
def graph(topic_id: int | None = None, limit: int = Query(default=300, ge=1, le=1000)) -> dict:
    with session_scope() as session:
        stmt = select(Relation).order_by(Relation.confidence.desc()).limit(limit)
        if topic_id is not None:
            stmt = stmt.where(Relation.topic_id == topic_id)
        relations = list(session.scalars(stmt))
        entity_ids = {r.subject_entity_id for r in relations} | {r.object_entity_id for r in relations}
        entities = list(session.scalars(select(Entity).where(Entity.id.in_(entity_ids)))) if entity_ids else []
    return {
        "nodes": [
            {"id": e.id, "name": e.name, "type": e.entity_type, "confidence": e.confidence, "priority": e.priority}
            for e in entities
        ],
        "edges": [
            {
                "id": r.id,
                "source": r.subject_entity_id,
                "target": r.object_entity_id,
                "label": r.predicate,
                "confidence": r.confidence,
            }
            for r in relations
        ],
    }


# BEGIN INTERNETBOARD V4.7 OBSERVABILITY
# Production observability added after dd0795a. This block is intentionally
# dependency-light so a failed refresh can be diagnosed from container logs.
import logging as _ib_logging
import os as _ib_os
import time as _ib_time
from fastapi import Request as _IBRequest

_ib_trace_logger = _ib_logging.getLogger("internetboard.refresh")


@app.get("/api/build")
def internetboard_build_info():
    return {
        "service": "internetboard-backend",
        "build_sha": _ib_os.environ.get("INTERNETBOARD_BUILD_SHA", "unknown"),
        "build_time": _ib_os.environ.get("INTERNETBOARD_BUILD_TIME", "unknown"),
        "release": "v4.9-serial-queue",
    }


@app.middleware("http")
async def internetboard_refresh_trace(request: _IBRequest, call_next):
    path = request.url.path
    trace = (
        request.method == "POST"
        and path.startswith("/api/topics/")
        and path.endswith("/run")
    )
    started = _ib_time.monotonic()
    if trace:
        _ib_trace_logger.info("manual-refresh request received path=%s", path)
    try:
        response = await call_next(request)
    except Exception:
        if trace:
            _ib_trace_logger.exception("manual-refresh request failed path=%s", path)
        raise
    if trace:
        elapsed_ms = int((_ib_time.monotonic() - started) * 1000)
        _ib_trace_logger.info(
            "manual-refresh response path=%s status=%s elapsed_ms=%s",
            path,
            response.status_code,
            elapsed_ms,
        )
        response.headers["X-InternetBoard-Build"] = _ib_os.environ.get(
            "INTERNETBOARD_BUILD_SHA", "unknown"
        )
    return response
# END INTERNETBOARD V4.7 OBSERVABILITY


# BEGIN INTERNETBOARD V4.8 INTELLIGENCE ROUTER
from .intelligence import router as intelligence_router

app.include_router(intelligence_router)
# END INTERNETBOARD V4.8 INTELLIGENCE ROUTER
