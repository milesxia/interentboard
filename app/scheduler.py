from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


class DailyScheduler:
    def __init__(self, timezone: str, hour: int, minute: int, callback):
        self.tz = ZoneInfo(timezone)
        self.hour = hour
        self.minute = minute
        self.callback = callback
        self._task: asyncio.Task | None = None
        self.next_run_time = self._next_run()

    def _next_run(self):
        now = datetime.now(self.tz)
        target = now.replace(hour=self.hour, minute=self.minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target

    async def _loop(self):
        while True:
            self.next_run_time = self._next_run()
            wait = max(1.0, (self.next_run_time - datetime.now(self.tz)).total_seconds())
            await asyncio.sleep(wait)
            try:
                await self.callback()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failed run must not kill future daily schedules.
                pass

    def start(self):
        if not self._task or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="intelboard-daily-scheduler")

    async def shutdown(self):
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
