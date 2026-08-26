from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import yaml
from sqlalchemy import select

from .config import settings
from .db import session_scope
from .models import Topic

logger = logging.getLogger(__name__)
DEFAULT_TOPICS_PATH = Path("/app/config/topics.yml")
DEFAULT_BASELINE_PATH = Path("/app/seed/baseline-v2.8.md")
MARKER_PATH = settings.data_dir / ".bootstrap" / "topics-v1.json"
PERSISTENT_BASELINE_PATH = settings.data_dir / ".bootstrap" / "baseline-v2.8.md"


def _queries(item: dict) -> list[str]:
    out: list[str] = []
    for raw in item.get("queries") or []:
        value = " ".join(str(raw).split()).strip()
        if value and value not in out:
            out.append(value)
    return out


def _description(item: dict) -> str:
    lines: list[str] = []
    pairs = (
        ("Current state", item.get("current_state")),
        ("Current summary", item.get("current_summary")),
        ("Analysis discipline", item.get("discipline")),
        ("Risk level", item.get("risk_level")),
    )
    for label, value in pairs:
        if value:
            lines.append(f"{label}: {value}")

    keywords = item.get("context_keywords") or []
    if keywords:
        lines.append("Context keywords: " + ", ".join(map(str, keywords)))

    domains = item.get("official_domains") or []
    if domains:
        lines.append("Preferred official domains: " + ", ".join(map(str, domains)))

    seed_urls = item.get("seed_urls") or []
    if seed_urls:
        lines.append("Seed evidence URLs:")
        lines.extend(f"- {url}" for url in seed_urls)

    watch_nodes = item.get("watch_nodes") or []
    if watch_nodes:
        lines.append("Scheduled watch points:")
        for node in watch_nodes:
            title = node.get("title") or node.get("id") or "watch"
            due = node.get("due_date") or "unspecified"
            lines.append(f"- {title} | due={due}")
            for query in node.get("queries") or []:
                lines.append(f"  query: {query}")

    slug = item.get("slug")
    if slug:
        lines.append(f"Bootstrap slug: {slug}")

    return "\n".join(lines)[:4000]


def _write_marker(payload: dict) -> None:
    MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "written_at": datetime.now(timezone.utc).isoformat(),
        **payload,
    }
    MARKER_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def bootstrap_defaults_once() -> dict:
    """Apply built-in defaults exactly once without overwriting user content.

    The durable marker is stored under /data, so image upgrades never re-apply
    defaults. On the one bootstrap pass, existing topic names are preserved and
    only missing built-in topics are inserted. Deleting or editing topics later
    never causes them to be restored by an image update.
    """
    if MARKER_PATH.exists():
        return {"seeded": False, "reason": "marker_exists"}

    if not DEFAULT_TOPICS_PATH.exists():
        logger.warning("Default topic file not found: %s", DEFAULT_TOPICS_PATH)
        return {"seeded": False, "reason": "seed_file_missing"}

    if DEFAULT_BASELINE_PATH.exists() and not PERSISTENT_BASELINE_PATH.exists():
        PERSISTENT_BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
        PERSISTENT_BASELINE_PATH.write_text(DEFAULT_BASELINE_PATH.read_text(encoding="utf-8"), encoding="utf-8")

    payload = yaml.safe_load(DEFAULT_TOPICS_PATH.read_text(encoding="utf-8")) or {}
    prepared: list[dict] = []
    for item in payload.get("topics") or []:
        queries = _queries(item)
        name = " ".join(str(item.get("name") or "").split()).strip()
        if not name or not queries:
            continue
        prepared.append(
            {
                "name": name,
                "query": "\n".join(queries),
                "description": _description(item),
                "enabled": bool(item.get("enabled", True)),
                "priority": int(item.get("priority", 50)),
            }
        )

    if not prepared:
        logger.warning("No valid default topics found in %s", DEFAULT_TOPICS_PATH)
        return {"seeded": False, "reason": "no_valid_topics"}

    inserted: list[str] = []
    preserved: list[str] = []
    with session_scope() as session:
        existing_names = set(session.scalars(select(Topic.name)))
        for item in prepared:
            if item["name"] in existing_names:
                preserved.append(item["name"])
                continue
            session.add(Topic(**item))
            inserted.append(item["name"])

    result = {
        "seeded": bool(inserted),
        "inserted": inserted,
        "preserved_existing": preserved,
        "source": str(DEFAULT_TOPICS_PATH),
        "baseline": str(PERSISTENT_BASELINE_PATH) if PERSISTENT_BASELINE_PATH.exists() else "",
    }
    _write_marker(result)
    logger.info(
        "Default bootstrap complete: inserted=%s preserved=%s; marker prevents all future re-application",
        len(inserted),
        len(preserved),
    )
    return result
