from __future__ import annotations

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
