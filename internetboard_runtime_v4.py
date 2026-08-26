#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import argparse
import re
import subprocess

ROOT = Path.cwd()
PARSER = argparse.ArgumentParser(description="InternetBoard V4: durable task runtime, self-healing queue and production diagnostics")
PARSER.add_argument("--push", action="store_true", help="commit and push patched source")
ARGS = PARSER.parse_args()


def read(path: str) -> str:
    p = ROOT / path
    if not p.exists():
        raise SystemExit(f"Missing required file: {path}")
    return p.read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    p = ROOT / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    print(f"[write] {path}")


def replace_once(path: str, old: str, new: str) -> None:
    text = read(path)
    if new in text:
        print(f"[skip] {path} already patched")
        return
    if old not in text:
        raise SystemExit(f"Could not patch {path}; expected anchor not found: {old[:180]!r}")
    write(path, text.replace(old, new, 1))


def regex_once(path: str, pattern: str, replacement: str, flags: int = 0) -> None:
    text = read(path)
    new, count = re.subn(pattern, replacement, text, count=1, flags=flags)
    if count != 1:
        raise SystemExit(f"Could not patch {path}; regex anchor count={count}: {pattern[:160]!r}")
    write(path, new)


def append_line_once(path: str, line: str) -> None:
    text = read(path)
    if line.strip() in {x.strip() for x in text.splitlines()}:
        return
    write(path, text.rstrip() + "\n" + line.rstrip() + "\n")


def run(*cmd: str) -> str:
    print("[run]", " ".join(cmd))
    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.returncode != 0:
        if proc.stderr:
            print(proc.stderr, end="")
        raise SystemExit(proc.returncode)
    return proc.stdout


# ---------------------------------------------------------------------------
# Preflight: V4 must be applied on top of V2+V3 production source.
# ---------------------------------------------------------------------------
main_now = read("backend/app/main.py")
pipeline_now = read("backend/app/pipeline.py")
frontend_now = read("frontend/app.js")
if "bootstrap_defaults_once" not in main_now or "/api/export/handoff" not in main_now:
    raise SystemExit("V4 expects the V2 production fix (bootstrap + handoff export).")
if "extract_visual_assets" not in pipeline_now or "analyze_visual" not in read("backend/app/ollama_client.py"):
    raise SystemExit("V4 expects the V3 visual-evidence pipeline first.")
if "INTERNETBOARD_API_KEY" in main_now or "ensureKey" in frontend_now:
    raise SystemExit("V4 expects the API-key-free trusted-LAN profile from V2.")


# ---------------------------------------------------------------------------
# Runtime: Redis-backed queue markers, run leases, heartbeats and diagnostics.
# No DB schema migration is required, so existing PostgreSQL remains compatible.
# ---------------------------------------------------------------------------
runtime_py = r'''from __future__ import annotations

import json
import os
import socket
import threading
import time
from dataclasses import dataclass

from redis import Redis
from redis.exceptions import RedisError

from .config import settings

_PREFIX = "internetboard:runtime"
_WORKER_KEY = f"{_PREFIX}:worker:last"
_LOCK_REFRESH = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""
_LOCK_DELETE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""
_QUEUE_DELETE = _LOCK_DELETE


def redis_client() -> Redis:
    return Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
        health_check_interval=15,
        retry_on_timeout=True,
    )


def _run_heartbeat_key(run_id: int) -> str:
    return f"{_PREFIX}:run:{run_id}:heartbeat"


def _run_lock_key(run_id: int) -> str:
    return f"{_PREFIX}:run:{run_id}:lock"


def _run_queue_key(run_id: int) -> str:
    return f"{_PREFIX}:run:{run_id}:queued"


def touch_worker(worker_name: str | None = None) -> None:
    payload = {
        "worker": worker_name or socket.gethostname(),
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "ts": time.time(),
    }
    try:
        redis_client().setex(
            _WORKER_KEY,
            settings.worker_heartbeat_ttl_seconds,
            json.dumps(payload, ensure_ascii=False),
        )
    except RedisError:
        pass


def clear_worker() -> None:
    try:
        redis_client().delete(_WORKER_KEY)
    except RedisError:
        pass


def touch_run(run_id: int) -> None:
    try:
        redis_client().setex(
            _run_heartbeat_key(run_id),
            settings.run_heartbeat_ttl_seconds,
            str(time.time()),
        )
    except RedisError:
        pass


def clear_run_heartbeat(run_id: int) -> None:
    try:
        redis_client().delete(_run_heartbeat_key(run_id))
    except RedisError:
        pass


def reserve_run_queue(run_id: int, task_id: str, ttl_seconds: int | None = None) -> bool:
    ttl = max(30, int(ttl_seconds or settings.run_queue_marker_ttl_seconds))
    try:
        return bool(redis_client().set(_run_queue_key(run_id), task_id, nx=True, ex=ttl))
    except RedisError:
        # Publishing a task is still preferable to silently losing it if Redis metadata
        # briefly fails. Celery itself will report broker failure if Redis is truly down.
        return True


def set_run_queued(run_id: int, task_id: str, ttl_seconds: int | None = None) -> None:
    ttl = max(30, int(ttl_seconds or settings.run_queue_marker_ttl_seconds))
    try:
        redis_client().setex(_run_queue_key(run_id), ttl, task_id)
    except RedisError:
        pass


def clear_run_queued(run_id: int, task_id: str | None = None) -> None:
    try:
        client = redis_client()
        if task_id:
            client.eval(_QUEUE_DELETE, 1, _run_queue_key(run_id), task_id)
        else:
            client.delete(_run_queue_key(run_id))
    except RedisError:
        pass


def run_runtime_state(run_id: int, terminal: bool = False) -> str:
    if terminal:
        return "terminal"
    try:
        client = redis_client()
        if client.exists(_run_heartbeat_key(run_id)) or client.exists(_run_lock_key(run_id)):
            return "running"
        if client.exists(_run_queue_key(run_id)):
            return "queued"
    except RedisError:
        return "unknown"
    return "stale"


def runtime_snapshot() -> dict:
    out = {
        "broker_ok": False,
        "worker_online": False,
        "worker": "",
        "worker_last_seen_seconds": None,
        "queue_depth": None,
        "vm_overcommit_memory": None,
    }
    try:
        client = redis_client()
        out["broker_ok"] = bool(client.ping())
        out["queue_depth"] = int(client.llen("celery"))
        raw = client.get(_WORKER_KEY)
        if raw:
            payload = json.loads(raw)
            out["worker_online"] = True
            out["worker"] = str(payload.get("worker") or payload.get("host") or "worker")
            ts = float(payload.get("ts") or 0)
            out["worker_last_seen_seconds"] = max(0, round(time.time() - ts, 1)) if ts else None
    except Exception as exc:
        out["error"] = str(exc)
    try:
        out["vm_overcommit_memory"] = int(open("/proc/sys/vm/overcommit_memory", "r", encoding="utf-8").read().strip())
    except Exception:
        pass
    return out


@dataclass
class RunLease:
    run_id: int
    token: str
    worker_name: str = ""

    def __post_init__(self) -> None:
        self.acquired = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def acquire(self) -> bool:
        try:
            client = redis_client()
            self.acquired = bool(
                client.set(
                    _run_lock_key(self.run_id),
                    self.token,
                    nx=True,
                    ex=settings.run_lock_ttl_seconds,
                )
            )
        except RedisError:
            self.acquired = False
        if not self.acquired:
            return False
        clear_run_queued(self.run_id, self.token)
        touch_run(self.run_id)
        touch_worker(self.worker_name)
        self._thread = threading.Thread(target=self._keepalive, name=f"run-lease-{self.run_id}", daemon=True)
        self._thread.start()
        return True

    def _keepalive(self) -> None:
        interval = max(5, settings.run_heartbeat_interval_seconds)
        while not self._stop.wait(interval):
            try:
                client = redis_client()
                refreshed = client.eval(
                    _LOCK_REFRESH,
                    1,
                    _run_lock_key(self.run_id),
                    self.token,
                    settings.run_lock_ttl_seconds,
                )
                if not refreshed:
                    return
                client.setex(
                    _run_heartbeat_key(self.run_id),
                    settings.run_heartbeat_ttl_seconds,
                    str(time.time()),
                )
                touch_worker(self.worker_name)
            except RedisError:
                # DB stages and Celery retry/recovery remain the source of truth.
                continue

    def release(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        try:
            redis_client().eval(_LOCK_DELETE, 1, _run_lock_key(self.run_id), self.token)
        except RedisError:
            pass
        clear_run_heartbeat(self.run_id)
        self.acquired = False

    def __enter__(self) -> "RunLease":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
'''
write("backend/app/runtime.py", runtime_py)


# ---------------------------------------------------------------------------
# Config: conservative runtime defaults for one heavy worker on TS-673A.
# ---------------------------------------------------------------------------
config = read("backend/app/config.py")
if "run_heartbeat_interval_seconds" not in config:
    anchor = "    max_run_retries: int = 2\n"
    replacement = '''    max_run_retries: int = 2
    run_heartbeat_interval_seconds: int = 15
    run_heartbeat_ttl_seconds: int = 60
    run_lock_ttl_seconds: int = 90
    run_queue_marker_ttl_seconds: int = 180
    worker_heartbeat_interval_seconds: int = 15
    worker_heartbeat_ttl_seconds: int = 50
    runtime_watchdog_seconds: int = 60
'''
    if anchor not in config:
        raise SystemExit("Could not patch runtime settings in backend/app/config.py")
    write("backend/app/config.py", config.replace(anchor, replacement, 1))


# ---------------------------------------------------------------------------
# Tasks: durable enqueue markers, per-run Redis lease, worker heartbeat,
# self-healing stale runs and safer Redis broker transport settings.
# ---------------------------------------------------------------------------
tasks_py = r'''from __future__ import annotations

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
    touch_worker(name)
    if not _worker_thread or not _worker_thread.is_alive():
        _worker_thread = threading.Thread(target=_worker_heartbeat_loop, args=(name,), name="worker-runtime-heartbeat", daemon=True)
        _worker_thread.start()
    # Any task stranded by a previous worker/container restart is recovered here.
    try:
        recover_stale_runs_once(reason="worker startup recovery")
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
'''
write("backend/app/tasks.py", tasks_py)


# ---------------------------------------------------------------------------
# Pipeline: stage transitions also touch the run heartbeat immediately.
# Background RunLease keeps it alive while long Ollama/search calls are blocking.
# Also preserve all parent sources when the same visual is reused/reposted.
# ---------------------------------------------------------------------------
pipeline = read("backend/app/pipeline.py")
if "from .runtime import touch_run" not in pipeline:
    pipeline = pipeline.replace(
        "from .ollama_client import ollama\n",
        "from .ollama_client import ollama\nfrom .runtime import touch_run\n",
        1,
    )
old_update = '''def _update_run(session: Session, run: ResearchRun, status: RunStatus, progress: int, message: str) -> None:
    run.status = status.value
    run.progress = max(0, min(100, progress))
    run.message = message[:4000]
    session.flush()
'''
new_update = '''def _update_run(session: Session, run: ResearchRun, status: RunStatus, progress: int, message: str) -> None:
    run.status = status.value
    run.progress = max(0, min(100, progress))
    run.message = message[:4000]
    session.flush()
    touch_run(run.id)
'''
if old_update in pipeline:
    pipeline = pipeline.replace(old_update, new_update, 1)
elif "touch_run(run.id)" not in pipeline:
    raise SystemExit("Could not patch pipeline heartbeat update")
# Visual provenance: same visual can be reposted by multiple parent sources.
old_existing_visual = '''            meta = source.metadata_json or {}
            if meta.get("visual_analysis_key") == cache_key:
                cached = _load_cached_visual(source)
'''
new_existing_visual = '''            meta = dict(source.metadata_json or {})
            parent_ids = {int(x) for x in meta.get("parent_source_ids", []) if str(x).isdigit()}
            if meta.get("parent_source_id"):
                parent_ids.add(int(meta["parent_source_id"]))
            parent_ids.add(parent_source_id)
            meta["parent_source_ids"] = sorted(parent_ids)
            meta["parent_source_id"] = min(parent_ids) if parent_ids else parent_source_id
            source.metadata_json = meta
            if meta.get("visual_analysis_key") == cache_key:
                cached = _load_cached_visual(source)
'''
if old_existing_visual in pipeline:
    pipeline = pipeline.replace(old_existing_visual, new_existing_visual, 1)
# New visual sources should start with the list form too.
pipeline = pipeline.replace(
    '                    "parent_source_id": parent_source_id,\n',
    '                    "parent_source_id": parent_source_id,\n                    "parent_source_ids": [parent_source_id],\n',
    1,
) if '"parent_source_ids": [parent_source_id]' not in pipeline else pipeline
write("backend/app/pipeline.py", pipeline)


# ---------------------------------------------------------------------------
# Main API: runtime visibility, stale recovery and safe manual recovery endpoint.
# ---------------------------------------------------------------------------
main = read("backend/app/main.py")
main = main.replace(
    "from .tasks import run_research_task\n",
    "from .runtime import run_runtime_state, runtime_snapshot\nfrom .tasks import enqueue_run, ensure_run_enqueued, run_research_task\n",
    1,
) if "from .runtime import run_runtime_state" not in main else main

# system_status: replace whole function block up to dashboard decorator.
status_pattern = r'@app\.get\("/api/system/status"\)\ndef system_status\(\) -> dict:\n.*?\n\n@app\.get\("/api/dashboard"\)'
status_replacement = '''@app.get("/api/system/status")
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

@app.get("/api/dashboard")'''
main_new, count = re.subn(status_pattern, status_replacement, main, count=1, flags=re.S)
if count != 1:
    raise SystemExit("Could not replace /api/system/status block")
main = main_new

# Dashboard run payloads gain runtime_state without changing DB schema.
old_dashboard_runs = '        "runs": [RunOut.model_validate(x).model_dump() for x in latest_runs],\n'
new_dashboard_runs = '''        "runs": [
            {**RunOut.model_validate(x).model_dump(), "runtime_state": run_runtime_state(x.id, terminal=x.status in {RunStatus.COMPLETED.value, RunStatus.FAILED.value})}
            for x in latest_runs
        ],
'''
if old_dashboard_runs in main:
    main = main.replace(old_dashboard_runs, new_dashboard_runs, 1)
elif '"runtime_state": run_runtime_state' not in main:
    raise SystemExit("Could not patch dashboard runtime state")

# Manual run: existing active run is no longer blindly returned; stale work is repaired.
old_run_active = '''        if active:
            return active
        run = ResearchRun(topic_id=topic_id, status=RunStatus.WAITING.value, progress=0, message="Manual refresh queued")
        session.add(run)
        session.flush()
        run_id = run.id
    run_research_task.delay(run_id)
'''
new_run_active = '''        if active:
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
    enqueue_run(run_id, reason="manual refresh")
'''
if old_run_active in main:
    main = main.replace(old_run_active, new_run_active, 1)
elif "ensure_run_enqueued(active_id" not in main:
    raise SystemExit("Could not patch manual run recovery")

# Failed-run retry must use durable enqueue marker.
main = main.replace(
    "    run_research_task.delay(run_id)\n    with session_scope() as session:\n        return session.get(ResearchRun, run_id)\n",
    "    enqueue_run(run_id, reason=\"manual failed-run retry\")\n    with session_scope() as session:\n        return session.get(ResearchRun, run_id)\n",
    1,
) if 'reason="manual failed-run retry"' not in main else main

# Add explicit recover endpoint before sources list.
if '/api/runs/{run_id}/recover' not in main:
    anchor = '@app.get("/api/sources", response_model=list[SourceOut])\n'
    recovery_endpoint = '''@app.post("/api/runs/{run_id}/recover", response_model=RunOut)
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


'''
    if anchor not in main:
        raise SystemExit("Could not insert run recovery endpoint")
    main = main.replace(anchor, recovery_endpoint + anchor, 1)

# Manual notes: if an active stale run exists, make sure it is actually queued.
old_note_tail = '''    if queued_run_id:
        run_research_task.delay(queued_run_id)
    return {"id": note_id, "topic_id": payload.topic_id, "title": payload.title, "priority": 100, "queued_run_id": queued_run_id}
'''
new_note_tail = '''    if queued_run_id:
        enqueue_run(queued_run_id, reason="manual note")
    elif active:
        ensure_run_enqueued(active.id, reason="manual note found stale active run")
    return {"id": note_id, "topic_id": payload.topic_id, "title": payload.title, "priority": 100, "queued_run_id": queued_run_id}
'''
if old_note_tail in main:
    main = main.replace(old_note_tail, new_note_tail, 1)
write("backend/app/main.py", main)


# ---------------------------------------------------------------------------
# Frontend: show actual worker/queue/stale state, adaptive polling, recovery button.
# ---------------------------------------------------------------------------
frontend = read("frontend/app.js")
old_stats = '''  $('#stats').innerHTML = [
    ['专题', c.topics || 0], ['运行中', c.active_runs || 0], ['证据', c.sources || 0], ['知识 Claim', c.claims || 0], ['未解决冲突', c.open_conflicts || 0]
  ].map(([label,value]) => `<div class="stat"><strong>${value}</strong><span>${label}</span></div>`).join('');
'''
new_stats = '''  const rt = state.system?.runtime || {};
  $('#stats').innerHTML = [
    ['专题', c.topics || 0], ['运行中', c.active_runs || 0], ['队列', rt.queue_depth ?? '-'], ['僵尸任务', (rt.stale_run_ids || []).length], ['证据', c.sources || 0], ['知识 Claim', c.claims || 0], ['未解决冲突', c.open_conflicts || 0]
  ].map(([label,value]) => `<div class="stat"><strong>${value}</strong><span>${label}</span></div>`).join('');
'''
if old_stats in frontend:
    frontend = frontend.replace(old_stats, new_stats, 1)
elif "僵尸任务" not in frontend:
    raise SystemExit("Could not patch frontend stats")
old_badge = '''  if (m.ok && m.model_ready) { b.className='badge good'; b.textContent=`${m.model} 已就绪`; }
  else if (m.ok) { b.className='badge warn'; b.textContent='Ollama 在线 / 模型拉取中'; }
  else { b.className='badge bad'; b.textContent='Ollama 未就绪'; }
'''
new_badge = '''  const rt = state.system?.runtime || {};
  if (!rt.broker_ok) { b.className='badge bad'; b.textContent='Redis/任务队列异常'; }
  else if (!rt.worker_online) { b.className='badge bad'; b.textContent='Worker 离线'; }
  else if ((rt.stale_run_ids || []).length) { b.className='badge warn'; b.textContent=`Worker 在线 · ${rt.stale_run_ids.length} 个任务待恢复`; }
  else if (m.ok && m.model_ready) { b.className='badge good'; b.textContent=`${m.model} · Worker 在线`; }
  else if (m.ok) { b.className='badge warn'; b.textContent='Ollama 在线 / 模型拉取中'; }
  else { b.className='badge bad'; b.textContent='Ollama 未就绪'; }
'''
if old_badge in frontend:
    frontend = frontend.replace(old_badge, new_badge, 1)

old_run_head = '''      <div class="item-head"><div class="item-title">Run #${r.id} · Topic ${r.topic_id}</div><span class="badge ${statusClass(r.status)}">${r.status}</span></div>
      <div class="meta">${esc(r.message || '')}</div>
'''
new_run_head = '''      <div class="item-head"><div class="item-title">Run #${r.id} · Topic ${r.topic_id}</div><span class="badge ${statusClass(r.status)}">${r.status}</span></div>
      <div class="meta">运行态：${esc(r.runtime_state || '-')} · ${esc(r.message || '')}</div>
      ${r.runtime_state === 'stale' ? `<div class="actions"><button onclick="recoverRun(${r.id})">恢复任务</button></div>` : ''}
'''
if old_run_head in frontend:
    frontend = frontend.replace(old_run_head, new_run_head, 1)
elif "recoverRun" not in frontend:
    raise SystemExit("Could not patch run runtime UI")

if "async function recoverRun" not in frontend:
    anchor = "window.runTopic = runTopic;\n"
    recovery_js = '''window.runTopic = runTopic;
async function recoverRun(id) {
  try { await api(`/api/runs/${id}/recover`, {method:'POST'}); toast('任务已恢复并重新进入队列'); await load(); } catch(e) { toast(e.message); }
}
window.recoverRun = recoverRun;
'''
    if anchor not in frontend:
        raise SystemExit("Could not insert recoverRun frontend function")
    frontend = frontend.replace(anchor, recovery_js, 1)

# Adaptive polling: 5s while work is active, 30s while idle. Avoid 5 API calls every 10s forever.
old_poll = '''load();
setInterval(() => {
  const active = (state.dashboard?.runs || []).some(r => !['COMPLETED','FAILED'].includes(r.status));
  if (active) load();
}, 10000);
'''
new_poll = '''let pollTimer = null;
async function poll() {
  await load();
  const active = (state.dashboard?.runs || []).some(r => !['COMPLETED','FAILED'].includes(r.status));
  const delay = active ? 5000 : 30000;
  clearTimeout(pollTimer);
  pollTimer = setTimeout(poll, delay);
}
poll();
'''
if old_poll in frontend:
    frontend = frontend.replace(old_poll, new_poll, 1)
elif "setTimeout(poll" not in frontend:
    raise SystemExit("Could not patch adaptive frontend polling")
write("frontend/app.js", frontend)


# ---------------------------------------------------------------------------
# Compose: runtime settings and a real Celery worker healthcheck.
# Redis overcommit is host-kernel state and cannot be safely forced by a container.
# ---------------------------------------------------------------------------
compose = read("docker-compose.yml")
if "RUN_HEARTBEAT_INTERVAL_SECONDS" not in compose:
    env_anchor = "  WEBSITE_WATCH_MINUTES: ${WEBSITE_WATCH_MINUTES}\n"
    runtime_env = '''  WEBSITE_WATCH_MINUTES: ${WEBSITE_WATCH_MINUTES}
  RUN_HEARTBEAT_INTERVAL_SECONDS: ${RUN_HEARTBEAT_INTERVAL_SECONDS:-15}
  RUN_HEARTBEAT_TTL_SECONDS: ${RUN_HEARTBEAT_TTL_SECONDS:-60}
  RUN_LOCK_TTL_SECONDS: ${RUN_LOCK_TTL_SECONDS:-90}
  RUN_QUEUE_MARKER_TTL_SECONDS: ${RUN_QUEUE_MARKER_TTL_SECONDS:-180}
  WORKER_HEARTBEAT_INTERVAL_SECONDS: ${WORKER_HEARTBEAT_INTERVAL_SECONDS:-15}
  WORKER_HEARTBEAT_TTL_SECONDS: ${WORKER_HEARTBEAT_TTL_SECONDS:-50}
  RUNTIME_WATCHDOG_SECONDS: ${RUNTIME_WATCHDOG_SECONDS:-60}
'''
    if env_anchor not in compose:
        raise SystemExit("Could not patch runtime environment in docker-compose.yml")
    compose = compose.replace(env_anchor, runtime_env, 1)
# Make Redis persistence policy explicit.
compose = compose.replace(
    'command: ["redis-server", "--appendonly", "yes", "--maxmemory-policy", "noeviction"]',
    'command: ["redis-server", "--appendonly", "yes", "--appendfsync", "everysec", "--maxmemory-policy", "noeviction"]',
    1,
)
if 'celery -A app.tasks.celery_app inspect ping' not in compose:
    worker_anchor = '    command: ["celery", "-A", "app.tasks.celery_app", "worker", "--loglevel=INFO", "--concurrency=1", "--prefetch-multiplier=1", "--max-tasks-per-child=10"]\n    networks: [internetboard]\n'
    worker_replacement = '''    command: ["celery", "-A", "app.tasks.celery_app", "worker", "--loglevel=INFO", "--concurrency=1", "--prefetch-multiplier=1", "--max-tasks-per-child=10"]
    stop_grace_period: 2m
    healthcheck:
      test: ["CMD-SHELL", "celery -A app.tasks.celery_app inspect ping -d celery@$$HOSTNAME --timeout=5 2>/dev/null | grep -q pong"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 60s
    networks: [internetboard]
'''
    if worker_anchor not in compose:
        raise SystemExit("Could not patch worker healthcheck in docker-compose.yml")
    compose = compose.replace(worker_anchor, worker_replacement, 1)
write("docker-compose.yml", compose)


# ---------------------------------------------------------------------------
# .env defaults remain upgrade-safe for existing QNAP installs.
# ---------------------------------------------------------------------------
env = read(".env.example")
for line in (
    "RUN_HEARTBEAT_INTERVAL_SECONDS=15",
    "RUN_HEARTBEAT_TTL_SECONDS=60",
    "RUN_LOCK_TTL_SECONDS=90",
    "RUN_QUEUE_MARKER_TTL_SECONDS=180",
    "WORKER_HEARTBEAT_INTERVAL_SECONDS=15",
    "WORKER_HEARTBEAT_TTL_SECONDS=50",
    "RUNTIME_WATCHDOG_SECONDS=60",
):
    if line not in env:
        env = env.rstrip() + "\n" + line + "\n"
write(".env.example", env)


# ---------------------------------------------------------------------------
# Doctor: surface the exact faults seen on this QNAP instead of guessing.
# ---------------------------------------------------------------------------
doctor = r'''#!/bin/sh
set -u
cd /share/Container/internetboard 2>/dev/null || cd "$(dirname "$0")" || exit 1
if docker compose version >/dev/null 2>&1; then COMPOSE="docker compose"; else COMPOSE="docker-compose"; fi
PORT=$(awk -F= '/^WEB_PORT=/{print $2}' .env 2>/dev/null)
echo "===== InternetBoard Doctor ====="
echo "Date: $(date)"
echo "Kernel: $(uname -a)"
echo "Memory:"; free -h 2>/dev/null || cat /proc/meminfo | head
echo "Disk:"; df -h /share/Container 2>/dev/null || true
echo "vm.overcommit_memory: $(cat /proc/sys/vm/overcommit_memory 2>/dev/null || echo unknown) (Redis recommends 1)"
echo
echo "DNS / Docker Hub registry:"
nslookup registry-1.docker.io 2>/dev/null || true
curl -4 -IsS --connect-timeout 8 --max-time 12 https://registry-1.docker.io/v2/ 2>/dev/null | head -1 || echo "Docker Hub HTTPS unavailable"
echo
echo "NVIDIA host:"; nvidia-smi 2>/dev/null || echo "host nvidia-smi unavailable"
echo "Compose:"; $COMPOSE version 2>/dev/null || true
echo "Containers:"; $COMPOSE ps 2>/dev/null || true
echo "Worker ping:"; docker exec internetboard-worker sh -lc 'celery -A app.tasks.celery_app inspect ping -d celery@$HOSTNAME --timeout=5' 2>/dev/null || echo "worker ping failed"
echo "Redis queue depth:"; docker exec internetboard-redis redis-cli llen celery 2>/dev/null || true
echo "Ollama GPU:"; docker exec internetboard-ollama nvidia-smi 2>/dev/null || echo "GPU unavailable in container"
echo "Ollama models:"; docker exec internetboard-ollama ollama list 2>/dev/null || true
echo "Ollama processor split:"; docker exec internetboard-ollama ollama ps 2>/dev/null || true
echo "Frontend health:"; curl -fsS "http://127.0.0.1:${PORT:-8733}/health" 2>/dev/null || true
echo
echo "Runtime status:"; curl -fsS "http://127.0.0.1:${PORT:-8733}/api/system/status" 2>/dev/null || true
echo
echo "Recent logs:"; $COMPOSE logs --tail=100 backend worker scheduler redis ollama 2>/dev/null || true
'''
write("doctor.sh", doctor)


# ---------------------------------------------------------------------------
# CI validator: future edits may not silently remove runtime self-healing.
# ---------------------------------------------------------------------------
validator = read("scripts/validate_production.py")
if 'runtime = text("backend/app/runtime.py")' not in validator:
    validator = validator.replace(
        'requirements = text("backend/requirements.txt")\n',
        'requirements = text("backend/requirements.txt")\nruntime = text("backend/app/runtime.py")\ntasks = text("backend/app/tasks.py")\n',
        1,
    )
checks = '''must("RunLease" in runtime and "reserve_run_queue" in runtime, "Redis run lease/queue markers are missing")
must("runtime_watchdog" in tasks and "ensure_run_enqueued" in tasks, "Task self-healing watchdog is missing")
must("worker_cancel_long_running_tasks_on_connection_loss" in tasks, "Celery broker-loss recovery setting is missing")
must("run_runtime_state" in main and "/api/runs/{run_id}/recover" in main, "Runtime status/recovery API is missing")
must("recoverRun" in frontend and "僵尸任务" in frontend, "Runtime recovery UI is missing")
must("inspect ping" in compose, "Worker Docker healthcheck is missing")
must("RUN_HEARTBEAT_INTERVAL_SECONDS" in compose, "Runtime heartbeat Compose settings are missing")
'''
if "Redis run lease/queue markers are missing" not in validator:
    validator = validator.replace('print("InternetBoard production invariants: PASS")', checks + 'print("InternetBoard production invariants: PASS")')
write("scripts/validate_production.py", validator)


# ---------------------------------------------------------------------------
# CI: syntax checks are not enough. Install production deps and import all
# runtime-critical modules so bad imports/circular dependencies fail before push.
# ---------------------------------------------------------------------------
workflow = read(".github/workflows/dockerhub.yml")
if "Import runtime modules" not in workflow:
    anchor = '''      - name: Python syntax check
        run: python -m compileall -q backend/app scripts/validate_production.py
      - name: Production invariants
'''
    replacement = '''      - name: Python syntax check
        run: python -m compileall -q backend/app scripts/validate_production.py
      - name: Install backend dependencies
        run: python -m pip install --disable-pip-version-check -q -r backend/requirements.txt
      - name: Import runtime modules
        env:
          DATABASE_URL: sqlite:////tmp/internetboard-ci.db
          REDIS_URL: redis://127.0.0.1:6379/0
        run: PYTHONPATH=backend python -c "import app.runtime, app.tasks, app.pipeline, app.main; print('runtime imports: PASS')"
      - name: Production invariants
'''
    if anchor not in workflow:
        raise SystemExit("Could not patch GitHub Actions runtime import check")
    workflow = workflow.replace(anchor, replacement, 1)
write(".github/workflows/dockerhub.yml", workflow)


# ---------------------------------------------------------------------------
# Docs: concise operational note, preserving the one-time bootstrap contract.
# ---------------------------------------------------------------------------
readme = read("README.md")
if "Runtime self-healing" not in readme:
    readme += '''\n\n## Runtime self-healing\n\nInternetBoard uses a Redis-backed per-run lease, queue marker and heartbeat. A duplicate Celery delivery cannot execute the same research run concurrently. If a worker/container or broker connection is interrupted, the worker-start recovery hook and periodic watchdog re-queue stale active runs without creating a new research history record. The dashboard shows worker state, queue depth and stale-run recovery controls.\n\n`doctor.sh` also checks Docker Hub DNS/HTTPS, Redis queue depth, Celery worker ping, `vm.overcommit_memory`, GPU visibility and Ollama processor split.\n'''
    write("README.md", readme)


# ---------------------------------------------------------------------------
# Local static validation.
# ---------------------------------------------------------------------------
run("python3", "-m", "compileall", "-q", "backend/app", "scripts/validate_production.py")
run("python3", "scripts/validate_production.py")
run("docker", "compose", "--env-file", ".env.example", "config", "-q")

# Semantic invariants beyond the repository validator.
checks_now = {
    "runtime lease": "class RunLease" in read("backend/app/runtime.py"),
    "watchdog": "internetboard.runtime_watchdog" in read("backend/app/tasks.py"),
    "worker health": "inspect ping" in read("docker-compose.yml"),
    "recover api": '/api/runs/{run_id}/recover' in read("backend/app/main.py"),
    "recover ui": "recoverRun" in read("frontend/app.js"),
    "heartbeat pipeline": "touch_run(run.id)" in read("backend/app/pipeline.py"),
    "visual provenance": "parent_source_ids" in read("backend/app/pipeline.py"),
    "ci imports": "Import runtime modules" in read(".github/workflows/dockerhub.yml"),
}
failed = [name for name, ok in checks_now.items() if not ok]
if failed:
    raise SystemExit("V4 semantic validation failed: " + ", ".join(failed))
print("InternetBoard V4 runtime invariants: PASS")

# Show changes before committing.
run("git", "diff", "--check")
run("git", "status", "--short")
print("\nPATCH COMPLETE")
print("Next runtime behavior: stale tasks self-heal, duplicate deliveries are locked, Worker health is visible, and CI checks imports.")

if ARGS.push:
    run("git", "add", "-A")
    status = run("git", "status", "--porcelain")
    if not status.strip():
        print("No changes to commit; repository already has V4.")
    else:
        run("git", "commit", "-m", "Production: self-healing task runtime and worker watchdog")
        run("git", "push", "origin", "main")
        print("\nPUSH COMPLETE")
