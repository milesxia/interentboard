from __future__ import annotations

import hashlib
import json
import math
from array import array
from pathlib import Path
from urllib.parse import urlparse

from app.db import Database
from app.services.chunker import split_text
from app.services.sourceintel import canonicalize_url, source_group


class KnowledgePipeline:
    """Transforms raw evidence into resumable chunks and long-term claims."""

    def __init__(self, db: Database, analyzer, settings):
        self.db = db
        self.analyzer = analyzer
        self.settings = settings

    def read_archived_text(self, evidence: dict) -> str:
        rel = (evidence.get("archive_path") or "").strip()
        if rel:
            path = self.settings.data_dir / rel
            if path.exists():
                return path.read_text(encoding="utf-8", errors="ignore")
        return evidence.get("excerpt", "") or ""

    async def process_evidence(self, evidence_id: int, text: str | None = None, progress_cb=None) -> list[dict]:
        evidence = self.db.get_evidence(evidence_id)
        if not evidence:
            raise KeyError(evidence_id)
        text = (text if text is not None else self.read_archived_text(evidence)).strip()
        if not text:
            raise ValueError(f"evidence {evidence_id} has no text")

        chunks = split_text(
            text,
            max_tokens=self.settings.ai_chunk_tokens,
            overlap_tokens=self.settings.ai_chunk_overlap_tokens,
        )
        if not chunks:
            self.db.set_evidence_processing(evidence_id, "done")
            return []
        ledger = self.db.ensure_chunks(evidence_id, chunks)
        self.db.set_evidence_processing(evidence_id, "processing")

        for pos, chunk in enumerate(ledger, start=1):
            if chunk.get("status") == "done":
                if progress_cb:
                    await progress_cb(pos, len(ledger), "resume-skip")
                continue
            last_error = ""
            success = False
            for attempt in range(2):
                try:
                    self.db.update_chunk(chunk["id"], "running", increment_retry=attempt > 0)
                    result = await self.analyzer.extract_chunk(evidence, chunk)
                    self.db.update_chunk(chunk["id"], "done", extraction=result)
                    self.db.add_claims(evidence["topic_slug"], evidence, chunk["id"], result.get("claims") or [])
                    success = True
                    break
                except Exception as exc:
                    last_error = str(exc)
                    self.db.update_chunk(chunk["id"], "failed", error=last_error, increment_retry=True)
            if progress_cb:
                await progress_cb(pos, len(ledger), "done" if success else "failed")
            if not success:
                self.db.set_evidence_processing(evidence_id, "partial")
                raise RuntimeError(f"证据 {evidence_id} 分块 {pos}/{len(ledger)} 处理失败，可断点续跑: {last_error}")

        done, total = self.db.chunk_completion(evidence_id)
        if total and done == total:
            self.db.set_evidence_processing(evidence_id, "done")
        else:
            self.db.set_evidence_processing(evidence_id, "partial")
            raise RuntimeError(f"证据 {evidence_id} 完整性校验失败: {done}/{total}")
        return self.db.claims_for_evidence(evidence_id, include_duplicates=True)

    async def index_pending_embeddings(self, topic_slug: str | None = None, limit: int = 192) -> int:
        if not getattr(self.settings, "enable_embeddings", False):
            return 0
        model = self.settings.embedding_model
        rows = self.db.pending_embedding_claims(model, topic_slug, limit=limit)
        if not rows:
            return 0
        batch_size = max(1, int(getattr(self.settings, "embedding_batch_size", 24)))
        done = 0
        for start in range(0, len(rows), batch_size):
            batch = rows[start : start + batch_size]
            texts = [r.get("statement", "") for r in batch]
            vectors = await self.analyzer.ollama.embed_texts(texts, model=model)
            for row, vec in zip(batch, vectors):
                packed = array("f", [float(x) for x in vec]).tobytes()
                self.db.save_claim_embedding(int(row["id"]), model, packed, len(vec))
                done += 1
        return done

    async def semantic_relevant_claims(self, topic_slug: str, query_text: str, limit: int = 60) -> list[dict]:
        if not getattr(self.settings, "enable_embeddings", False):
            return []
        if self.db.claim_count(topic_slug) < int(getattr(self.settings, "semantic_rag_min_claims", 200)):
            return []
        await self.index_pending_embeddings(topic_slug, limit=max(80, limit * 3))
        qvecs = await self.analyzer.ollama.embed_texts([query_text], model=self.settings.embedding_model)
        if not qvecs:
            return []
        q = qvecs[0]
        rows = self.db.embedding_rows(topic_slug, self.settings.embedding_model, limit=1400)
        scored = []
        for row in rows:
            try:
                vec = array("f")
                vec.frombytes(row["vector"])
                if len(vec) != len(q):
                    continue
                # Ollama returns L2-normalized embeddings, so dot product is cosine similarity.
                score = sum(float(a) * float(b) for a, b in zip(vec, q))
                if row.get("human_override"):
                    score += 0.08
                if row.get("source_grade") == "A":
                    score += 0.04
                row["retrieval_score"] = round(score, 6)
                try:
                    row["entities"] = json.loads(row.get("entities_json") or "[]")
                except Exception:
                    row["entities"] = []
                scored.append((score, row))
            except Exception:
                continue
        scored.sort(key=lambda x: x[0], reverse=True)
        return [row for score, row in scored[:limit] if score > 0.18]

    async def relevant_claims(self, topic_slug: str, terms: list[str], limit: int = 60) -> list[dict]:
        # RAGFlow-style hybrid recall: lexical/metadata + FTS + optional semantic vectors.
        lexical = self.db.relevant_claims(topic_slug, terms, limit=max(limit, 80))
        fts = self.db.fts_claims(topic_slug, terms, limit=max(limit, 80))
        semantic = await self.semantic_relevant_claims(topic_slug, "；".join(terms[:24]), limit=max(limit, 80))
        scores: dict[int, float] = {}
        rows: dict[int, dict] = {}
        for rank, group in enumerate((lexical, fts, semantic)):
            weight = (1.0, 1.15, 1.3)[rank]
            for pos, row in enumerate(group):
                cid = int(row["id"])
                rows[cid] = row
                scores[cid] = scores.get(cid, 0.0) + weight / (1.0 + pos / 12.0)
                if row.get("human_override"):
                    scores[cid] += 1.0
        ordered = sorted(rows.values(), key=lambda r: scores.get(int(r["id"]), 0.0), reverse=True)
        return ordered[:limit]

    def create_manual_evidence(self, manual: dict) -> int:
        source_id = int(manual["id"])
        raw = manual["raw_content"].strip()
        root: Path = self.settings.manual_archive_dir
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"manual-{source_id}.txt"
        path.write_text(raw, encoding="utf-8")
        rel = str(path.relative_to(self.settings.data_dir))

        source_type = manual.get("source_type", "intel")
        source_kind = "manual-news" if source_type == "news" else "manual-intel"
        source_url = (manual.get("source_url") or "").strip()
        url = source_url or f"manual://{source_id}"
        domain = urlparse(source_url).hostname or "" if source_url else ""
        grade = "B" if source_type == "news" else "U"
        # User-entered intelligence is intentionally distinguishable from official evidence.
        item = {
            "topic_slug": manual["topic_slug"],
            "url": url,
            "title": manual.get("title") or ("人工新闻资料" if source_type == "news" else "人工情报"),
            "source_domain": domain,
            "source_grade": grade,
            "source_kind": source_kind,
            "publish_date": manual.get("info_date"),
            "event_date": manual.get("info_date"),
            "content_hash": hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest(),
            "excerpt": raw[:7000],
            "analysis": {
                "manual_source_id": source_id,
                "confidence_label": manual.get("confidence_label", "medium"),
            },
            "is_material": True,
            "review_status": "approved",
            "manual_note": "用户主动录入，已自动进入AI提炼流程",
            "archive_path": rel,
            "processing_status": "pending",
            "canonical_url": canonicalize_url(url),
            "change_kind": "manual",
            "change_ratio": 1.0,
            "source_group_id": source_group(canonicalize_url(url)),
        }
        evidence_id = self.db.add_evidence(item)
        if not evidence_id:
            raise RuntimeError("无法创建人工证据")
        self.db.update_manual_source(source_id, status="processing", evidence_id=evidence_id)
        return evidence_id

    async def process_manual_source(self, source_id: int, progress_cb=None) -> tuple[dict, list[dict]]:
        manual = self.db.get_manual_source(source_id)
        if not manual:
            raise KeyError(source_id)
        try:
            evidence_id = manual.get("evidence_id") or self.create_manual_evidence(manual)
            claims = await self.process_evidence(int(evidence_id), manual["raw_content"], progress_cb=progress_cb)
            self.db.update_manual_source(source_id, status="done", evidence_id=int(evidence_id))
            return self.db.get_evidence(int(evidence_id)) or {}, claims
        except Exception as exc:
            self.db.update_manual_source(source_id, status="failed", error=str(exc))
            raise
