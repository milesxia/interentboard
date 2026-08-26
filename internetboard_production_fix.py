#!/usr/bin/env python3
from pathlib import Path
import argparse
import re
import subprocess

ROOT = Path.cwd()
PARSER = argparse.ArgumentParser(description="Apply the InternetBoard v1.0 production fix")
PARSER.add_argument("--push", action="store_true", help="commit and push the patched production source")
ARGS = PARSER.parse_args()


def read(path):
    return (ROOT / path).read_text(encoding='utf-8')


def write(path, text):
    p = ROOT / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding='utf-8')
    print(f'[write] {path}')


def replace_once(path, old, new):
    text = read(path)
    if old not in text:
        raise SystemExit(f'Expected text not found in {path}: {old[:100]!r}')
    write(path, text.replace(old, new, 1))


bootstrap_py = r'''from __future__ import annotations

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
'''
write('backend/app/bootstrap.py', bootstrap_py)

handoff_py = r'''from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .config import settings
from .db import session_scope
from .models import (
    Claim,
    Conflict,
    Entity,
    KnowledgeVersion,
    ManualNote,
    Relation,
    ResearchRun,
    Source,
    Topic,
    WebsiteWatch,
)


def _dt(value) -> str:
    return value.isoformat() if value else ""


def _safe(value) -> str:
    return str(value or "").replace("\x00", "").strip()


def _queries(raw: str) -> list[str]:
    values: list[str] = []
    for line in (raw or "").splitlines():
        line = " ".join(line.strip().lstrip("-*• ").split())
        if line and line not in values:
            values.append(line)
    if not values and raw.strip():
        values.append(" ".join(raw.split()))
    return values


def build_handoff_markdown() -> Path:
    now = datetime.now(timezone.utc).astimezone(ZoneInfo(settings.timezone))
    filename = f"InternetBoard-handoff-{now:%Y%m%d-%H%M%S}.md"
    export_dir = settings.data_dir / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    path = export_dir / filename

    with session_scope() as session:
        topics = list(session.scalars(select(Topic).order_by(Topic.priority.desc(), Topic.id.asc())))
        runs = list(session.scalars(select(ResearchRun).order_by(ResearchRun.created_at.desc()).limit(100)))
        claims = list(session.scalars(select(Claim).order_by(Claim.priority.desc(), Claim.updated_at.desc()).limit(800)))
        notes = list(session.scalars(select(ManualNote).order_by(ManualNote.updated_at.desc()).limit(500)))
        conflicts = list(session.scalars(select(Conflict).order_by(Conflict.created_at.desc()).limit(300)))
        entities = list(session.scalars(select(Entity).order_by(Entity.priority.desc(), Entity.id.asc()).limit(800)))
        relations = list(session.scalars(select(Relation).order_by(Relation.confidence.desc(), Relation.id.asc()).limit(800)))
        sources = list(session.scalars(select(Source).order_by(Source.retrieved_at.desc()).limit(120)))
        watches = list(session.scalars(select(WebsiteWatch).order_by(WebsiteWatch.id.asc()).limit(500)))
        versions = list(session.scalars(select(KnowledgeVersion).order_by(KnowledgeVersion.created_at.desc()).limit(300)))

        runs_by_topic = defaultdict(list)
        claims_by_topic = defaultdict(list)
        notes_by_topic = defaultdict(list)
        sources_by_topic = defaultdict(list)
        watches_by_topic = defaultdict(list)
        for item in runs:
            runs_by_topic[item.topic_id].append(item)
        for item in claims:
            claims_by_topic[item.topic_id].append(item)
        for item in notes:
            notes_by_topic[item.topic_id].append(item)
        for item in sources:
            sources_by_topic[item.topic_id].append(item)
        for item in watches:
            watches_by_topic[item.topic_id].append(item)

        entity_names = {item.id: item.name for item in entities}

        lines: list[str] = [
            "---",
            "kind: internetboard_handoff",
            "schema_version: 1",
            f"generated_at: {now.isoformat()}",
            f"app_version: {settings.app_version}",
            f"model: {settings.ollama_model}",
            "format: markdown",
            "---",
            "",
            "# InternetBoard AI Handoff",
            "",
            "> This file is an LLM-oriented project handoff generated from the live InternetBoard database.",
            "> Treat human/manual records as higher authority than AI-generated records when they conflict.",
            "",
            "## System Rules",
            "",
            "- Human confirmed/manual knowledge has priority 100.",
            "- Human-edited knowledge has priority at least 80.",
            "- AI-supported facts normally use priority 50.",
            "- AI inference normally uses priority 20.",
            "- Conflicts are explicit and must not be silently overwritten.",
            "- Topic query blocks may contain multiple independent search queries, one per line.",
            "- This handoff is a snapshot; source URLs and timestamps should be re-checked for fresh research.",
            "",
            "## Snapshot Counts",
            "",
            f"- Topics: {len(topics)}",
            f"- Research runs included: {len(runs)}",
            f"- Claims included: {len(claims)}",
            f"- Manual notes included: {len(notes)}",
            f"- Conflicts included: {len(conflicts)}",
            f"- Entities included: {len(entities)}",
            f"- Relations included: {len(relations)}",
            f"- Evidence sources included: {len(sources)}",
            "",
        ]

        for topic in topics:
            lines.extend([f"## Topic {topic.id}: {_safe(topic.name)}", ""])
            lines.append(f"- Enabled: {topic.enabled}")
            lines.append(f"- Priority: {topic.priority}")
            lines.append("- Search queries:")
            for query in _queries(topic.query):
                lines.append(f"  - {query}")
            if topic.description:
                lines.extend(["", "### Research Context / Guardrails", "", _safe(topic.description), ""])

            topic_notes = notes_by_topic.get(topic.id, [])
            if topic_notes:
                lines.extend(["### Manual Notes", ""])
                for note in topic_notes:
                    lines.append(f"#### {_safe(note.title) or 'Manual note'} | priority={note.priority} | updated={_dt(note.updated_at)}")
                    lines.extend(["", _safe(note.content), ""])

            topic_claims = claims_by_topic.get(topic.id, [])
            if topic_claims:
                lines.extend(["### Knowledge Claims", ""])
                for claim in topic_claims:
                    lines.append(
                        f"- [{claim.status}] [origin={claim.origin}] [priority={claim.priority}] "
                        f"[confidence={claim.confidence:.2f}] {_safe(claim.claim_text)}"
                    )
                    if claim.trend:
                        lines.append(f"  - trend: {_safe(claim.trend)}")
                    if claim.prediction:
                        lines.append(f"  - prediction: {_safe(claim.prediction)}")

            topic_runs = runs_by_topic.get(topic.id, [])[:12]
            if topic_runs:
                lines.extend(["", "### Recent Research Runs", ""])
                for run in topic_runs:
                    lines.append(f"#### Run {run.id} | {run.status} | {_dt(run.created_at)}")
                    if run.summary:
                        lines.extend(["", "Summary:", _safe(run.summary)])
                    if run.trend:
                        lines.extend(["", "Trend:", _safe(run.trend)])
                    if run.prediction:
                        lines.extend(["", "Prediction:", _safe(run.prediction)])
                    if run.error:
                        lines.extend(["", "Error:", _safe(run.error)])
                    lines.append("")

            topic_watches = watches_by_topic.get(topic.id, [])
            if topic_watches:
                lines.extend(["### Website Watches", ""])
                for watch in topic_watches:
                    lines.append(
                        f"- {_safe(watch.url)} | enabled={watch.enabled} | "
                        f"last_checked={_dt(watch.last_checked_at)} | last_changed={_dt(watch.last_changed_at)}"
                    )
                lines.append("")

            topic_sources = sources_by_topic.get(topic.id, [])[:15]
            if topic_sources:
                lines.extend(["### Evidence Index and Excerpts", ""])
                for source in topic_sources:
                    lines.append(f"#### Source {source.id}: {_safe(source.title) or _safe(source.url)}")
                    lines.append(f"- URL: {_safe(source.url)}")
                    lines.append(f"- Retrieved: {_dt(source.retrieved_at)}")
                    lines.append(f"- MIME: {_safe(source.mime_type)}")
                    excerpt = _safe(source.content)[:800]
                    if excerpt:
                        lines.extend(["", excerpt, ""])

        if conflicts:
            lines.extend(["# Conflicts", ""])
            for item in conflicts:
                lines.append(f"## Conflict {item.id} | topic={item.topic_id} | status={item.status}")
                lines.append(f"- A: {_safe(item.claim_a_text)}")
                lines.append(f"- B: {_safe(item.claim_b_text)}")
                lines.append(f"- Reason: {_safe(item.reason)}")
                if item.resolution:
                    lines.append(f"- Resolution: {_safe(item.resolution)}")
                lines.append("")

        if relations:
            lines.extend(["# Knowledge Graph Relations", ""])
            for relation in relations:
                subject = entity_names.get(relation.subject_entity_id, f"entity:{relation.subject_entity_id}")
                obj = entity_names.get(relation.object_entity_id, f"entity:{relation.object_entity_id}")
                lines.append(
                    f"- topic={relation.topic_id} | {_safe(subject)} --[{_safe(relation.predicate)}]--> {_safe(obj)} "
                    f"| confidence={relation.confidence:.2f} | priority={relation.priority}"
                )
            lines.append("")

        if versions:
            lines.extend(["# Recent Human/AI Knowledge Version History", ""])
            for version in versions:
                payload = json.dumps(version.snapshot_json, ensure_ascii=False, separators=(",", ":"))
                lines.append(
                    f"- {_dt(version.created_at)} | {version.object_type}:{version.object_id} "
                    f"v{version.version} | actor={version.actor} | {payload[:1800]}"
                )
            lines.append("")

    baseline_path = settings.data_dir / ".bootstrap" / "baseline-v2.8.md"
    if baseline_path.exists():
        lines.extend([
            "# Original Bootstrap Baseline",
            "",
            "> Preserved from the first production bootstrap. It is never overwritten by later image upgrades.",
            "",
            baseline_path.read_text(encoding="utf-8", errors="replace"),
            "",
        ])

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path
'''
write('backend/app/handoff.py', handoff_py)

# Backend config: no API key, multi-query control, export directory.
config = read('backend/app/config.py')
config = config.replace('from pydantic import Field\n', '')
config = re.sub(r'\n\s*api_key: str = Field\([^\n]+\)', '', config, count=1)
if 'max_queries_per_round:' not in config:
    config = config.replace('    max_search_rounds: int = 2\n', '    max_search_rounds: int = 2\n    max_queries_per_round: int = 6\n')
if 'self.data_dir / "exports"' not in config:
    config = config.replace('            self.data_dir / "vector",\n', '            self.data_dir / "vector",\n            self.data_dir / "exports",\n            self.data_dir / ".bootstrap",\n')
write('backend/app/config.py', config)

# Backend main: remove application API-key auth, seed once, add handoff download.
main = read('backend/app/main.py')
main = main.replace('from fastapi import Depends, FastAPI, HTTPException, Query', 'from fastapi import FastAPI, HTTPException, Query')
main = main.replace('from fastapi.responses import ORJSONResponse', 'from fastapi.responses import FileResponse, ORJSONResponse')
main = main.replace('from .security import require_api_key\n', '')
if 'from .bootstrap import bootstrap_defaults_once' not in main:
    main = main.replace('from .config import settings\n', 'from .bootstrap import bootstrap_defaults_once\nfrom .config import settings\n')
if 'from .handoff import build_handoff_markdown' not in main:
    main = main.replace('from .knowledge import record_version\n', 'from .handoff import build_handoff_markdown\nfrom .knowledge import record_version\n')
main = main.replace('    init_db()\n    logger.info("InternetBoard %s started", settings.app_version)', '    init_db()\n    bootstrap = bootstrap_defaults_once()\n    logger.info("Default topic bootstrap: %s", bootstrap)\n    logger.info("InternetBoard %s started", settings.app_version)')
main = main.replace(', dependencies=[Depends(require_api_key)]', '')
if '/api/export/handoff' not in main:
    marker = '@app.get("/api/graph"'
    pos = main.find(marker)
    if pos < 0:
        raise SystemExit('Could not find graph endpoint insertion point in backend/app/main.py')
    endpoint = '''@app.get("/api/export/handoff")\ndef export_handoff():\n    path = build_handoff_markdown()\n    return FileResponse(path, media_type="text/markdown; charset=utf-8", filename=path.name)\n\n\n'''
    main = main[:pos] + endpoint + main[pos:]
write('backend/app/main.py', main)

# Pipeline: support one query per line and include description in AI brief.
pipeline = read('backend/app/pipeline.py')
if 'def _topic_queries(' not in pipeline:
    anchor = 'def _analysis_cache_path(topic: Topic, content_hash: str) -> Path:\n'
    helper = '''def _topic_queries(raw: str) -> list[str]:\n    queries: list[str] = []\n    for line in (raw or "").splitlines():\n        value = normalize_space(line.strip().lstrip("-*\\u2022 "))\n        if value and value not in queries:\n            queries.append(value)\n    if not queries and normalize_space(raw):\n        queries.append(normalize_space(raw))\n    return queries\n\n\ndef _topic_brief(topic: Topic) -> str:\n    parts = [topic.query.strip()]\n    if topic.description.strip():\n        parts.append("Research context and guardrails:\\n" + topic.description.strip())\n    return "\\n\\n".join(part for part in parts if part)\n\n\n'''
    if anchor not in pipeline:
        raise SystemExit('Could not find pipeline insertion point')
    pipeline = pipeline.replace(anchor, helper + anchor, 1)
pipeline = pipeline.replace('f"v1|{topic.id}|{topic.name}|{topic.query}|{settings.ollama_model}"', 'f"v2|{topic.id}|{topic.name}|{topic.query}|{topic.description}|{settings.ollama_model}"')
pipeline = pipeline.replace('query=topic.query,', 'query=_topic_brief(topic),')
pipeline = pipeline.replace('pending_queries = [topic.query]', 'pending_queries = _topic_queries(topic.query)')
pipeline = pipeline.replace('for query in pending_queries[:3]:', 'for query in pending_queries[: settings.max_queries_per_round]:')
write('backend/app/pipeline.py', pipeline)

# Backend image now uses repository root build context and includes bootstrap assets.
dockerfile = '''FROM python:3.12.11-slim-bookworm\n\nENV PYTHONDONTWRITEBYTECODE=1 \\\n    PYTHONUNBUFFERED=1 \\\n    PIP_NO_CACHE_DIR=1 \\\n    PYTHONPATH=/app\n\nRUN apt-get update \\\n    && apt-get install -y --no-install-recommends curl ca-certificates tini tzdata \\\n    && rm -rf /var/lib/apt/lists/*\n\nWORKDIR /app\nCOPY backend/requirements.txt /app/requirements.txt\nRUN pip install --no-cache-dir -r /app/requirements.txt\n\nCOPY backend/app /app/app\nCOPY config /app/config\nCOPY seed /app/seed\n\nRUN mkdir -p /data\n\nENTRYPOINT ["/usr/bin/tini", "--"]\nCMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]\n'''
write('backend/Dockerfile', dockerfile)

req = read('backend/requirements.txt')
if 'PyYAML==' not in req:
    req = req.rstrip() + '\nPyYAML==6.0.2\n'
write('backend/requirements.txt', req)

# API key shim is no longer used; remove it to avoid misleading future maintenance.
security = ROOT / 'backend/app/security.py'
if security.exists():
    security.unlink()
    print('[delete] backend/app/security.py')

# Frontend: no auth prompt, editable multi-query topics, one-click LLM handoff export.
index_html = '''<!doctype html>\n<html lang="zh-CN">\n<head>\n  <meta charset="utf-8">\n  <meta name="viewport" content="width=device-width, initial-scale=1">\n  <title>InternetBoard</title>\n  <link rel="stylesheet" href="/styles.css">\n</head>\n<body>\n  <header class="topbar">\n    <div>\n      <div class="eyebrow">LOCAL RESEARCH AGENT</div>\n      <h1>InternetBoard</h1>\n    </div>\n    <div class="top-actions">\n      <span id="modelBadge" class="badge muted">\u6a21\u578b\u68c0\u6d4b\u4e2d</span>\n      <button id="exportBtn" class="ghost">\u5bfc\u51fa\u4ea4\u63a5</button>\n      <button id="refreshBtn">\u5237\u65b0</button>\n    </div>\n  </header>\n  <main>\n    <section class="stats" id="stats"></section>\n\n    <section class="grid two">\n      <article class="panel">\n        <div class="panel-head"><h2>\u4e13\u9898</h2><span>\u6bcf\u65e5 03:00 \u81ea\u52a8\u7814\u7a76\uff0c\u6bcf\u884c\u4e00\u4e2a\u67e5\u8be2\u70b9</span></div>\n        <form id="topicForm" class="form">\n          <input name="topic_id" type="hidden">\n          <input name="name" placeholder="\u4e13\u9898\u540d\u79f0" required>\n          <textarea name="query" rows="5" placeholder="\u6bcf\u884c\u4e00\u4e2a\u72ec\u7acb\u67e5\u8be2\u8bed\u53e5 / \u7814\u7a76\u95ee\u9898" required></textarea>\n          <textarea name="description" rows="4" placeholder="\u7814\u7a76\u80cc\u666f\u3001\u5f53\u524d\u72b6\u6001\u3001\u5224\u65ad\u8fb9\u754c\u548c\u6ce8\u610f\u4e8b\u9879\uff08\u53ef\u9009\uff09"></textarea>\n          <div class="actions">\n            <button id="topicSubmit" type="submit">\u65b0\u589e\u4e13\u9898</button>\n            <button id="topicCancel" type="button" class="ghost" hidden>\u53d6\u6d88\u7f16\u8f91</button>\n          </div>\n        </form>\n        <div id="topics" class="stack"></div>\n      </article>\n      <article class="panel">\n        <div class="panel-head"><h2>\u4efb\u52a1\u72b6\u6001</h2><span>\u65ad\u70b9\u4e0e\u5931\u8d25\u72b6\u6001\u4fdd\u7559</span></div>\n        <div id="runs" class="stack"></div>\n      </article>\n    </section>\n    <section class="grid two">\n      <article class="panel">\n        <div class="panel-head"><h2>\u4eba\u5de5\u8f93\u5165</h2><span>\u4f18\u5148\u7ea7 100</span></div>\n        <form id="noteForm" class="form">\n          <select name="topic_id" id="noteTopic" required></select>\n          <input name="title" placeholder="\u6807\u9898\uff08\u53ef\u9009\uff09">\n          <textarea name="content" rows="6" placeholder="\u8f93\u5165\u5df2\u77e5\u4fe1\u606f\u3001\u7ebf\u7d22\u3001\u4eba\u5de5\u7ed3\u8bba\u6216\u9700\u8981 AI \u6574\u7406\u7684\u5185\u5bb9" required></textarea>\n          <button type="submit">\u5199\u5165\u77e5\u8bc6\u5e93\u8f93\u5165</button>\n        </form>\n      </article>\n      <article class="panel">\n        <div class="panel-head"><h2>\u7f51\u9875\u53d8\u5316\u76d1\u6d4b</h2><span>\u53d8\u5316\u540e\u81ea\u52a8\u89e6\u53d1\u7814\u7a76</span></div>\n        <form id="watchForm" class="form">\n          <select name="topic_id" id="watchTopic" required></select>\n          <input name="url" type="url" placeholder="https://example.com/page" required>\n          <button type="submit">\u6dfb\u52a0\u76d1\u6d4b</button>\n        </form>\n        <div id="watches" class="stack small"></div>\n      </article>\n    </section>\n    <section class="panel">\n      <div class="panel-head"><h2>\u6700\u65b0\u77e5\u8bc6</h2><span>\u4eba\u5de5 &gt; AI\u4e8b\u5b9e &gt; AI\u63a8\u6d4b</span></div>\n      <div id="claims" class="claims"></div>\n    </section>\n    <section class="grid two">\n      <article class="panel">\n        <div class="panel-head"><h2>\u51b2\u7a81</h2><span>\u53d1\u73b0\u51b2\u7a81\u53ea\u8bb0\u5f55\uff0c\u4e0d\u9759\u9ed8\u8986\u76d6</span></div>\n        <div id="conflicts" class="stack"></div>\n      </article>\n      <article class="panel">\n        <div class="panel-head"><h2>\u8bc1\u636e</h2><span>\u539f\u59cb\u5185\u5bb9\u53ef\u8ffd\u6eaf</span></div>\n        <div id="sources" class="stack"></div>\n      </article>\n    </section>\n    <section class="panel">\n      <div class="panel-head"><h2>\u77e5\u8bc6\u56fe\u8c31</h2><span>Entity / Relation</span></div>\n      <svg id="graph" viewBox="0 0 1000 520" role="img" aria-label="Knowledge graph"></svg>\n    </section>\n  </main>\n\n  <div id="toast" class="toast"></div>\n  <script src="/app.js"></script>\n</body>\n</html>\n'''
write('frontend/index.html', index_html)

app_js = read('frontend/app.js')
# Remove API-key state/helpers and simplify fetch wrapper.
app_js = re.sub(
    r"const state = \{ apiKey: localStorage\.getItem\('internetboard_api_key'\) \|\| '', dashboard: null, system: null \};",
    "const state = { dashboard: null, system: null };",
    app_js,
    count=1,
)
app_js = re.sub(
    r"function ensureKey\(\) \{.*?\n\}\nasync function api\(path, options=\{\}\) \{.*?\n\}",
    """async function api(path, options={}) {\n  const headers = { ...(options.headers || {}) };\n  if (options.body && !(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';\n  const res = await fetch(path, {...options, headers});\n  if (!res.ok) {\n    let msg = `${res.status} ${res.statusText}`;\n    try { const j = await res.json(); msg = j.detail || msg; } catch (_) {}\n    throw new Error(msg);\n  }\n  return res.status === 204 ? null : res.json();\n}""",
    app_js,
    count=1,
    flags=re.S,
)
# Topic cards: preserve query lines and add edit action.
old_topic_block = '''        <div><div class="item-title">${esc(t.name)}</div><div class="meta">${esc(t.query)} · \u4f18\u5148\u7ea7 ${t.priority} · ${t.enabled?'\u542f\u7528':'\u505c\u7528'}</div></div>\n        <div class="actions"><button onclick="runTopic(${t.id})">\u7acb\u5373\u5237\u65b0</button></div>'''
new_topic_block = '''        <div><div class="item-title">${esc(t.name)}</div><div class="meta">${esc(t.query).replace(/\\n/g,'<br>')}<br>\u4f18\u5148\u7ea7 ${t.priority} · ${t.enabled?'\u542f\u7528':'\u505c\u7528'}</div></div>\n        <div class="actions"><button class="ghost" onclick="editTopic(${t.id})">\u7f16\u8f91</button><button onclick="runTopic(${t.id})">\u7acb\u5373\u5237\u65b0</button></div>'''
if old_topic_block not in app_js:
    raise SystemExit('Could not patch topic render block in frontend/app.js')
app_js = app_js.replace(old_topic_block, new_topic_block, 1)
# Remove old API key button handler.
app_js = re.sub(r"\$\('#apiKeyBtn'\)\.addEventListener\('click', \(\) => \{.*?\n\}\);\n", '', app_js, count=1, flags=re.S)
# Replace old topic submit handler with edit/create behavior and export button.
old_submit = '''$('#refreshBtn').addEventListener('click', load);\n$('#topicForm').addEventListener('submit', async e => {\n  e.preventDefault(); const f=new FormData(e.currentTarget);\n  try { await api('/api/topics',{method:'POST',body:JSON.stringify({name:f.get('name'),query:f.get('query'),description:'',enabled:true,priority:50})}); e.currentTarget.reset(); toast('\u4e13\u9898\u5df2\u521b\u5efa'); await load(); } catch(err){ toast(err.message); }\n});'''
new_submit = '''function resetTopicForm() {\n  const form = $('#topicForm');\n  form.reset();\n  form.elements.topic_id.value = '';\n  $('#topicSubmit').textContent = '\u65b0\u589e\u4e13\u9898';\n  $('#topicCancel').hidden = true;\n}\nfunction editTopic(id) {\n  const topic = (state.dashboard?.topics || []).find(t => t.id === id);\n  if (!topic) return;\n  const form = $('#topicForm');\n  form.elements.topic_id.value = topic.id;\n  form.elements.name.value = topic.name || '';\n  form.elements.query.value = topic.query || '';\n  form.elements.description.value = topic.description || '';\n  $('#topicSubmit').textContent = '\u4fdd\u5b58\u4e13\u9898';\n  $('#topicCancel').hidden = false;\n  form.scrollIntoView({behavior:'smooth', block:'center'});\n}\nwindow.editTopic = editTopic;\n$('#topicCancel').addEventListener('click', resetTopicForm);\n$('#exportBtn').addEventListener('click', () => { window.location.href = '/api/export/handoff'; });\n$('#refreshBtn').addEventListener('click', load);\n$('#topicForm').addEventListener('submit', async e => {\n  e.preventDefault(); const f=new FormData(e.currentTarget);\n  const id = Number(f.get('topic_id') || 0);\n  const payload = {name:f.get('name'),query:f.get('query'),description:f.get('description')||'',enabled:true,priority:50};\n  try {\n    if (id) await api(`/api/topics/${id}`,{method:'PATCH',body:JSON.stringify(payload)});\n    else await api('/api/topics',{method:'POST',body:JSON.stringify(payload)});\n    resetTopicForm(); toast(id?'\u4e13\u9898\u5df2\u66f4\u65b0':'\u4e13\u9898\u5df2\u521b\u5efa'); await load();\n  } catch(err){ toast(err.message); }\n});'''
if old_submit not in app_js:
    raise SystemExit('Could not patch topic form handler in frontend/app.js')
app_js = app_js.replace(old_submit, new_submit, 1)
write('frontend/app.js', app_js)

# Production Compose: registry-only runtime; NAS never builds application source.
compose = '''x-logging: &default-logging\n  driver: json-file\n  options:\n    max-size: "20m"\n    max-file: "3"\n\nx-app-env: &app-env\n  TZ: ${TZ}\n  DATABASE_URL: postgresql+psycopg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB}\n  REDIS_URL: redis://redis:6379/0\n  DATA_DIR: /data\n  OLLAMA_BASE_URL: http://ollama:11434\n  OLLAMA_MODEL: ${OLLAMA_MODEL}\n  OLLAMA_CONTEXT_LENGTH: ${OLLAMA_CONTEXT_LENGTH}\n  OLLAMA_KEEP_ALIVE: ${OLLAMA_KEEP_ALIVE}\n  OLLAMA_TIMEOUT_SECONDS: ${OLLAMA_TIMEOUT_SECONDS}\n  OLLAMA_JSON_RETRIES: ${OLLAMA_JSON_RETRIES}\n  MAX_SEARCH_ROUNDS: ${MAX_SEARCH_ROUNDS}\n  MAX_QUERIES_PER_ROUND: ${MAX_QUERIES_PER_ROUND}\n  MAX_RESULTS_PER_QUERY: ${MAX_RESULTS_PER_QUERY}\n  MAX_SOURCES_PER_RUN: ${MAX_SOURCES_PER_RUN}\n  MAX_TOTAL_AI_CHUNKS: ${MAX_TOTAL_AI_CHUNKS}\n  MAX_AI_CHUNKS_PER_SOURCE: ${MAX_AI_CHUNKS_PER_SOURCE}\n  CHUNK_CHARS: ${CHUNK_CHARS}\n  CHUNK_OVERLAP_CHARS: ${CHUNK_OVERLAP_CHARS}\n  ALLOW_PRIVATE_URLS: ${ALLOW_PRIVATE_URLS}\n  SEARXNG_URL: ${SEARXNG_URL}\n  SCHEDULER_HOUR: ${SCHEDULER_HOUR}\n  SCHEDULER_MINUTE: ${SCHEDULER_MINUTE}\n  WEBSITE_WATCH_MINUTES: ${WEBSITE_WATCH_MINUTES}\n\nservices:\n  postgres:\n    image: postgres:16-alpine\n    container_name: internetboard-postgres\n    restart: unless-stopped\n    logging: *default-logging\n    environment:\n      TZ: ${TZ}\n      POSTGRES_DB: ${POSTGRES_DB}\n      POSTGRES_USER: ${POSTGRES_USER}\n      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}\n    command: ["postgres", "-c", "shared_buffers=256MB", "-c", "effective_cache_size=1GB", "-c", "max_connections=50"]\n    volumes:\n      - ${APP_ROOT}/postgres:/var/lib/postgresql/data\n    healthcheck:\n      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER} -d ${POSTGRES_DB}"]\n      interval: 10s\n      timeout: 5s\n      retries: 12\n    networks: [internetboard]\n\n  redis:\n    image: redis:7.4-alpine\n    container_name: internetboard-redis\n    restart: unless-stopped\n    logging: *default-logging\n    command: ["redis-server", "--appendonly", "yes", "--maxmemory-policy", "noeviction"]\n    environment:\n      TZ: ${TZ}\n    volumes:\n      - ${APP_ROOT}/redis:/data\n    healthcheck:\n      test: ["CMD", "redis-cli", "ping"]\n      interval: 10s\n      timeout: 5s\n      retries: 12\n    networks: [internetboard]\n\n  ollama:\n    image: ollama/ollama:latest\n    pull_policy: always\n    container_name: internetboard-ollama\n    restart: unless-stopped\n    logging: *default-logging\n    environment:\n      TZ: ${TZ}\n      OLLAMA_HOST: 0.0.0.0:11434\n      OLLAMA_KEEP_ALIVE: ${OLLAMA_KEEP_ALIVE}\n      OLLAMA_CONTEXT_LENGTH: ${OLLAMA_CONTEXT_LENGTH}\n      OLLAMA_NUM_PARALLEL: "1"\n      OLLAMA_MAX_LOADED_MODELS: "1"\n      OLLAMA_MAX_QUEUE: "32"\n      OLLAMA_FLASH_ATTENTION: "true"\n      OLLAMA_KV_CACHE_TYPE: q8_0\n      OLLAMA_LOAD_TIMEOUT: 20m\n      NVIDIA_VISIBLE_DEVICES: all\n    volumes:\n      - ${APP_ROOT}/ollama:/root/.ollama\n    deploy:\n      resources:\n        reservations:\n          devices:\n            - driver: nvidia\n              count: all\n              capabilities: [gpu]\n    healthcheck:\n      test: ["CMD", "ollama", "list"]\n      interval: 20s\n      timeout: 10s\n      retries: 30\n      start_period: 30s\n    networks: [internetboard]\n\n  model-init:\n    image: ollama/ollama:latest\n    pull_policy: always\n    container_name: internetboard-model-init\n    restart: "no"\n    logging: *default-logging\n    depends_on:\n      ollama:\n        condition: service_healthy\n    environment:\n      OLLAMA_HOST: http://ollama:11434\n      OLLAMA_MODEL: ${OLLAMA_MODEL}\n    entrypoint: ["/bin/sh", "-c"]\n    command: ['ollama pull "$${OLLAMA_MODEL}"']\n    networks: [internetboard]\n\n  backend:\n    image: milesxia/internetboard-backend:latest\n    pull_policy: always\n    container_name: internetboard-backend\n    restart: unless-stopped\n    logging: *default-logging\n    depends_on:\n      postgres:\n        condition: service_healthy\n      redis:\n        condition: service_healthy\n      ollama:\n        condition: service_healthy\n      model-init:\n        condition: service_completed_successfully\n    environment: *app-env\n    volumes:\n      - ${APP_ROOT}/data:/data\n    healthcheck:\n      test: ["CMD", "curl", "-fsS", "http://127.0.0.1:8000/health"]\n      interval: 20s\n      timeout: 10s\n      retries: 20\n      start_period: 30s\n    networks: [internetboard]\n\n  worker:\n    image: milesxia/internetboard-worker:latest\n    pull_policy: always\n    container_name: internetboard-worker\n    restart: unless-stopped\n    logging: *default-logging\n    depends_on:\n      backend:\n        condition: service_healthy\n    environment: *app-env\n    volumes:\n      - ${APP_ROOT}/data:/data\n    command: ["celery", "-A", "app.tasks.celery_app", "worker", "--loglevel=INFO", "--concurrency=1", "--prefetch-multiplier=1", "--max-tasks-per-child=10"]\n    networks: [internetboard]\n\n  scheduler:\n    image: milesxia/internetboard-scheduler:latest\n    pull_policy: always\n    container_name: internetboard-scheduler\n    restart: unless-stopped\n    logging: *default-logging\n    depends_on:\n      backend:\n        condition: service_healthy\n    environment: *app-env\n    volumes:\n      - ${APP_ROOT}/data:/data\n    command: ["celery", "-A", "app.tasks.celery_app", "beat", "--loglevel=INFO", "--schedule=/data/celerybeat-schedule"]\n    networks: [internetboard]\n\n  frontend:\n    image: milesxia/internetboard-frontend:latest\n    pull_policy: always\n    container_name: internetboard-frontend\n    restart: unless-stopped\n    logging: *default-logging\n    depends_on:\n      backend:\n        condition: service_healthy\n    ports:\n      - "${WEB_PORT}:80"\n    healthcheck:\n      test: ["CMD-SHELL", "wget -qO- http://127.0.0.1/health >/dev/null || exit 1"]\n      interval: 20s\n      timeout: 10s\n      retries: 10\n    networks: [internetboard]\n\nnetworks:\n  internetboard:\n    name: internetboard\n    driver: bridge\n'''
write('docker-compose.yml', compose)

env_example = '''COMPOSE_PROJECT_NAME=internetboard\nAPP_ROOT=/share/Container/internetboard\nTZ=Asia/Shanghai\nWEB_PORT=8733\nPOSTGRES_DB=internetboard\nPOSTGRES_USER=internetboard\nPOSTGRES_PASSWORD=REPLACE_WITH_RANDOM_PASSWORD\nOLLAMA_MODEL=qwen3.8:27b-q4_K_M\nOLLAMA_CONTEXT_LENGTH=8192\nOLLAMA_KEEP_ALIVE=10m\nOLLAMA_TIMEOUT_SECONDS=900\nOLLAMA_JSON_RETRIES=3\nMAX_SEARCH_ROUNDS=2\nMAX_QUERIES_PER_ROUND=6\nMAX_RESULTS_PER_QUERY=8\nMAX_SOURCES_PER_RUN=8\nMAX_TOTAL_AI_CHUNKS=12\nMAX_AI_CHUNKS_PER_SOURCE=3\nCHUNK_CHARS=5500\nCHUNK_OVERLAP_CHARS=500\nALLOW_PRIVATE_URLS=false\nSEARXNG_URL=\nSCHEDULER_HOUR=3\nSCHEDULER_MINUTE=0\nWEBSITE_WATCH_MINUTES=60\n'''
write('.env.example', env_example)

# CI: validate source, then build backend from repository root so config/seed are packaged.
workflow = '''name: Build and Push InternetBoard\n\non:\n  push:\n    branches:\n      - main\n    tags:\n      - "v*"\n  workflow_dispatch:\n\npermissions:\n  contents: read\n\njobs:\n  validate:\n    name: Validate Production Source\n    runs-on: ubuntu-latest\n    steps:\n      - name: Checkout repository\n        uses: actions/checkout@v6\n      - name: Set up Python\n        uses: actions/setup-python@v6\n        with:\n          python-version: "3.12"\n      - name: Python syntax check\n        run: python -m compileall -q backend/app scripts/validate_production.py\n      - name: Production invariants\n        run: python scripts/validate_production.py\n      - name: Compose validation\n        run: docker compose --env-file .env.example config -q\n\n  docker:\n    name: Build InternetBoard Images\n    needs: validate\n    runs-on: ubuntu-latest\n    steps:\n      - name: Checkout repository\n        uses: actions/checkout@v6\n      - name: Set up Docker Buildx\n        uses: docker/setup-buildx-action@v4\n      - name: Login to Docker Hub\n        uses: docker/login-action@v4\n        with:\n          username: ${{ secrets.DOCKERHUB_USERNAME }}\n          password: ${{ secrets.DOCKERHUB_TOKEN }}\n      - name: Build and push backend worker scheduler\n        uses: docker/build-push-action@v7\n        with:\n          context: .\n          file: ./backend/Dockerfile\n          platforms: linux/amd64\n          push: true\n          tags: |\n            ${{ secrets.DOCKERHUB_USERNAME }}/internetboard-backend:latest\n            ${{ secrets.DOCKERHUB_USERNAME }}/internetboard-backend:v1.0\n            ${{ secrets.DOCKERHUB_USERNAME }}/internetboard-worker:latest\n            ${{ secrets.DOCKERHUB_USERNAME }}/internetboard-worker:v1.0\n            ${{ secrets.DOCKERHUB_USERNAME }}/internetboard-scheduler:latest\n            ${{ secrets.DOCKERHUB_USERNAME }}/internetboard-scheduler:v1.0\n          cache-from: type=gha\n          cache-to: type=gha,mode=max\n      - name: Build and push frontend\n        uses: docker/build-push-action@v7\n        with:\n          context: ./frontend\n          platforms: linux/amd64\n          push: true\n          tags: |\n            ${{ secrets.DOCKERHUB_USERNAME }}/internetboard-frontend:latest\n            ${{ secrets.DOCKERHUB_USERNAME }}/internetboard-frontend:v1.0\n          cache-from: type=gha\n          cache-to: type=gha,mode=max\n'''
write('.github/workflows/dockerhub.yml', workflow)

validate_py = r'''from pathlib import Path


def must(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")

compose = text("docker-compose.yml")
main = text("backend/app/main.py")
dockerfile = text("backend/Dockerfile")
frontend = text("frontend/app.js")
index = text("frontend/index.html")
workflow = text(".github/workflows/dockerhub.yml")
topics = text("config/topics.yml")

must("build:" not in compose, "Production Compose must not build on the NAS")
for image in (
    "milesxia/internetboard-backend:latest",
    "milesxia/internetboard-worker:latest",
    "milesxia/internetboard-scheduler:latest",
    "milesxia/internetboard-frontend:latest",
    "ollama/ollama:latest",
):
    must(image in compose, f"Missing production image: {image}")

runtime_text = "\n".join((main, frontend, index, compose, text(".env.example")))
must("INTERNETBOARD_API_KEY" not in runtime_text, "API-key UI/runtime dependency must be removed")
must("X-API-Key" not in runtime_text, "API-key header must be removed")
must("bootstrap_defaults_once" in main, "One-time bootstrap is not wired into startup")
must("/api/export/handoff" in main, "Handoff export endpoint is missing")
must("exportBtn" in index and "exportBtn" in frontend, "Handoff export UI is missing")
must("COPY config /app/config" in dockerfile, "Backend image does not contain config defaults")
must("COPY seed /app/seed" in dockerfile, "Backend image does not contain seed assets")
must("context: ." in workflow and "file: ./backend/Dockerfile" in workflow, "Backend CI build context is not repository root")
must(topics.count("- slug:") >= 5, "Expected built-in topic definitions are missing")
print("InternetBoard production invariants: PASS")
'''
write('scripts/validate_production.py', validate_py)

# Installer: pull-only, preserve data, no application API key.
install_sh = '''#!/bin/sh\nset -eu\n\nTARGET="/share/Container/internetboard"\nSOURCE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\nfail() { echo "[FAIL] $*" >&2; exit 1; }\nok() { echo "[ OK ] $*"; }\ninfo() { echo "[....] $*"; }\n\ncommand -v docker >/dev/null 2>&1 || fail "Docker/Container Station command not found"\nif docker compose version >/dev/null 2>&1; then COMPOSE="docker compose"; elif command -v docker-compose >/dev/null 2>&1; then COMPOSE="docker-compose"; else fail "Docker Compose command not found"; fi\nARCH=$(uname -m)\n[ "$ARCH" = "x86_64" ] || [ "$ARCH" = "amd64" ] || fail "Unsupported architecture: $ARCH"\nMEM_KB=$(awk '/MemTotal/ {print $2}' /proc/meminfo)\n[ "${MEM_KB:-0}" -ge 35000000 ] || fail "At least 35GB RAM is required for this production profile"\n\nmkdir -p "$TARGET"\nif [ "$SOURCE" != "$TARGET" ]; then\n  OLD_ENV=""\n  if [ -f "$TARGET/.env" ]; then OLD_ENV=$(mktemp); cp "$TARGET/.env" "$OLD_ENV"; fi\n  cp -a "$SOURCE/." "$TARGET/"\n  if [ -n "$OLD_ENV" ]; then cp "$OLD_ENV" "$TARGET/.env"; rm -f "$OLD_ENV"; fi\nfi\ncd "$TARGET"\nmkdir -p data/source data/chunk data/knowledge data/vector data/history data/conflict data/exports data/.bootstrap postgres redis ollama backups\n\nif [ ! -f .env ]; then\n  if [ -f "$TARGET/postgres/PG_VERSION" ]; then\n    fail "Existing PostgreSQL data detected but .env is missing. Create .env with the existing database password before using install.sh."\n  fi\n  cp .env.example .env\n  DBPASS=$(od -An -N24 -tx1 /dev/urandom | tr -d ' \\n')\n  sed -i "s/REPLACE_WITH_RANDOM_PASSWORD/${DBPASS}/" .env\n  chmod 600 .env || true\nfi\n\ninfo "Validating Compose"\n$COMPOSE config -q\nok "Compose syntax valid"\n\ninfo "Pulling production images"\n$COMPOSE pull\n\ninfo "Starting PostgreSQL, Redis and Ollama"\n$COMPOSE up -d postgres redis ollama\n\ninfo "Starting model initialization"\n$COMPOSE up -d model-init\nMODEL=$(awk -F= '/^OLLAMA_MODEL=/{print $2}' .env)\nMODEL_OK=0\nI=0\nwhile [ "$I" -lt 480 ]; do\n  if docker exec internetboard-ollama ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -Fx "$MODEL" >/dev/null 2>&1; then MODEL_OK=1; break; fi\n  I=$((I+1)); sleep 30\ndone\n[ "$MODEL_OK" -eq 1 ] || fail "Model pull did not complete successfully"\n\ninfo "Starting InternetBoard"\n$COMPOSE up -d backend worker scheduler frontend\nBACKEND_OK=0\nI=0\nwhile [ "$I" -lt 180 ]; do\n  STATUS=$(docker inspect -f '{{.State.Health.Status}}' internetboard-backend 2>/dev/null || true)\n  if [ "$STATUS" = "healthy" ]; then BACKEND_OK=1; break; fi\n  I=$((I+1)); sleep 5\ndone\n[ "$BACKEND_OK" -eq 1 ] || { $COMPOSE logs --tail=160 backend model-init ollama >&2 || true; fail "Backend did not become healthy"; }\nPORT=$(awk -F= '/^WEB_PORT=/{print $2}' .env)\necho\necho "InternetBoard Production is ready."\necho "Web: http://<NAS-IP>:${PORT}"\necho "Default topics are seeded only once. Future image upgrades never overwrite user edits."\necho "Use the Export Handoff button in the web UI to create an LLM-ready Markdown handoff."\n'''
write('install.sh', install_sh)
(ROOT / 'install.sh').chmod(0o755)

# Update the two most misleading docs so the repository no longer advertises the old path.
qnap_doc = '''# InternetBoard v1.0 Production - QNAP TS-673A\n\n## Runtime\n\n- QNAP path: `/share/Container/internetboard`\n- Web: `http://NAS_IP:8733`\n- AI: `qwen3.8:27b-q4_K_M` only\n- Ollama: `ollama/ollama:latest`\n- InternetBoard images: `milesxia/internetboard-*:latest`\n- NAS never builds application source.\n\n## First start\n\nOn the first start only, if the database has no topics and the durable bootstrap marker does not exist, the built-in `config/topics.yml` definitions are inserted into PostgreSQL. After that, `/data/.bootstrap/topics-v1.json` prevents any image upgrade from re-applying or overwriting defaults. User edits remain authoritative.\n\n## Handoff export\n\nThe web UI contains an `Export Handoff` action. It generates an LLM-oriented Markdown snapshot under `/share/Container/internetboard/data/exports` and downloads the same file to the browser. It contains topic/query definitions, manual notes, claims, recent research summaries, conflicts, graph relations and evidence excerpts.\n\n## Upgrade\n\n```bash\ncd /share/Container/internetboard\ndocker compose pull\ndocker compose up -d\n```\n\nPersistent directories under `/share/Container/internetboard` are not removed during an image upgrade.\n\n## Access\n\nThere is no InternetBoard application API-key prompt in the trusted-LAN profile. Do not expose the service directly to the public Internet; use HTTPS and access control at the reverse proxy/VPN layer if remote access is required.\n'''
write('QNAP-DEPLOY.md', qnap_doc)

codespaces_doc = '''# Codespaces -> GitHub Actions -> Docker Hub\n\nA push to `main` triggers `.github/workflows/dockerhub.yml`. CI validates the production invariants and then publishes:\n\n- `milesxia/internetboard-backend:latest`\n- `milesxia/internetboard-worker:latest`\n- `milesxia/internetboard-scheduler:latest`\n- `milesxia/internetboard-frontend:latest`\n\nThe backend build uses the repository root as its Docker build context so `config/` and `seed/` are packaged into the image. QNAP only pulls published images.\n\nDocker Hub credentials remain GitHub Actions repository secrets and are not stored in source code.\n'''
write('CODESPACES-DOCKERHUB.md', codespaces_doc)

# README targeted cleanup.
readme = read('README.md')
readme = readme.replace('`ollama/ollama:0.32.13`', '`ollama/ollama:latest`')
readme = readme.replace('Web port: `8788` by default.', 'Web port: `8733` by default.')
readme = readme.replace('builds the application images, ', 'pulls the published application images, ')
readme = re.sub(r'\nThe generated API key is stored in `\.env`.*?browser local storage\.\n', '\nThe trusted-LAN profile does not use an application API-key prompt.\n', readme, count=1)
readme = readme.replace('Protected `/api/*` routes require `X-API-Key`.', 'The trusted-LAN profile has no application-level API key; use reverse-proxy/VPN access control before any Internet exposure.')
if '## One-time bootstrap' not in readme:
    insert_at = readme.find('## Core behavior')
    block = '''## One-time bootstrap\n\nBuilt-in topics and query points from `config/topics.yml` are inserted only once when PostgreSQL has no topics. A durable marker under `data/.bootstrap` prevents future image updates from overwriting user edits. Each line in a topic query block is executed as an independent first-round web query.\n\n## LLM handoff export\n\nThe dashboard `Export Handoff` action produces a versioned Markdown snapshot under `data/exports` and downloads it to the browser. The export is designed for uploading into another LLM and includes topic/query definitions, manual knowledge, claims, recent runs, conflicts, graph relations and evidence excerpts.\n\n'''
    if insert_at >= 0:
        readme = readme[:insert_at] + block + readme[insert_at:]
write('README.md', readme)

# Final static self-checks.
import py_compile
for path in ('backend/app/bootstrap.py', 'backend/app/handoff.py', 'backend/app/main.py', 'backend/app/config.py', 'backend/app/pipeline.py', 'scripts/validate_production.py'):
    py_compile.compile(str(ROOT / path), doraise=True)

# Run invariant script in-process.
exec(compile(read('scripts/validate_production.py'), 'scripts/validate_production.py', 'exec'), {'__name__': '__main__'})
print('PATCH COMPLETE')

if ARGS.push:
    paths = [
        'backend/app/bootstrap.py',
        'backend/app/handoff.py',
        'backend/app/config.py',
        'backend/app/main.py',
        'backend/app/pipeline.py',
        'backend/app/security.py',
        'backend/Dockerfile',
        'backend/requirements.txt',
        'frontend/index.html',
        'frontend/app.js',
        'docker-compose.yml',
        '.env.example',
        '.github/workflows/dockerhub.yml',
        'scripts/validate_production.py',
        'install.sh',
        'QNAP-DEPLOY.md',
        'CODESPACES-DOCKERHUB.md',
        'README.md',
    ]
    subprocess.run(['git', 'diff', '--check'], check=True)
    subprocess.run(['git', 'add', '-A', '--', *paths], check=True)
    staged = subprocess.run(['git', 'diff', '--cached', '--quiet'])
    if staged.returncode == 0:
        print('No staged changes; repository may already contain this production fix.')
    else:
        subprocess.run([
            'git', 'commit', '-m',
            'Production: one-time bootstrap, editable topics and LLM handoff export'
        ], check=True)
        subprocess.run(['git', 'push', 'origin', 'main'], check=True)
        print('PUSH COMPLETE')
