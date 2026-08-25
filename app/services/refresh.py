from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

from app.db import Database, now_iso
from app.services.rules import extract_date_candidates, material_score, source_grade, transition_is_safe


class RefreshEngine:
    def __init__(self, db: Database, topics_config: list[dict], searcher, fetcher, analyzer, baseline_store, settings):
        self.db = db
        self.topics_config = {t["slug"]: t for t in topics_config}
        self.searcher = searcher
        self.fetcher = fetcher
        self.analyzer = analyzer
        self.baseline_store = baseline_store
        self.settings = settings
        self._system_lock = asyncio.Lock()

    def busy(self) -> bool:
        return self._system_lock.locked()

    async def refresh_all(self, mode: str = "manual-all") -> list[int]:
        if self._system_lock.locked():
            return []
        async with self._system_lock:
            run_ids = []
            for slug in self.topics_config:
                run_ids.append(await self._refresh_topic_unlocked(slug, mode=mode))
            return run_ids

    async def refresh_topic(self, slug: str, mode: str = "manual") -> int:
        if slug not in self.topics_config:
            raise KeyError(slug)
        run_id = self.db.create_run(slug, mode)
        if self._system_lock.locked():
            self.db.update_run(run_id, status="skipped", progress=100, finished_at=now_iso(), message="系统已有检索任务运行，为保护NAS已跳过")
            return run_id
        async with self._system_lock:
            return await self._refresh_topic_unlocked(slug, mode=mode, run_id=run_id)

    def _archive(self, slug: str, page) -> str:
        if not self.settings.archive_fulltext:
            return ""
        day = datetime.now().strftime("%Y%m%d")
        root = self.settings.archive_dir / slug / day
        root.mkdir(parents=True, exist_ok=True)
        stem = page.content_hash[:16]
        is_pdf = "pdf" in (page.content_type or "") or page.url.lower().split("?")[0].endswith(".pdf")
        raw_path = root / f"{stem}.{'pdf' if is_pdf else 'html'}"
        txt_path = root / f"{stem}.txt"
        raw_path.write_bytes(page.raw)
        txt_path.write_text(page.text, encoding="utf-8", errors="ignore")
        return str(txt_path.relative_to(self.settings.data_dir))

    def _apply_stage_changes(self, current: dict, analysis: dict, new_evidence: list[dict]) -> dict:
        state = dict(current)
        for change in analysis.get("stage_changes", []):
            obj = str(change.get("object", ""))
            new_stage = str(change.get("suggested_stage", ""))
            old_stage = str(state.get(obj, change.get("old_stage", "")))
            ids = [int(x) for x in change.get("evidence_ids", []) if str(x).isdigit()]
            used = [e for e in new_evidence if e.get("id") in ids] or new_evidence
            try:
                confidence = float(change.get("confidence", 0))
            except Exception:
                confidence = 0
            if obj and new_stage and old_stage and confidence >= 0.85 and transition_is_safe(old_stage, new_stage, used):
                state[obj] = new_stage
        return state

    async def _refresh_topic_unlocked(self, slug: str, mode: str, run_id: int | None = None) -> int:
        cfg = self.topics_config[slug]
        run_id = run_id or self.db.create_run(slug, mode)
        try:
            today = date.today().isoformat()
            due_watch = self.db.due_watch_nodes(slug, today)
            self.db.update_run(run_id, progress=5, message="生成增量检索任务")
            urls: dict[str, dict] = {}
            for u in cfg.get("seed_urls", []):
                urls[u] = {"title": "固定监测入口", "url": u, "snippet": ""}

            queries = list(cfg.get("queries", [])) + self.db.enabled_custom_queries(slug)
            for node in due_watch:
                queries.extend(node.get("queries", []))
            seen_q = set()
            queries = [q for q in queries if q and not (q in seen_q or seen_q.add(q))]

            for idx, q in enumerate(queries):
                outcome = await self.searcher.search(q)
                self.db.add_search_attempt(run_id, slug, q, outcome.provider, len(outcome.hits), outcome.error)
                for h in outcome.hits:
                    urls.setdefault(h.url, {"title": h.title, "url": h.url, "snippet": h.snippet})
                pct = 5 + int(25 * (idx + 1) / max(1, len(queries)))
                msg = f"检索：{q[:32]}" + ("（无结果）" if not outcome.hits else "")
                self.db.update_run(run_id, progress=pct, message=msg)
                if len(urls) >= self.settings.max_candidates_per_topic:
                    break

            items = list(urls.values())[: self.settings.max_candidates_per_topic]
            self.db.update_run(run_id, found_count=len(items), progress=35, message=f"抓取 {len(items)} 个候选页面")
            sem = asyncio.Semaphore(self.settings.max_fetch_concurrency)

            async def one(meta):
                async with sem:
                    try:
                        page = await self.fetcher.fetch(meta["url"])
                        return meta, page, None
                    except Exception as e:
                        return meta, None, str(e)

            results = await asyncio.gather(*(one(x) for x in items)) if items else []
            new_evidence = []
            official_domains = cfg.get("official_domains", [])
            for meta, page, err in results:
                if err or page is None or self.db.content_hash_exists(slug, page.content_hash):
                    continue
                excerpt = page.text[:7000]
                grade = source_grade(page.url, official_domains)
                dates = extract_date_candidates(f"{page.title}\n{excerpt}")
                archive_path = self._archive(slug, page)
                ev = {
                    "topic_slug": slug,
                    "url": page.url,
                    "title": page.title or meta.get("title", ""),
                    "source_domain": urlparse(page.url).hostname or "",
                    "source_grade": grade,
                    "content_hash": page.content_hash,
                    "excerpt": excerpt,
                    "analysis": {"rule_material_score": material_score(excerpt), "date_candidates": dates},
                    "date_candidates": dates,
                    "is_material": material_score(excerpt) > 0,
                    "archive_path": archive_path,
                }
                eid = self.db.add_evidence(ev)
                if eid:
                    ev["id"] = eid
                    new_evidence.append(ev)

            self.db.update_run(run_id, progress=70, new_count=len(new_evidence), message="本地模型综合历史与新增证据")
            db_topic = self.db.get_topic(slug) or {}
            try:
                stage_obj = json.loads(db_topic.get("stage_state") or "{}")
            except Exception:
                stage_obj = cfg.get("stage_state", {})
            topic_for_ai = {**cfg, **db_topic, "stage_state_obj": stage_obj}
            baseline = self.baseline_store.relevant(cfg.get("context_keywords", []), limit=8)
            recent = self.db.recent_summaries(slug, 3)
            analysis = await self.analyzer.analyze(topic_for_ai, baseline, recent, new_evidence, due_watch)
            summary = analysis.get("conclusion") or ("本周期无新增实质进展。" if not new_evidence else f"新增候选证据 {len(new_evidence)} 条，等待复核。")
            risk = analysis.get("risk_direction", "unchanged")
            if cfg.get("risk_level") == "not_applicable":
                risk = "not_applicable"
            current_state = analysis.get("current_state") or db_topic.get("current_state") or summary
            new_stage_state = self._apply_stage_changes(stage_obj, analysis, new_evidence)

            for update in analysis.get("watch_updates", []):
                node_id = str(update.get("id", ""))
                status = str(update.get("status", "unconfirmed"))
                if node_id and status in {"completed", "adjusted", "unconfirmed"}:
                    self.db.update_watch_node(node_id, status, str(update.get("reason", "")))

            full_scan = mode in {"manual", "scheduled", "manual-all"}
            self.db.finish_topic(slug, summary, risk, full_scan=full_scan, state=analysis, stage_state=new_stage_state, current_state=current_state)
            self.db.update_run(run_id, status="done", progress=100, finished_at=now_iso(), message="完成", new_count=len(new_evidence))
            return run_id
        except Exception as e:
            self.db.update_run(run_id, status="failed", progress=100, finished_at=now_iso(), message="失败", error=str(e))
            return run_id
