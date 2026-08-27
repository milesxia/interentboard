from __future__ import annotations

import json
import logging
import math
import os
import statistics
import time
import uuid
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import redis
from sqlalchemy import inspect

from . import models as db_models
from .db import session_scope

logger = logging.getLogger("internetboard.queue_runtime")

TZ = ZoneInfo(os.getenv("TZ", "Asia/Shanghai"))
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
QUEUE_DEFAULT_RUN_SECONDS = max(300, int(os.getenv("QUEUE_DEFAULT_RUN_SECONDS", "3600")))
STALL_PROGRESS_TIMEOUT_SECONDS = max(300, int(os.getenv("STALL_PROGRESS_TIMEOUT_SECONDS", "1200")))
AUTO_RECOVERY_MAX_ATTEMPTS = max(0, int(os.getenv("AUTO_RECOVERY_MAX_ATTEMPTS", "2")))
QUEUE_RECOVERY_DELAY_SECONDS = max(30, int(os.getenv("QUEUE_RECOVERY_DELAY_SECONDS", "210")))
TASK_ID_TTL_SECONDS = max(3600, int(os.getenv("QUEUE_TASK_ID_TTL_SECONDS", "604800")))
AI_JOB_TTL_SECONDS = max(3600, int(os.getenv("AI_JOB_TTL_SECONDS", "86400")))
AI_QA_DEFAULT_SECONDS = max(120, int(os.getenv("AI_QA_DEFAULT_SECONDS", "900")))
AI_SUMMARY_DEFAULT_SECONDS = max(120, int(os.getenv("AI_SUMMARY_DEFAULT_SECONDS", "1200")))

ACTIVE_NAMES = {
    "waiting", "queued", "pending", "running", "searching", "fetching",
    "chunking", "analyzing", "synthesizing", "processing",
}
QUEUED_NAMES = {"waiting", "queued", "pending"}
RUNNING_NAMES = ACTIVE_NAMES - QUEUED_NAMES
FAILED_NAMES = {"failed", "error"}
COMPLETED_NAMES = {"completed", "complete", "success", "succeeded", "done"}

AI_JOB_PREFIX = "ib:v410:ai:job:"
AI_JOB_INDEX = "ib:v410:ai:index"
AI_JOB_ACTIVE = "ib:v410:ai:active"


def _redis() -> redis.Redis:
    return redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=3, socket_connect_timeout=3)


def _now() -> datetime:
    return datetime.now(TZ)


def _status_text(value: Any) -> str:
    if value is None:
        return "unknown"
    raw = getattr(value, "value", value)
    raw = str(raw)
    if "." in raw:
        raw = raw.rsplit(".", 1)[-1]
    return raw.strip().lower()


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (list, tuple, set)):
        return [_json_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    enum_value = getattr(value, "value", None)
    if enum_value is not None:
        return _json_value(enum_value)
    return str(value)


def model_to_dict(obj: Any) -> dict[str, Any]:
    mapper = inspect(obj).mapper
    out: dict[str, Any] = {}
    for attr in mapper.column_attrs:
        try:
            out[attr.key] = _json_value(getattr(obj, attr.key))
        except Exception:
            continue
    return out


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        raw = value.strip().replace("Z", "+00:00")
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def _first_dt(row: dict[str, Any], names: tuple[str, ...]) -> datetime | None:
    for name in names:
        dt = _parse_dt(row.get(name))
        if dt is not None:
            return dt
    return None


def _duration_seconds(row: dict[str, Any]) -> float | None:
    start = _first_dt(row, ("started_at", "created_at", "queued_at"))
    finish = _first_dt(row, ("finished_at", "completed_at", "updated_at"))
    if start is None or finish is None:
        return None
    seconds = (finish - start).total_seconds()
    if 30 <= seconds <= 7 * 24 * 3600:
        return seconds
    return None


def _to_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace("%", ""))
        except ValueError:
            return None
    return None


def _progress_percent(row: dict[str, Any]) -> tuple[float | None, str]:
    for key in ("progress_percent", "progress_pct", "percent", "progress", "completion_percent"):
        number = _to_number(row.get(key))
        if number is None:
            continue
        if 0 <= number <= 1:
            number *= 100
        if 0 <= number <= 100:
            return round(number, 1), key

    total = None
    done = None
    for key in ("total_chunks", "chunk_total", "chunks_total", "total_steps"):
        total = _to_number(row.get(key))
        if total and total > 0:
            break
    for key in ("completed_chunks", "processed_chunks", "chunk_index", "current_chunk", "completed_steps"):
        done = _to_number(row.get(key))
        if done is not None:
            break
    if total and done is not None:
        ratio = min(1.0, max(0.0, done / total))
        return round(45 + ratio * 45, 1), "chunk-counter"

    phase = _status_text(row.get("status"))
    phase_map = {
        "waiting": 0.0, "queued": 0.0, "pending": 0.0, "running": 5.0,
        "searching": 12.0, "fetching": 28.0, "chunking": 42.0,
        "analyzing": 62.0, "processing": 68.0, "synthesizing": 90.0,
    }
    return (phase_map.get(phase), "phase" if phase in phase_map else "unknown")


def _history_estimate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    durations: list[float] = []
    for row in rows:
        if _status_text(row.get("status")) not in COMPLETED_NAMES:
            continue
        seconds = _duration_seconds(row)
        if seconds is not None:
            durations.append(seconds)
        if len(durations) >= 30:
            break
    if durations:
        median = float(statistics.median(durations))
        p75 = float(sorted(durations)[max(0, math.ceil(len(durations) * 0.75) - 1)])
    else:
        median = float(QUEUE_DEFAULT_RUN_SECONDS)
        p75 = median
    count = len(durations)
    return {
        "sample_count": count,
        "median_seconds": round(median),
        "p75_seconds": round(p75),
        "confidence": "high" if count >= 10 else "medium" if count >= 3 else "low",
        "fallback_used": count == 0,
    }


def _job_key(job_id: str) -> str:
    return f"{AI_JOB_PREFIX}{job_id}"


def create_ai_job(kind: str, payload: dict[str, Any], label: str) -> dict[str, Any]:
    job_id = uuid.uuid4().hex
    now = _now()
    body = {
        "job_id": job_id,
        "kind": kind,
        "label": label,
        "status": "queued",
        "created_at": now.isoformat(),
        "created_epoch": now.timestamp(),
        "updated_at": now.isoformat(),
        "updated_epoch": now.timestamp(),
        "payload": payload,
        "task_id": None,
        "result": None,
        "error": None,
    }
    r = _redis()
    pipe = r.pipeline()
    pipe.set(_job_key(job_id), json.dumps(body, ensure_ascii=False, default=str), ex=AI_JOB_TTL_SECONDS)
    pipe.zadd(AI_JOB_INDEX, {job_id: now.timestamp()})
    pipe.expire(AI_JOB_INDEX, AI_JOB_TTL_SECONDS * 2)
    pipe.execute()
    return body


def _save_ai_job(body: dict[str, Any]) -> dict[str, Any]:
    now = _now()
    body["updated_at"] = now.isoformat()
    body["updated_epoch"] = now.timestamp()
    r = _redis()
    r.set(_job_key(str(body["job_id"])), json.dumps(body, ensure_ascii=False, default=str), ex=AI_JOB_TTL_SECONDS)
    return body


def get_ai_job(job_id: str) -> dict[str, Any] | None:
    try:
        raw = _redis().get(_job_key(job_id))
        if not raw:
            return None
        body = json.loads(raw)
        return body if isinstance(body, dict) else None
    except Exception:
        return None


def set_ai_job_task_id(job_id: str, task_id: str | None) -> None:
    body = get_ai_job(job_id)
    if not body:
        return
    body["task_id"] = task_id
    _save_ai_job(body)


def mark_ai_job_running(job_id: str) -> dict[str, Any]:
    body = get_ai_job(job_id) or {"job_id": job_id, "kind": "unknown", "label": job_id}
    now = _now()
    body["status"] = "running"
    body["started_at"] = now.isoformat()
    body["started_epoch"] = now.timestamp()
    body["error"] = None
    _save_ai_job(body)
    try:
        _redis().sadd(AI_JOB_ACTIVE, job_id)
        _redis().expire(AI_JOB_ACTIVE, AI_JOB_TTL_SECONDS * 2)
    except Exception:
        pass
    return body


def complete_ai_job(job_id: str, result: dict[str, Any]) -> dict[str, Any]:
    body = get_ai_job(job_id) or {"job_id": job_id}
    body["status"] = "completed"
    body["result"] = result
    body["error"] = None
    body["finished_at"] = _now().isoformat()
    _save_ai_job(body)
    try:
        _redis().srem(AI_JOB_ACTIVE, job_id)
    except Exception:
        pass
    return body


def fail_ai_job(job_id: str, error: str) -> dict[str, Any]:
    body = get_ai_job(job_id) or {"job_id": job_id}
    body["status"] = "failed"
    body["error"] = str(error)[:4000]
    body["finished_at"] = _now().isoformat()
    _save_ai_job(body)
    try:
        _redis().srem(AI_JOB_ACTIVE, job_id)
    except Exception:
        pass
    return body


def list_ai_jobs(limit: int = 100) -> list[dict[str, Any]]:
    r = _redis()
    try:
        ids = r.zrevrange(AI_JOB_INDEX, 0, max(0, limit - 1))
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    stale: list[str] = []
    for job_id in ids:
        body = get_ai_job(job_id)
        if body:
            out.append(body)
        else:
            stale.append(job_id)
    if stale:
        try:
            r.zrem(AI_JOB_INDEX, *stale)
        except Exception:
            pass
    return out


def _redis_progress_state(run_id: int) -> dict[str, Any]:
    try:
        raw = _redis().get(f"ib:v410:progress:{run_id}")
        if raw:
            body = json.loads(raw)
            if isinstance(body, dict):
                return body
    except Exception:
        pass
    return {}


def _recovery_attempts(run_id: int) -> int:
    try:
        return int(_redis().get(f"ib:v410:recovery:{run_id}") or 0)
    except Exception:
        return 0


def _current_remaining(row: dict[str, Any], history_seconds: float, now: datetime) -> tuple[float, float | None, str]:
    start = _first_dt(row, ("started_at", "created_at", "queued_at"))
    elapsed = max(0.0, (now - start).total_seconds()) if start else 0.0
    progress, source = _progress_percent(row)
    if progress is not None and progress >= 5 and elapsed >= 60:
        frac = min(0.98, max(0.05, progress / 100.0))
        live_total = elapsed / frac
        predicted = live_total * 0.65 + history_seconds * 0.35
        predicted = min(max(elapsed, history_seconds * 3.0), max(elapsed, history_seconds * 0.45, predicted))
        return max(60.0, predicted - elapsed), progress, f"live-{source}+history"
    return max(60.0, history_seconds - elapsed), progress, "history"


def _with_runtime_meta(row: dict[str, Any], now: datetime) -> dict[str, Any]:
    run_id = row.get("id")
    progress, source = _progress_percent(row)
    state = _redis_progress_state(int(run_id)) if isinstance(run_id, int) else {}
    changed_at = state.get("changed_at")
    stalled_for = None
    if changed_at:
        dt = _parse_dt(changed_at)
        if dt:
            stalled_for = max(0, int((now - dt).total_seconds()))
    return {
        **row,
        "kind": "research",
        "label": f"Research Run #{run_id}" if run_id is not None else "Research Run",
        "status": _status_text(row.get("status")),
        "progress_percent": progress,
        "progress_source": source,
        "progress_changed_at": changed_at,
        "no_progress_seconds": stalled_for,
        "auto_recovery_attempts": _recovery_attempts(int(run_id)) if isinstance(run_id, int) else 0,
    }


def _aux_row(job: dict[str, Any]) -> dict[str, Any]:
    created = job.get("created_at") or _now().isoformat()
    kind = str(job.get("kind") or "ai")
    status = str(job.get("status") or "queued").lower()
    progress = 50.0 if status == "running" else 0.0
    return {
        "id": f"AI-{str(job.get('job_id') or '')[:8]}",
        "job_id": job.get("job_id"),
        "kind": kind,
        "label": job.get("label") or ("当日总结" if kind == "daily_summary" else "知识库问答"),
        "status": status,
        "progress_percent": progress,
        "created_at": created,
        "started_at": job.get("started_at"),
        "message": job.get("error") or "",
    }


def _aux_duration(row: dict[str, Any], research_median: float) -> float:
    if row.get("kind") == "knowledge_qa":
        return float(max(AI_QA_DEFAULT_SECONDS, research_median * 0.20))
    if row.get("kind") == "daily_summary":
        return float(max(AI_SUMMARY_DEFAULT_SECONDS, research_median * 0.30))
    return float(max(AI_QA_DEFAULT_SECONDS, research_median * 0.25))


def build_queue_view(rows: list[dict[str, Any]]) -> dict[str, Any]:
    now = _now()
    normalized = [_with_runtime_meta({**row, "status": _status_text(row.get("status"))}, now) for row in rows]
    research_running = [row for row in normalized if row["status"] in RUNNING_NAMES]
    research_queued = [row for row in normalized if row["status"] in QUEUED_NAMES]

    aux = [_aux_row(j) for j in list_ai_jobs(100)]
    aux_running = [row for row in aux if row["status"] == "running"]
    aux_queued = [row for row in aux if row["status"] == "queued"]

    def dt_key(row: dict[str, Any]) -> tuple[datetime, str]:
        dt = _first_dt(row, ("started_at", "created_at", "queued_at")) or now
        return dt, str(row.get("id") or "")

    research_running.sort(key=dt_key)
    research_queued.sort(key=dt_key)
    aux_running.sort(key=dt_key)
    aux_queued.sort(key=dt_key)

    history = _history_estimate(normalized)
    median = float(history["median_seconds"])
    all_running = sorted(research_running + aux_running, key=dt_key)
    current = all_running[0] if all_running else None
    extra_running = all_running[1:]

    cursor = now
    if current is not None:
        if current.get("kind") == "research":
            remaining, progress, basis = _current_remaining(current, median, now)
        else:
            expected = _aux_duration(current, median)
            start = _first_dt(current, ("started_at", "created_at")) or now
            elapsed = max(0.0, (now - start).total_seconds())
            remaining, progress, basis = max(60.0, expected - elapsed), current.get("progress_percent"), "aux-history"
        cursor = now + timedelta(seconds=remaining)
        current = {
            **current,
            "eta_remaining_seconds": round(remaining),
            "eta_complete_at": cursor.isoformat(),
            "eta_basis": basis,
            "progress_percent": progress,
        }

    pending = sorted(research_queued + aux_queued, key=dt_key)
    queued_view: list[dict[str, Any]] = []
    for index, row in enumerate(pending, 1):
        duration = median if row.get("kind") == "research" else _aux_duration(row, median)
        start_at = cursor
        finish_at = start_at + timedelta(seconds=duration)
        queued_view.append({
            **row,
            "queue_position": index,
            "eta_start_at": start_at.isoformat(),
            "eta_complete_at": finish_at.isoformat(),
            "eta_duration_seconds": round(duration),
            "eta_basis": "history" if history["sample_count"] else "fallback",
        })
        cursor = finish_at

    return {
        "mode": "serial-ai",
        "research_concurrency": 1,
        "current": current,
        "queued": queued_view,
        "queued_count": len(queued_view),
        "extra_running": extra_running,
        "serial_violation": len(all_running) > 1,
        "all_complete_at": cursor.isoformat() if current or queued_view else None,
        "estimate": history,
        "monitor": {
            "stall_timeout_seconds": STALL_PROGRESS_TIMEOUT_SECONDS,
            "max_auto_recoveries": AUTO_RECOVERY_MAX_ATTEMPTS,
            "recovery_delay_seconds": QUEUE_RECOVERY_DELAY_SECONDS,
        },
    }


def record_enqueued_task(run_id: int, task_id: str | None) -> None:
    if not task_id:
        return
    try:
        r = _redis()
        pipe = r.pipeline()
        pipe.set(f"ib:v410:task:{run_id}", task_id, ex=TASK_ID_TTL_SECONDS)
        pipe.delete(f"ib:v410:progress:{run_id}")
        pipe.execute()
    except Exception as exc:
        logger.warning("could not record task id run_id=%s: %s", run_id, exc)


def reset_recovery_state(run_id: int) -> None:
    try:
        _redis().delete(
            f"ib:v410:recovery:{run_id}",
            f"ib:v410:progress:{run_id}",
            f"ib:v410:task:{run_id}",
        )
    except Exception as exc:
        logger.warning("could not reset recovery state run_id=%s: %s", run_id, exc)


def _research_run_model():
    model = getattr(db_models, "ResearchRun", None)
    if model is None:
        raise RuntimeError("ResearchRun model unavailable")
    return model


def _enum_state(current: Any, desired: str) -> Any:
    enum_cls = type(current)
    candidate = getattr(enum_cls, desired.upper(), None)
    if candidate is not None:
        return candidate
    raw = getattr(current, "value", current)
    if isinstance(raw, str) and raw.isupper():
        return desired.upper()
    return desired.lower()


def _progress_signature(row: dict[str, Any]) -> str:
    interesting: dict[str, Any] = {"status": _status_text(row.get("status"))}
    tokens = (
        "progress", "percent", "stage", "phase", "step", "chunk", "source", "query",
        "message", "status_message", "processed", "completed", "current",
    )
    for key, value in row.items():
        key_l = key.lower()
        if any(token in key_l for token in tokens) and "heartbeat" not in key_l:
            interesting[key] = value
    return json.dumps(interesting, ensure_ascii=False, sort_keys=True, default=str)[:8000]


def _set_run_state(run_id: int, status: str, message: str, clear_error: bool) -> None:
    Run = _research_run_model()
    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            return
        run.status = _enum_state(getattr(run, "status", None), status)
        for field in ("message", "status_message"):
            if hasattr(run, field):
                try:
                    setattr(run, field, message)
                    break
                except Exception:
                    pass
        if clear_error:
            for field in ("error", "error_message", "finished_at", "completed_at"):
                if hasattr(run, field):
                    try:
                        setattr(run, field, None)
                    except Exception:
                        pass
        else:
            for field in ("error_message", "error"):
                if hasattr(run, field):
                    try:
                        setattr(run, field, message)
                        break
                    except Exception:
                        pass
            for field in ("finished_at", "completed_at"):
                if hasattr(run, field):
                    try:
                        setattr(run, field, _now())
                    except Exception:
                        pass


def _revoke_if_known(celery_app: Any, run_id: int) -> str | None:
    try:
        task_id = _redis().get(f"ib:v410:task:{run_id}")
    except Exception:
        task_id = None
    if not task_id:
        return None
    try:
        celery_app.control.revoke(task_id, terminate=True, signal="SIGTERM")
        logger.warning("revoked stalled research task run_id=%s task_id=%s", run_id, task_id)
    except Exception as exc:
        logger.exception("failed to revoke stalled task run_id=%s task_id=%s: %s", run_id, task_id, exc)
    return task_id


def _query_running_rows() -> list[dict[str, Any]]:
    Run = _research_run_model()
    with session_scope() as session:
        query = session.query(Run)
        filtered = False
        try:
            status_attr = getattr(Run, "status")
            enum_cls = status_attr.property.columns[0].type.enum_class
            values = []
            if enum_cls is not None:
                for name in RUNNING_NAMES:
                    candidate = getattr(enum_cls, name.upper(), None)
                    if candidate is not None:
                        values.append(candidate)
            if values:
                query = query.filter(status_attr.in_(values))
                filtered = True
        except Exception:
            filtered = False
        if not filtered:
            query = query.order_by(Run.id.desc()).limit(2000)
        objects = query.all()
        rows = [model_to_dict(obj) for obj in objects]
    return [row for row in rows if _status_text(row.get("status")) in RUNNING_NAMES]


def _monitor_aux_jobs(celery_app: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    r = _redis()
    try:
        ids = list(r.smembers(AI_JOB_ACTIVE))
    except Exception:
        return result
    now_epoch = time.time()
    for job_id in ids:
        body = get_ai_job(job_id)
        if not body:
            r.srem(AI_JOB_ACTIVE, job_id)
            continue
        if str(body.get("status")) != "running":
            r.srem(AI_JOB_ACTIVE, job_id)
            continue
        started = float(body.get("started_epoch") or body.get("updated_epoch") or now_epoch)
        idle = max(0, int(now_epoch - started))
        if idle < STALL_PROGRESS_TIMEOUT_SECONDS:
            continue
        task_id = body.get("task_id")
        if task_id:
            try:
                celery_app.control.revoke(task_id, terminate=True, signal="SIGTERM")
            except Exception:
                logger.exception("failed to revoke stalled AI job job_id=%s", job_id)
        fail_ai_job(job_id, f"AI job stalled for {idle}s and was terminated")
        result.append({"job_id": job_id, "task_id": task_id, "idle_seconds": idle})
    return result


def monitor_stalled_runs(celery_app: Any) -> dict[str, Any]:
    rows = _query_running_rows()
    now = _now()
    now_epoch = time.time()
    r = _redis()
    result: dict[str, Any] = {
        "running": [], "stalled": [], "recovery_scheduled": [], "failed": [],
        "aux_failed": _monitor_aux_jobs(celery_app),
    }

    if len(rows) > 1:
        result["serial_violation"] = [row.get("id") for row in rows]
        logger.error("serial execution violation: multiple research runs=%s", result["serial_violation"])

    for row in rows:
        run_id = row.get("id")
        if not isinstance(run_id, int):
            continue
        signature = _progress_signature(row)
        key = f"ib:v410:progress:{run_id}"
        previous_raw = r.get(key)
        try:
            previous = json.loads(previous_raw) if previous_raw else {}
        except Exception:
            previous = {}

        if not previous or previous.get("signature") != signature:
            payload = {"signature": signature, "changed_at": now.isoformat(), "changed_epoch": now_epoch}
            r.set(key, json.dumps(payload, ensure_ascii=False), ex=TASK_ID_TTL_SECONDS)
            result["running"].append({"run_id": run_id, "progress_changed": True})
            continue

        changed_epoch = float(previous.get("changed_epoch") or now_epoch)
        idle_seconds = max(0, int(now_epoch - changed_epoch))
        result["running"].append({"run_id": run_id, "progress_changed": False, "idle_seconds": idle_seconds})
        if idle_seconds < STALL_PROGRESS_TIMEOUT_SECONDS:
            continue

        attempts_key = f"ib:v410:recovery:{run_id}"
        attempts = int(r.get(attempts_key) or 0)
        result["stalled"].append({"run_id": run_id, "idle_seconds": idle_seconds, "attempts": attempts})
        task_id = _revoke_if_known(celery_app, run_id)

        if attempts >= AUTO_RECOVERY_MAX_ATTEMPTS:
            message = (
                f"FAILED after {attempts} automatic recoveries: no real progress for "
                f"{idle_seconds}s (threshold {STALL_PROGRESS_TIMEOUT_SECONDS}s)"
            )
            _set_run_state(run_id, "failed", message, clear_error=False)
            r.delete(key)
            result["failed"].append({"run_id": run_id, "task_id": task_id, "message": message})
            continue

        attempt = attempts + 1
        r.set(attempts_key, attempt, ex=TASK_ID_TTL_SECONDS)
        message = (
            f"STALLED: no real progress for {idle_seconds}s; automatic recovery "
            f"{attempt}/{AUTO_RECOVERY_MAX_ATTEMPTS} scheduled"
        )
        _set_run_state(run_id, "waiting", message, clear_error=True)
        r.delete(key)
        celery_app.send_task(
            "internetboard.requeue_stalled_v410",
            args=[run_id, attempt],
            queue="control",
            countdown=QUEUE_RECOVERY_DELAY_SECONDS,
        )
        result["recovery_scheduled"].append({
            "run_id": run_id, "attempt": attempt, "task_id": task_id,
            "delay_seconds": QUEUE_RECOVERY_DELAY_SECONDS,
        })

    return result
