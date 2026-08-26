from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .models import (
    Chunk,
    Claim,
    ClaimEntity,
    ClaimEvidence,
    Conflict,
    Entity,
    KnowledgeVersion,
    Relation,
    Source,
    utcnow,
)
from .schemas import ChunkAnalysis, ExtractedClaim, ExtractedEntity, ExtractedRelation, RunSynthesis
from .utils import atomic_write_json, normalize_entity_name, normalize_space, sha256_text


def record_version(session: Session, object_type: str, object_id: int, actor: str, snapshot: dict) -> None:
    latest = session.scalar(
        select(func.max(KnowledgeVersion.version)).where(
            KnowledgeVersion.object_type == object_type,
            KnowledgeVersion.object_id == object_id,
        )
    )
    pending = session.info.setdefault("history_files", [])
    pending_versions = [
        item["version"] for item in pending
        if item["object_type"] == object_type and item["object_id"] == object_id
    ]
    version = max([latest or 0, *pending_versions]) + 1
    payload = {
        "object_type": object_type,
        "object_id": object_id,
        "version": version,
        "actor": actor,
        "snapshot": snapshot,
    }
    session.add(
        KnowledgeVersion(
            object_type=object_type,
            object_id=object_id,
            version=version,
            actor=actor,
            snapshot_json=snapshot,
        )
    )
    pending.append(payload)


def upsert_entity(session: Session, item: ExtractedEntity, priority: int = 50) -> Entity:
    normalized = normalize_entity_name(item.name)
    entity = session.scalar(
        select(Entity).where(Entity.entity_type == item.type, Entity.normalized_name == normalized)
    )
    if entity:
        if priority >= entity.priority:
            record_version(
                session,
                "entity",
                entity.id,
                "ai",
                {
                    "name": entity.name,
                    "description": entity.description,
                    "confidence": entity.confidence,
                    "priority": entity.priority,
                },
            )
            entity.name = item.name[:300]
            entity.description = item.description[:4000]
            entity.confidence = max(entity.confidence, item.confidence)
            entity.priority = max(entity.priority, priority)
        return entity
    entity = Entity(
        entity_type=item.type,
        name=item.name[:300],
        normalized_name=normalized,
        description=item.description[:4000],
        confidence=item.confidence,
        priority=priority,
    )
    session.add(entity)
    session.flush()
    record_version(session, "entity", entity.id, "ai", {"created": True, "name": entity.name})
    return entity


def upsert_claim(
    session: Session,
    *,
    topic_id: int,
    run_id: int | None,
    source_id: int | None,
    item: ExtractedClaim,
    origin: str = "ai",
    priority: int | None = None,
) -> Claim:
    normalized_text = normalize_space(item.text)
    content_hash = sha256_text(normalized_text.casefold())
    priority = priority if priority is not None else (20 if item.type == "inference" else 50)
    claim = session.scalar(select(Claim).where(Claim.topic_id == topic_id, Claim.content_hash == content_hash))
    if claim:
        claim.last_seen_at = utcnow()
        claim.occurrence_count += 1
        if priority >= claim.priority:
            record_version(
                session,
                "claim",
                claim.id,
                origin,
                {
                    "claim_text": claim.claim_text,
                    "category": claim.category,
                    "claim_type": claim.claim_type,
                    "event_time": claim.event_time,
                    "confidence": claim.confidence,
                    "importance": claim.importance,
                    "priority": claim.priority,
                    "origin": claim.origin,
                },
            )
            claim.claim_text = normalized_text
            claim.category = item.category
            claim.claim_type = item.type
            claim.event_time = item.event_time
            claim.confidence = max(claim.confidence, item.confidence)
            claim.importance = max(claim.importance, item.importance)
            claim.priority = priority
            claim.origin = origin
            claim.run_id = run_id or claim.run_id
    else:
        claim = Claim(
            topic_id=topic_id,
            run_id=run_id,
            claim_text=normalized_text,
            category=item.category,
            claim_type=item.type,
            event_time=item.event_time,
            confidence=item.confidence,
            importance=item.importance,
            priority=priority,
            origin=origin,
            content_hash=content_hash,
        )
        session.add(claim)
        session.flush()
        record_version(session, "claim", claim.id, origin, {"created": True, "claim_text": claim.claim_text})

    if source_id:
        exists = session.scalar(
            select(ClaimEvidence).where(ClaimEvidence.claim_id == claim.id, ClaimEvidence.source_id == source_id)
        )
        if not exists:
            session.add(ClaimEvidence(claim_id=claim.id, source_id=source_id))
    return claim


def link_claim_entities(session: Session, claim: Claim, entities: dict[str, Entity], names: list[str]) -> None:
    for name in names:
        entity = entities.get(normalize_entity_name(name))
        if not entity:
            continue
        exists = session.scalar(
            select(ClaimEntity).where(ClaimEntity.claim_id == claim.id, ClaimEntity.entity_id == entity.id)
        )
        if not exists:
            session.add(ClaimEntity(claim_id=claim.id, entity_id=entity.id))


def upsert_relation(
    session: Session,
    *,
    topic_id: int,
    run_id: int,
    source_id: int,
    item: ExtractedRelation,
    entities: dict[str, Entity],
    priority: int = 50,
) -> Relation | None:
    subject = entities.get(normalize_entity_name(item.subject))
    obj = entities.get(normalize_entity_name(item.object))
    if not subject or not obj:
        return None
    key = f"{subject.id}|{normalize_space(item.predicate).casefold()}|{obj.id}"
    content_hash = sha256_text(key)
    relation = session.scalar(
        select(Relation).where(Relation.topic_id == topic_id, Relation.content_hash == content_hash)
    )
    if relation:
        relation.confidence = max(relation.confidence, item.confidence)
        relation.priority = max(relation.priority, priority)
        if priority >= relation.priority:
            relation.run_id = run_id
            relation.source_id = source_id
        return relation
    relation = Relation(
        topic_id=topic_id,
        run_id=run_id,
        source_id=source_id,
        subject_entity_id=subject.id,
        predicate=normalize_space(item.predicate)[:200],
        object_entity_id=obj.id,
        confidence=item.confidence,
        priority=priority,
        content_hash=content_hash,
    )
    session.add(relation)
    return relation


def persist_chunk_analysis(
    session: Session,
    *,
    topic_id: int,
    run_id: int,
    source_id: int,
    analysis: ChunkAnalysis,
    origin: str = "ai",
    priority_override: int | None = None,
) -> dict:
    entities: dict[str, Entity] = {}
    for item in analysis.entities:
        entity = upsert_entity(session, item, priority=priority_override or 50)
        entities[normalize_entity_name(item.name)] = entity

    claims: list[Claim] = []
    for item in analysis.claims:
        claim = upsert_claim(
            session,
            topic_id=topic_id,
            run_id=run_id,
            source_id=source_id,
            item=item,
            origin=origin,
            priority=priority_override,
        )
        claims.append(claim)
        link_claim_entities(session, claim, entities, item.entity_names)

    for item in analysis.relations:
        if normalize_entity_name(item.subject) not in entities:
            entity = upsert_entity(
                session,
                ExtractedEntity(name=item.subject, type="other", confidence=item.confidence),
                priority=priority_override or 50,
            )
            entities[normalize_entity_name(item.subject)] = entity
        if normalize_entity_name(item.object) not in entities:
            entity = upsert_entity(
                session,
                ExtractedEntity(name=item.object, type="other", confidence=item.confidence),
                priority=priority_override or 50,
            )
            entities[normalize_entity_name(item.object)] = entity
        upsert_relation(
            session,
            topic_id=topic_id,
            run_id=run_id,
            source_id=source_id,
            item=item,
            entities=entities,
            priority=priority_override or 50,
        )

    return {
        "claims": [claim.claim_text for claim in claims],
        "entities": [entity.name for entity in entities.values()],
        "gaps": analysis.search_gaps,
        "importance": analysis.importance,
        "confidence": analysis.confidence,
    }


def persist_conflicts(session: Session, *, topic_id: int, run_id: int, synthesis: RunSynthesis) -> list[Conflict]:
    created: list[Conflict] = []
    for item in synthesis.conflicts:
        key = sha256_text("|".join(sorted([normalize_space(item.claim_a), normalize_space(item.claim_b)])))
        conflict = session.scalar(select(Conflict).where(Conflict.topic_id == topic_id, Conflict.content_hash == key))
        if conflict:
            if conflict.status == "open":
                conflict.reason = item.reason
                conflict.confidence = max(conflict.confidence, item.confidence)
            created.append(conflict)
            continue

        claim_a_hash = sha256_text(normalize_space(item.claim_a).casefold())
        claim_b_hash = sha256_text(normalize_space(item.claim_b).casefold())
        claim_a = session.scalar(
            select(Claim).where(Claim.topic_id == topic_id, Claim.content_hash == claim_a_hash)
        )
        claim_b = session.scalar(
            select(Claim).where(Claim.topic_id == topic_id, Claim.content_hash == claim_b_hash)
        )
        conflict = Conflict(
            topic_id=topic_id,
            run_id=run_id,
            claim_a_id=claim_a.id if claim_a else None,
            claim_b_id=claim_b.id if claim_b else None,
            claim_a_text=normalize_space(item.claim_a),
            claim_b_text=normalize_space(item.claim_b),
            reason=item.reason,
            confidence=item.confidence,
            content_hash=key,
        )
        session.add(conflict)
        session.flush()
        created.append(conflict)
        atomic_write_json(
            settings.conflict_dir / f"conflict_{conflict.id}.json",
            {
                "id": conflict.id,
                "topic_id": topic_id,
                "run_id": run_id,
                "claim_a": conflict.claim_a_text,
                "claim_b": conflict.claim_b_text,
                "reason": conflict.reason,
                "confidence": conflict.confidence,
                "status": conflict.status,
            },
        )
    return created


def export_run_snapshot(session: Session, topic_id: int, run_id: int) -> str:
    claims = list(session.scalars(select(Claim).where(Claim.topic_id == topic_id).order_by(Claim.priority.desc(), Claim.id.desc()).limit(500)))
    conflicts = list(session.scalars(select(Conflict).where(Conflict.topic_id == topic_id).order_by(Conflict.id.desc()).limit(200)))
    payload = {
        "topic_id": topic_id,
        "run_id": run_id,
        "claims": [
            {
                "id": c.id,
                "text": c.claim_text,
                "category": c.category,
                "type": c.claim_type,
                "confidence": c.confidence,
                "importance": c.importance,
                "priority": c.priority,
                "origin": c.origin,
                "status": c.status,
            }
            for c in claims
        ],
        "conflicts": [
            {
                "id": c.id,
                "claim_a": c.claim_a_text,
                "claim_b": c.claim_b_text,
                "reason": c.reason,
                "status": c.status,
            }
            for c in conflicts
        ],
    }
    path = settings.knowledge_dir / f"topic_{topic_id}_run_{run_id}.json"
    atomic_write_json(path, payload)
    return str(path)
