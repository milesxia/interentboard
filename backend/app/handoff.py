from __future__ import annotations

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
        visual_source_count = sum(1 for item in sources if (item.metadata_json or {}).get("visual"))

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
            f"- Visual evidence included: {visual_source_count}",
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
                    meta = source.metadata_json or {}
                    if meta.get("visual"):
                        lines.append("- Visual evidence: yes")
                        lines.append(f"- Visual kind: {_safe(meta.get('visual_kind'))}")
                        if meta.get("page_number"):
                            lines.append(f"- PDF page: {meta.get('page_number')}")
                        lines.append(f"- Visual hash: {_safe(meta.get('visual_hash'))}")
                        if meta.get("parent_source_id"):
                            lines.append(f"- Parent source id: {meta.get('parent_source_id')}")
                    excerpt = _safe(source.content)[:1200 if meta.get("visual") else 800]
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
