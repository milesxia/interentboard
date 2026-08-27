from __future__ import annotations

import logging
import socket
import threading
from datetime import timedelta
from uuid import uuid4

from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_ready, worker_shutdown
from sqlalchemy import select

from .config import settings
from .db import init_db, session_scope
from .fetcher import fetch_document
from .models import ResearchRun, RunStatus, Topic, WebsiteWatch, utcnow
from .pipeline import execute_research_run, mark_run_failed
from .runtime import (
    RunLease,
    clear_run_queued,
    clear_transient_runtime_state,
    clear_worker,
    reserve_run_queue,
    run_runtime_state,
    set_run_queued,
    touch_worker,
)

logger = logging.getLogger(__name__)
celery_app = Celery("internetboard", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    timezone=settings.timezone,
    enable_utc=True,
    task_ignore_result=True,
    task_store_errors_even_if_ignored=False,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_reject_on_worker_lost=True,
    worker_cancel_long_running_tasks_on_connection_loss=True,
    broker_connection_retry_on_startup=True,
    broker_connection_max_retries=None,
    broker_transport_options={
        "visibility_timeout": 21600,
        "health_check_interval": 25,
        "socket_keepalive": True,
        "retry_on_timeout": True,
    },
    result_backend_transport_options={"visibility_timeout": 21600},
    result_expires=86400,
    beat_schedule={
        "daily-research-0300": {
            "task": "internetboard.run_all_topics",
            "schedule": crontab(hour=settings.scheduler_hour, minute=settings.scheduler_minute),
        },
        "website-watch": {
            "task": "internetboard.check_website_watches",
            "schedule": timedelta(minutes=settings.website_watch_minutes),
        },
        "runtime-watchdog": {
            "task": "internetboard.runtime_watchdog",
            "schedule": timedelta(seconds=settings.runtime_watchdog_seconds),
        },
    },
)

_ACTIVE = [
    RunStatus.WAITING.value,
    RunStatus.SEARCHING.value,
    RunStatus.FETCHING.value,
    RunStatus.CHUNKING.value,
    RunStatus.AI_ANALYSIS.value,
    RunStatus.KNOWLEDGE_UPDATE.value,
]
_worker_stop = threading.Event()
_worker_thread: threading.Thread | None = None


def _worker_name(sender=None) -> str:
    return str(getattr(sender, "hostname", "") or f"celery@{socket.gethostname()}")


def _worker_heartbeat_loop(name: str) -> None:
    while not _worker_stop.is_set():
        touch_worker(name)
        _worker_stop.wait(max(5, settings.worker_heartbeat_interval_seconds))


@worker_ready.connect
def _on_worker_ready(sender=None, **kwargs) -> None:
    global _worker_thread
    name = _worker_name(sender)
    _worker_stop.clear()

    reset = clear_transient_runtime_state()
    logger.info("Worker startup transient runtime reset: %s", reset)

    touch_worker(name)
    if not _worker_thread or not _worker_thread.is_alive():
        _worker_thread = threading.Thread(target=_worker_heartbeat_loop, args=(name,), name="worker-runtime-heartbeat", daemon=True)
        _worker_thread.start()
    try:
        recovered = recover_stale_runs_once(reason="worker startup recovery")
        logger.info("Worker startup recovery result: %s", recovered)
    except Exception:
        logger.exception("Runtime recovery failed during worker startup")


@worker_shutdown.connect
def _on_worker_shutdown(sender=None, **kwargs) -> None:
    _worker_stop.set()
    clear_worker()


def enqueue_run(run_id: int, *, reason: str = "queued", ttl_seconds: int | None = None) -> str | None:
    task_id = str(uuid4())
    if not reserve_run_queue(run_id, task_id, ttl_seconds=ttl_seconds):
        return None
    try:
        run_research_task.apply_async(args=[run_id], task_id=task_id)
    except Exception:
        clear_run_queued(run_id, task_id)
        raise
    logger.info("Research run %s queued as Celery task %s (%s)", run_id, task_id, reason)
    return task_id


def ensure_run_enqueued(run_id: int, *, reason: str = "runtime recovery") -> str:
    state = run_runtime_state(run_id)
    if state in {"running", "queued"}:
        return state
    with session_scope() as session:
        run = session.get(ResearchRun, run_id)
        if not run:
            return "missing"
        if run.status in {RunStatus.COMPLETED.value, RunStatus.FAILED.value}:
            return "terminal"
        previous = run.status
        run.status = RunStatus.WAITING.value
        run.message = f"Recovered stale {previous} task; re-queued automatically ({reason})"
        run.error = ""
        run.finished_at = None
    task_id = enqueue_run(run_id, reason=reason)
    return "requeued" if task_id else run_runtime_state(run_id)


def recover_stale_runs_once(*, reason: str = "watchdog") -> dict:
    init_db()
    with session_scope() as session:
        active_ids = list(session.scalars(select(ResearchRun.id).where(ResearchRun.status.in_(_ACTIVE))))
    recovered: list[int] = []
    live: list[int] = []
    queued: list[int] = []
    for run_id in active_ids:
        state = run_runtime_state(run_id)
        if state == "running":
            live.append(run_id)
            continue
        if state == "queued":
            queued.append(run_id)
            continue
        result = ensure_run_enqueued(run_id, reason=reason)
        if result == "requeued":
            recovered.append(run_id)
    return {"active": active_ids, "live": live, "queued": queued, "recovered": recovered}


@celery_app.task(name="internetboard.run_research", bind=True, max_retries=settings.max_run_retries)
def run_research_task(self, run_id: int) -> dict:
    init_db()
    task_id = str(self.request.id or uuid4())
    clear_run_queued(run_id, task_id)
    with session_scope() as session:
        run = session.get(ResearchRun, run_id)
        if not run:
            return {"run_id": run_id, "status": "MISSING"}
        if run.status in {RunStatus.COMPLETED.value, RunStatus.FAILED.value}:
            logger.info("Skipping terminal run %s status=%s task=%s", run_id, run.status, task_id)
            return {"run_id": run_id, "status": run.status, "skipped": True}
    lease = RunLease(run_id=run_id, token=task_id, worker_name=f"celery@{socket.gethostname()}")
    if not lease.acquire():
        logger.warning("Skipping duplicate execution for run %s task=%s because another lease is active", run_id, task_id)
        return {"run_id": run_id, "status": "DUPLICATE_SKIPPED"}
    try:
        execute_research_run(run_id)
        return {"run_id": run_id, "status": "COMPLETED"}
    except Exception as exc:
        logger.exception("Research run %s failed", run_id)
        with session_scope() as session:
            run = session.get(ResearchRun, run_id)
            if run:
                run.retry_count += 1
                retry_count = run.retry_count
            else:
                retry_count = settings.max_run_retries + 1
        mark_run_failed(run_id, str(exc))
        if retry_count <= settings.max_run_retries:
            countdown = min(600, 60 * (2**retry_count))
            with session_scope() as session:
                run = session.get(ResearchRun, run_id)
                if run:
                    run.status = RunStatus.WAITING.value
                    run.message = f"Retry scheduled after failure ({retry_count}/{settings.max_run_retries})"
                    run.finished_at = None
            set_run_queued(run_id, task_id, ttl_seconds=countdown + settings.run_queue_marker_ttl_seconds)
            raise self.retry(exc=exc, countdown=countdown)
        raise
    finally:
        lease.release()


@celery_app.task(name="internetboard.runtime_watchdog")
def runtime_watchdog() -> dict:
    return recover_stale_runs_once(reason="periodic watchdog")


@celery_app.task(name="internetboard.run_all_topics")
def run_all_topics() -> dict:
    init_db()
    queued_ids: list[int] = []
    existing_ids: list[int] = []
    with session_scope() as session:
        topics = list(session.scalars(select(Topic).where(Topic.enabled.is_(True)).order_by(Topic.priority.desc())))
        for topic_row in topics:
            topic = session.scalar(select(Topic).where(Topic.id == topic_row.id).with_for_update())
            active = session.scalar(select(ResearchRun).where(ResearchRun.topic_id == topic.id, ResearchRun.status.in_(_ACTIVE)))
            if active:
                existing_ids.append(active.id)
                continue
            run = ResearchRun(topic_id=topic.id, status=RunStatus.WAITING.value, progress=0, message="Daily scheduled run")
            session.add(run)
            session.flush()
            queued_ids.append(run.id)
    queued: list[int] = []
    recovered: list[int] = []
    for run_id in queued_ids:
        if enqueue_run(run_id, reason="daily schedule"):
            queued.append(run_id)
    for run_id in existing_ids:
        if ensure_run_enqueued(run_id, reason="daily schedule found stale active run") == "requeued":
            recovered.append(run_id)
    return {"queued": queued, "recovered": recovered}


@celery_app.task(name="internetboard.check_website_watches")
def check_website_watches() -> dict:
    init_db()
    changed_topics: set[int] = set()
    checked = 0
    with session_scope() as session:
        watches = list(session.scalars(select(WebsiteWatch).where(WebsiteWatch.enabled.is_(True))))
    for watch in watches:
        try:
            doc = fetch_document(watch.url)
            checked += 1
            with session_scope() as session:
                current = session.get(WebsiteWatch, watch.id)
                if not current:
                    continue
                previous = current.last_hash
                current.last_hash = doc.content_hash
                current.last_checked_at = utcnow()
                if previous and previous != doc.content_hash:
                    current.last_changed_at = utcnow()
                    changed_topics.add(current.topic_id)
        except Exception as exc:
            logger.warning("Website watch failed %s: %s", watch.url, exc)
    new_ids: list[int] = []
    existing_ids: list[int] = []
    with session_scope() as session:
        for topic_id in changed_topics:
            topic = session.scalar(select(Topic).where(Topic.id == topic_id).with_for_update())
            if not topic:
                continue
            active = session.scalar(select(ResearchRun).where(ResearchRun.topic_id == topic_id, ResearchRun.status.in_(_ACTIVE)))
            if active:
                existing_ids.append(active.id)
                continue
            run = ResearchRun(topic_id=topic_id, status=RunStatus.WAITING.value, progress=0, message="Website change detected")
            session.add(run)
            session.flush()
            new_ids.append(run.id)
    queued: list[int] = []
    recovered: list[int] = []
    for run_id in new_ids:
        if enqueue_run(run_id, reason="website change"):
            queued.append(run_id)
    for run_id in existing_ids:
        if ensure_run_enqueued(run_id, reason="website watch found stale active run") == "requeued":
            recovered.append(run_id)
    return {"checked": checked, "changed_topics": sorted(changed_topics), "queued": queued, "recovered": recovered}


# BEGIN INTERNETBOARD V4.7 ENQUEUE TRACE
# Wrap the existing, already-tested enqueue implementation. Runtime callers
# resolve this global name when they execute, so main.py, watchdog and schedule
# paths all receive consistent enqueue diagnostics without changing queue logic.
import logging as _ib_enqueue_logging

_ib_enqueue_logger = _ib_enqueue_logging.getLogger("internetboard.enqueue")
_ib_enqueue_run_impl = enqueue_run


def enqueue_run(run_id: int, *, reason: str = "queued", ttl_seconds: int | None = None) -> str | None:
    _ib_enqueue_logger.info("enqueue request run_id=%s reason=%s", run_id, reason)
    try:
        task_id = _ib_enqueue_run_impl(run_id, reason=reason, ttl_seconds=ttl_seconds)
    except Exception:
        _ib_enqueue_logger.exception("enqueue failed run_id=%s reason=%s", run_id, reason)
        raise
    if task_id:
        _ib_enqueue_logger.info("enqueue accepted run_id=%s task_id=%s reason=%s", run_id, task_id, reason)
    else:
        _ib_enqueue_logger.warning("enqueue returned empty run_id=%s reason=%s", run_id, reason)
    return task_id
# END INTERNETBOARD V4.7 ENQUEUE TRACE


# BEGIN INTERNETBOARD V4.9 SERIAL QUEUE
# Heavy research runs are serialized on the research queue. Control/watchdog
# tasks stay independently consumable so a stuck Qwen run cannot blind monitoring.
import os as _v49_os
from .queue_runtime import monitor_stalled_runs as _v49_monitor_stalled_runs
from .queue_runtime import record_enqueued_task as _v49_record_enqueued_task

celery_app.conf.task_default_queue = "control"
celery_app.conf.task_routes = {
    "internetboard.run_research": {"queue": "research"},
    "internetboard.run_all_topics": {"queue": "control"},
    "internetboard.check_website_watches": {"queue": "control"},
    "internetboard.runtime_watchdog": {"queue": "control"},
    "internetboard.queue_watchdog_v49": {"queue": "control"},
    "internetboard.requeue_stalled_v49": {"queue": "control"},
}

_v49_beat = dict(celery_app.conf.beat_schedule or {})
_v49_beat["v49-progress-watchdog"] = {
    "task": "internetboard.queue_watchdog_v49",
    "schedule": float(_v49_os.getenv("QUEUE_WATCHDOG_SECONDS", "60")),
    "options": {"queue": "control"},
}
celery_app.conf.beat_schedule = _v49_beat

# Wrap the V4.7 enqueue implementation only to persist the task id used by
# progress recovery. Existing queue marker / locking semantics stay intact.
_v49_enqueue_impl = enqueue_run


def enqueue_run(run_id: int, *, reason: str = "queued", ttl_seconds: int | None = None) -> str | None:
    task_id = _v49_enqueue_impl(run_id, reason=reason, ttl_seconds=ttl_seconds)
    _v49_record_enqueued_task(run_id, task_id)
    return task_id


@celery_app.task(name="internetboard.queue_watchdog_v49")
def queue_watchdog_v49() -> dict:
    return _v49_monitor_stalled_runs(celery_app)


@celery_app.task(name="internetboard.requeue_stalled_v49")
def requeue_stalled_v49(run_id: int, attempt: int = 1) -> dict:
    result = ensure_run_enqueued(run_id, reason=f"v4.9 stalled recovery {attempt}")
    return {"run_id": run_id, "attempt": attempt, "enqueue_result": result}
# END INTERNETBOARD V4.9 SERIAL QUEUE
