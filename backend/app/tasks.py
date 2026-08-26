from __future__ import annotations

import logging
from datetime import timedelta

from celery import Celery
from celery.schedules import crontab
from sqlalchemy import select

from .config import settings
from .db import init_db, session_scope
from .fetcher import fetch_document
from .models import ResearchRun, RunStatus, Topic, WebsiteWatch, utcnow
from .pipeline import execute_research_run, mark_run_failed

logger = logging.getLogger(__name__)

celery_app = Celery("internetboard", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    timezone=settings.timezone,
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_reject_on_worker_lost=True,
    broker_connection_retry_on_startup=True,
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
    },
)


@celery_app.task(name="internetboard.run_research", bind=True, max_retries=settings.max_run_retries)
def run_research_task(self, run_id: int) -> dict:
    init_db()
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
            with session_scope() as session:
                run = session.get(ResearchRun, run_id)
                if run:
                    run.status = RunStatus.WAITING.value
                    run.message = f"Retry scheduled after failure ({retry_count}/{settings.max_run_retries})"
                    run.finished_at = None
            raise self.retry(exc=exc, countdown=min(600, 60 * (2**retry_count)))
        raise


@celery_app.task(name="internetboard.run_all_topics")
def run_all_topics() -> dict:
    init_db()
    queued: list[int] = []
    with session_scope() as session:
        topics = list(session.scalars(select(Topic).where(Topic.enabled.is_(True)).order_by(Topic.priority.desc())))
        for topic_row in topics:
            topic = session.scalar(select(Topic).where(Topic.id == topic_row.id).with_for_update())
            active = session.scalar(
                select(ResearchRun).where(
                    ResearchRun.topic_id == topic.id,
                    ResearchRun.status.in_([
                        RunStatus.WAITING.value,
                        RunStatus.SEARCHING.value,
                        RunStatus.FETCHING.value,
                        RunStatus.CHUNKING.value,
                        RunStatus.AI_ANALYSIS.value,
                        RunStatus.KNOWLEDGE_UPDATE.value,
                    ]),
                )
            )
            if active:
                continue
            run = ResearchRun(topic_id=topic.id, status=RunStatus.WAITING.value, progress=0, message="Daily scheduled run")
            session.add(run)
            session.flush()
            queued.append(run.id)
    for run_id in queued:
        run_research_task.delay(run_id)
    return {"queued": queued}


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

    queued: list[int] = []
    with session_scope() as session:
        for topic_id in changed_topics:
            topic = session.scalar(select(Topic).where(Topic.id == topic_id).with_for_update())
            if not topic:
                continue
            active = session.scalar(
                select(ResearchRun).where(
                    ResearchRun.topic_id == topic_id,
                    ResearchRun.status.not_in([RunStatus.COMPLETED.value, RunStatus.FAILED.value]),
                )
            )
            if active:
                continue
            run = ResearchRun(topic_id=topic_id, status=RunStatus.WAITING.value, progress=0, message="Website change detected")
            session.add(run)
            session.flush()
            queued.append(run.id)
    for run_id in queued:
        run_research_task.delay(run_id)
    return {"checked": checked, "changed_topics": sorted(changed_topics), "queued": queued}
