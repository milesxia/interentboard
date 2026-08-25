from __future__ import annotations

import asyncio
import json


class TaskWorker:
    """Single durable worker: appropriate for a NAS with one inference slot.

    Tasks survive app restarts in SQLite. A task that was running when the NAS/app
    stopped is returned to the queue on startup; chunk ledgers make the underlying
    research job resume rather than repeat completed extraction work.
    """

    def __init__(self, db, engine, *, poll_seconds: int = 2):
        self.db = db
        self.engine = engine
        self.poll_seconds = max(1, int(poll_seconds))
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.current_task_id: int | None = None

    def start(self) -> None:
        self.db.recover_interrupted_tasks()
        if not self._task or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="internetboard-task-worker")

    async def shutdown(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _execute(self, item: dict) -> None:
        kind = item.get("kind")
        payload = json.loads(item.get("payload_json") or "{}")
        if kind == "refresh-all":
            await self.engine.refresh_all(mode=payload.get("mode", "queued-all"))
        elif kind == "refresh-topic":
            slug = item.get("topic_slug") or payload.get("slug")
            await self.engine.refresh_topic(slug, mode=payload.get("mode", "queued"))
        elif kind == "manual-knowledge":
            await self.engine.ingest_manual_source(int(payload["source_id"]))
        else:
            raise ValueError(f"unknown queue task kind: {kind}")

    async def _run(self) -> None:
        while not self._stop.is_set():
            item = self.db.claim_next_task()
            if not item:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
                except asyncio.TimeoutError:
                    pass
                continue
            self.current_task_id = int(item["id"])
            try:
                await self._execute(item)
            except asyncio.CancelledError:
                # Leave as running; startup recovery will requeue it.
                raise
            except Exception as exc:
                self.db.finish_task(self.current_task_id, False, str(exc))
            else:
                self.db.finish_task(self.current_task_id, True)
            finally:
                self.current_task_id = None
