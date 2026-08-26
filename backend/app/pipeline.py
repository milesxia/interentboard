from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .chunker import TextChunk, chunk_text, relevance_score
from .config import settings
from .db import session_scope
from .fetcher import FetchedDocument, archive_document, fetch_document
from .knowledge import export_run_snapshot, persist_chunk_analysis, persist_conflicts
from .models import Chunk, ManualNote, ResearchRun, RunEvidence, RunStatus, Source, Topic, WebsiteWatch, utcnow
from .ollama_client import ollama
from .schemas import ChunkAnalysis
from .search import SearchResult, search_web
from .utils import atomic_write_json, estimate_tokens, normalize_space, sha256_text

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CandidateChunk:
    chunk_id: int
    source_id: int
    source_title: str
    source_url: str
    content: str
    content_hash: str
    score: float
    is_manual: bool = False


def _update_run(session: Session, run: ResearchRun, status: RunStatus, progress: int, message: str) -> None:
    run.status = status.value
    run.progress = max(0, min(100, progress))
    run.message = message[:4000]
    session.flush()


def _link_run_source(session: Session, run_id: int, source_id: int) -> None:
    exists = session.scalar(select(RunEvidence).where(RunEvidence.run_id == run_id, RunEvidence.source_id == source_id))
    if not exists:
        session.add(RunEvidence(run_id=run_id, source_id=source_id))


def _upsert_source(session: Session, topic_id: int, run_id: int, document: FetchedDocument) -> Source:
    source = session.scalar(
        select(Source).where(Source.topic_id == topic_id, Source.content_hash == document.content_hash)
    )
    if source:
        source.last_seen_at = utcnow()
        source.seen_count += 1
        source.run_id = run_id
        if not source.storage_path:
            source.storage_path = archive_document(document, topic_id)
        _link_run_source(session, run_id, source.id)
        return source

    source = Source(
        topic_id=topic_id,
        run_id=run_id,
        url=document.url,
        canonical_url=document.canonical_url,
        title=document.title,
        content=document.text,
        content_hash=document.content_hash,
        source_time=document.source_time,
        mime_type=document.mime_type,
        storage_path=archive_document(document, topic_id),
        metadata_json={**document.metadata, "raw_hash": document.raw_hash},
    )
    session.add(source)
    session.flush()
    _link_run_source(session, run_id, source.id)
    return source


def _upsert_manual_source(session: Session, topic_id: int, run_id: int, note: ManualNote) -> Source:
    content_hash = sha256_text(note.content)
    source = session.scalar(select(Source).where(Source.topic_id == topic_id, Source.content_hash == content_hash))
    if source:
        source.last_seen_at = utcnow()
        source.seen_count += 1
        source.run_id = run_id
        _link_run_source(session, run_id, source.id)
        return source
    path = settings.source_dir / f"manual_topic{topic_id}_note{note.id}_{content_hash[:12]}.txt"
    if not path.exists():
        path.write_text(note.content, encoding="utf-8")
    source = Source(
        topic_id=topic_id,
        run_id=run_id,
        url=f"manual://note/{note.id}",
        canonical_url=f"manual://note/{note.id}",
        title=note.title or f"Manual note #{note.id}",
        content=note.content,
        content_hash=content_hash,
        source_time=note.updated_at,
        mime_type="text/plain",
        storage_path=str(path),
        metadata_json={"manual_note_id": note.id, "priority": note.priority},
    )
    session.add(source)
    session.flush()
    _link_run_source(session, run_id, source.id)
    return source


def _store_chunks(session: Session, source: Source, query: str) -> list[CandidateChunk]:
    created: list[CandidateChunk] = []
    items = chunk_text(source.content)
    is_manual = source.url.startswith("manual://")
    for item in items:
        score = relevance_score(item.content, query, item.index)
        chunk = session.scalar(
            select(Chunk).where(Chunk.source_id == source.id, Chunk.chunk_index == item.index)
        )
        if not chunk:
            chunk = Chunk(
                source_id=source.id,
                chunk_index=item.index,
                content=item.content,
                content_hash=item.content_hash,
                char_count=item.char_count,
                token_estimate=item.token_estimate,
                relevance_score=score,
            )
            session.add(chunk)
            session.flush()
        else:
            chunk.relevance_score = score
        created.append(
            CandidateChunk(
                chunk_id=chunk.id,
                source_id=source.id,
                source_title=source.title,
                source_url=source.canonical_url or source.url,
                content=chunk.content,
                content_hash=chunk.content_hash,
                score=score + (100.0 if is_manual else 0.0),
                is_manual=is_manual,
            )
        )
    return created


def _select_ai_chunks(
    candidates: list[CandidateChunk],
    *,
    exclude_hashes: set[str],
    limit: int,
) -> list[CandidateChunk]:
    if limit <= 0:
        return []
    per_source: dict[int, int] = {}
    selected: list[CandidateChunk] = []
    selected_hashes: set[str] = set()
    manual_used = 0
    max_manual = max(2, limit // 3)
    for item in sorted(candidates, key=lambda x: x.score, reverse=True):
        if item.content_hash in exclude_hashes or item.content_hash in selected_hashes:
            continue
        if item.is_manual and manual_used >= max_manual:
            continue
        used = per_source.get(item.source_id, 0)
        if used >= settings.max_ai_chunks_per_source:
            continue
        selected.append(item)
        selected_hashes.add(item.content_hash)
        per_source[item.source_id] = used + 1
        if item.is_manual:
            manual_used += 1
        if len(selected) >= limit:
            break
    return selected


def _analysis_cache_path(topic: Topic, content_hash: str) -> Path:
    model_key = settings.ollama_model.replace("/", "_").replace(":", "_")
    prompt_key = sha256_text(f"v1|{topic.id}|{topic.name}|{topic.query}|{settings.ollama_model}")[:16]
    return settings.chunk_dir / f"{content_hash}.{prompt_key}.{model_key}.json"


def _load_or_analyze(topic: Topic, candidate: CandidateChunk) -> ChunkAnalysis:
    path = _analysis_cache_path(topic, candidate.content_hash)
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("model") == settings.ollama_model:
                return ChunkAnalysis.model_validate(payload["analysis"])
        except Exception:
            logger.warning("Ignoring invalid chunk analysis cache: %s", path)

    analysis = ollama.analyze_chunk(
        topic_name=topic.name,
        query=topic.query,
        source_title=candidate.source_title,
        source_url=candidate.source_url,
        chunk=candidate.content,
    )
    atomic_write_json(path, {"model": settings.ollama_model, "analysis": analysis.model_dump()})
    return analysis


def _build_digest(items: list[dict], topic_id: int, session: Session, token_budget: int = 5200) -> str:
    lines: list[str] = []
    used = 0
    for idx, item in enumerate(items, start=1):
        block = (
            f"[证据块 {idx}] 来源: {item['source_title']} | {item['source_url']}\n"
            f"Claims: {json.dumps(item['claims'], ensure_ascii=False)}\n"
            f"Entities: {json.dumps(item['entities'], ensure_ascii=False)}\n"
            f"Relations: {json.dumps(item.get('relations', []), ensure_ascii=False)}\n"
            f"Gaps: {json.dumps(item['gaps'], ensure_ascii=False)}\n"
        )
        cost = estimate_tokens(block)
        if used + cost > token_budget:
            break
        lines.append(block)
        used += cost

    manual_notes = list(
        session.scalars(select(ManualNote).where(ManualNote.topic_id == topic_id).order_by(ManualNote.updated_at.desc()).limit(20))
    )
    for note in manual_notes:
        block = f"[人工输入 priority=100] {note.title}: {note.content[:3000]}\n"
        cost = estimate_tokens(block)
        if used + cost > token_budget:
            break
        lines.append(block)
        used += cost
    return "\n".join(lines)


def _collect_watch_results(session: Session, topic_id: int) -> list[SearchResult]:
    watches = list(
        session.scalars(
            select(WebsiteWatch).where(WebsiteWatch.topic_id == topic_id, WebsiteWatch.enabled.is_(True))
        )
    )
    return [SearchResult(url=w.url, title="Website watch", provider="watch") for w in watches]


def _load_persisted_run_candidates(session: Session, run_id: int, query: str) -> tuple[list[CandidateChunk], set[str], int]:
    sources = list(
        session.scalars(
            select(Source)
            .join(RunEvidence, RunEvidence.source_id == Source.id)
            .where(RunEvidence.run_id == run_id)
            .order_by(Source.id.asc())
        )
    )
    candidates: list[CandidateChunk] = []
    seen_urls: set[str] = set()
    web_count = 0
    for source in sources:
        candidates.extend(_store_chunks(session, source, query))
        if not source.url.startswith("manual://"):
            web_count += 1
            if source.url:
                seen_urls.add(source.url)
            if source.canonical_url:
                seen_urls.add(source.canonical_url)
    return candidates, seen_urls, web_count


def execute_research_run(run_id: int) -> None:
    with session_scope() as session:
        run = session.get(ResearchRun, run_id)
        if not run:
            raise RuntimeError(f"ResearchRun {run_id} does not exist")
        topic = session.get(Topic, run.topic_id)
        if not topic:
            raise RuntimeError(f"Topic {run.topic_id} does not exist")
        run.started_at = run.started_at or utcnow()
        run.error = ""
        _update_run(session, run, RunStatus.SEARCHING, 5, "Starting research")

    digest_items: list[dict] = []
    with session_scope() as session:
        run = session.get(ResearchRun, run_id)
        topic = session.get(Topic, run.topic_id)
        all_candidates, seen_urls, source_count = _load_persisted_run_candidates(session, run_id, topic.query)
        if all_candidates:
            run.message = f"Resuming with {len(all_candidates)} persisted evidence chunks"

    pending_queries = [topic.query]

    for round_index in range(settings.max_search_rounds):
        with session_scope() as session:
            run = session.get(ResearchRun, run_id)
            topic = session.get(Topic, run.topic_id)
            run.search_round = round_index + 1
            _update_run(
                session,
                run,
                RunStatus.SEARCHING,
                8 + round_index * 10,
                f"Search round {round_index + 1}/{settings.max_search_rounds}",
            )
            search_results: list[SearchResult] = _collect_watch_results(session, topic.id) if round_index == 0 else []

        for query in pending_queries[:3]:
            search_results.extend(search_web(query, settings.max_results_per_query))

        unique_results: list[SearchResult] = []
        fetch_candidate_limit = max(settings.max_sources_per_run * 3, settings.max_results_per_query)
        for result in search_results:
            if result.url in seen_urls:
                continue
            seen_urls.add(result.url)
            unique_results.append(result)
            if len(unique_results) >= fetch_candidate_limit:
                break

        with session_scope() as session:
            run = session.get(ResearchRun, run_id)
            topic = session.get(Topic, run.topic_id)
            _update_run(session, run, RunStatus.FETCHING, 20 + round_index * 10, f"Fetching {len(unique_results)} sources")
            manual_notes = (
                list(session.scalars(select(ManualNote).where(ManualNote.topic_id == topic.id).order_by(ManualNote.updated_at.desc()).limit(10)))
                if round_index == 0
                else []
            )
            manual_sources = [_upsert_manual_source(session, topic.id, run.id, note) for note in manual_notes]
            for source in manual_sources:
                all_candidates.extend(_store_chunks(session, source, topic.query))

        for result in unique_results:
            if source_count >= settings.max_sources_per_run:
                break
            try:
                document = fetch_document(result.url)
                with session_scope() as session:
                    run = session.get(ResearchRun, run_id)
                    topic = session.get(Topic, run.topic_id)
                    source = _upsert_source(session, topic.id, run.id, document)
                    all_candidates.extend(_store_chunks(session, source, topic.query))
                    source_count += 1
            except Exception as exc:
                logger.warning("Fetch failed for %s: %s", result.url, exc)

        analyzed_hashes = {item.get("content_hash") for item in digest_items}
        remaining_budget = settings.max_total_ai_chunks - len(digest_items)
        if round_index == 0 and settings.max_search_rounds > 1:
            round_budget = min(remaining_budget, max(1, (settings.max_total_ai_chunks * 2) // 3))
        else:
            round_budget = remaining_budget
        with session_scope() as session:
            run = session.get(ResearchRun, run_id)
            _update_run(session, run, RunStatus.CHUNKING, 35 + round_index * 10, "Selecting relevant chunks within the 8K context budget")

        selected = _select_ai_chunks(all_candidates, exclude_hashes=analyzed_hashes, limit=round_budget)
        if not selected and not digest_items:
            raise RuntimeError("No usable evidence chunks were collected")

        with session_scope() as session:
            run = session.get(ResearchRun, run_id)
            topic = session.get(Topic, run.topic_id)
            _update_run(
                session,
                run,
                RunStatus.AI_ANALYSIS,
                45 + round_index * 15,
                f"Analyzing {len(selected)} selected evidence chunks with {settings.ollama_model}",
            )

        new_gaps: list[str] = []
        for candidate in selected:
            if candidate.content_hash in analyzed_hashes:
                continue
            analysis = _load_or_analyze(topic, candidate)
            with session_scope() as session:
                persist = persist_chunk_analysis(
                    session,
                    topic_id=topic.id,
                    run_id=run_id,
                    source_id=candidate.source_id,
                    analysis=analysis,
                    origin="manual" if candidate.is_manual else "ai",
                    priority_override=100 if candidate.is_manual else None,
                )
            analyzed_hashes.add(candidate.content_hash)
            digest_items.append(
                {
                    "content_hash": candidate.content_hash,
                    "source_title": candidate.source_title,
                    "source_url": candidate.source_url,
                    "claims": [item.model_dump() for item in analysis.claims],
                    "entities": [item.model_dump() for item in analysis.entities],
                    "relations": [item.model_dump() for item in analysis.relations],
                    "gaps": analysis.search_gaps,
                    "importance": analysis.importance,
                    "confidence": analysis.confidence,
                    "persisted": persist,
                }
            )
            new_gaps.extend(analysis.search_gaps)

        if round_index + 1 >= settings.max_search_rounds or source_count >= settings.max_sources_per_run:
            break
        pending_queries = []
        for gap in new_gaps:
            gap = normalize_space(gap)
            if gap and gap.casefold() != topic.query.casefold() and gap not in pending_queries:
                pending_queries.append(gap)
            if len(pending_queries) >= 2:
                break
        if not pending_queries:
            break

    with session_scope() as session:
        run = session.get(ResearchRun, run_id)
        topic = session.get(Topic, run.topic_id)
        _update_run(session, run, RunStatus.AI_ANALYSIS, 82, "Synthesizing final research result")
        digest = _build_digest(digest_items, topic.id, session)

    synthesis = ollama.synthesize_run(
        topic_name=topic.name,
        query=topic.query,
        evidence_digest=digest,
        allow_followup=False,
    )

    with session_scope() as session:
        run = session.get(ResearchRun, run_id)
        topic = session.get(Topic, run.topic_id)
        _update_run(session, run, RunStatus.KNOWLEDGE_UPDATE, 92, "Updating knowledge base and conflicts")
        run.summary = synthesis.summary
        run.trend = synthesis.trend
        run.prediction = synthesis.prediction
        run.confidence = synthesis.confidence
        persist_conflicts(session, topic_id=topic.id, run_id=run.id, synthesis=synthesis)
        snapshot_path = export_run_snapshot(session, topic.id, run.id)
        run.status = RunStatus.COMPLETED.value
        run.progress = 100
        run.message = f"Completed; knowledge snapshot: {snapshot_path}"
        run.finished_at = utcnow()


def mark_run_failed(run_id: int, error: str) -> None:
    with session_scope() as session:
        run = session.get(ResearchRun, run_id)
        if not run:
            return
        run.status = RunStatus.FAILED.value
        run.error = error[:12000]
        run.message = "Failed; state and persisted evidence are retained for retry"
        run.finished_at = utcnow()
