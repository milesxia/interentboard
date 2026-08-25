from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import settings
from app.scheduler import DailyScheduler


async def noop():
    pass


scheduler = DailyScheduler(settings.timezone, settings.schedule_hour, settings.schedule_minute, noop)
now = datetime.now(ZoneInfo(settings.timezone))
print('now:', now.isoformat(timespec='seconds'))
print('next:', scheduler.next_run_time.isoformat(timespec='seconds'))
