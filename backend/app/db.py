from __future__ import annotations

import logging
import time
from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings
from .utils import atomic_write_json

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


engine_options = {"pool_pre_ping": True}
if not settings.database_url.startswith("sqlite"):
    engine_options.update(pool_size=5, max_overflow=5, pool_recycle=1800)
engine = create_engine(settings.database_url, **engine_options)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@event.listens_for(Session, "after_commit")
def _write_committed_history_files(session: Session) -> None:
    pending = session.info.pop("history_files", [])
    for item in pending:
        path = settings.history_dir / f"{item['object_type']}_{item['object_id']}_v{item['version']}.json"
        try:
            atomic_write_json(path, item)
        except Exception as exc:
            logger.warning("Database commit succeeded but history file write failed for %s: %s", path, exc)


@event.listens_for(Session, "after_rollback")
def _discard_rolled_back_history_files(session: Session) -> None:
    session.info.pop("history_files", None)


@contextmanager
def session_scope():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def wait_for_database(max_attempts: int = 60, delay_seconds: int = 2) -> None:
    last_error: Exception | None = None
    for _ in range(max_attempts):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return
        except Exception as exc:  # pragma: no cover - startup resiliency
            last_error = exc
            time.sleep(delay_seconds)
    raise RuntimeError(f"Database unavailable after {max_attempts} attempts: {last_error}")


def init_db() -> None:
    from . import models  # noqa: F401

    wait_for_database()
    Base.metadata.create_all(bind=engine)
