from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from urllib.parse import urlparse

from app.db import Database, now_iso
from app.services.rules import extract_date_candidates, material_score, source_grade, transition_is_safe
from app.services.sourceintel import canonicalize_url, changed_excerpt, change_ratio, classify_source


class RefreshEngine:
    def __init__(self, db: Database, topics_config: list[dict], searcher, fetcher, analyzer, baseline_store, knowledge_pipeline, settings):
        self.db = db
        self.topics_config = {t["slug"]: t for t in topics_config}
        self.searcher = searcher
        self.fetcher = fetcher
        self.analyzer = analyzer
        self.baseline_store = baseline_store
        self.knowledge_pipeline = knowledge_pipeline
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
            self.db.update_run(run_id, status="skipped", progress=100, finished_at=now_iso(), message="系统已有任务运行")
            return run_id
        async with self._system_lock:
            return await self._refresh_topic_unlocked(slug, mode=mode, run_id=run_id)

    async def ingest_manual_source(self, source_id: int) -> int:
        manual = self.db.get_manual_source(source_id)
        if not manual:
            raise KeyError(source_id)
        slug = manual["topic_slug"]
        run_id = self.db.create_run(slug, "manual-knowledge")
        async with self._system_lock:
            try:
                self.db.set_run_step(run_id, "manual-source", "running", detail="保存原始内容并自动提炼")
                self.db.update_run(run_id, progress=5, message="保存人工原文")

                async def cb(done: int, total: int, state: str):
                    pct = 10 + int(55 * done / max(1, total))
                    self.db.update_run(run_id, progress=pct, message=f"人工资料AI提炼 {done}/{total} · {state}")
                    self.db.set_run_step(run_id, "extract", "running", current=done, total=total, detail=state)

                evidence, claims = await self.knowledge_pipeline.process_manual_source(source_id, progress_cb=cb)
                self.db.set_run_step(run_id, "extract", "done", current=1, total=1, detail=f"自动入库 {len(claims)} 条Claim")
                self.db.update_run(run_id, progress=72, new_count=1, message=f"已自动入库 {len(claims)} 条知识，更新专题判断")
                self.db.set_run_step(run_id, "analysis", "running", detail="长期知识检索 + Qwen3.8综合判断")
                await self._analyze_and_finish_topic(slug, claims, [evidence], full_scan=False)
                self.db.set_run_step(run_id, "analysis", "done", detail="专题状态已更新")
                self.db.set_run_step(run_id, "manual-source", "done", detail="完成")
                self.db.update_run(run_id, status="done", progress=100, finished_at=now_iso(), message=f"人工知识已入库，共 {len(claims)} 条")
                return run_id
            except Exception as exc:
                self.db.set_run_step(run_id, "manual-source", "failed", detail=str(exc))
                self.db.update_run(run_id, status="failed", progress=100, finished_at=now_iso(), message="人工知识处理失败，可自动重试", error=str(exc))
                raise

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
        meta_path = root / f"{stem}.json"
        if not raw_path.exists():
            raw_path.write_bytes(page.raw)
        if not txt_path.exists():
            txt_path.write_text(page.text, encoding="utf-8", errors="ignore")
        if not meta_path.exists():
            meta_path.write_text(
                json.dumps(
                    {
                        "url": page.url,
                        "title": page.title,
                        "content_type": page.content_type,
                        "status_code": getattr(page, "status_code", 200),
                        "headers": getattr(page, "headers", {}),
                        "archived_at": now_iso(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
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

    def _history_terms(self, cfg: dict, new_claims: list[dict]) -> list[str]:
        terms = list(cfg.get("context_keywords", [])) + [cfg.get("name", "")]
        for c in new_claims[:50]:
            terms.extend(c.get("entities") or [])
            statement = c.get("statement", "")
            if 2 <= len(statement) <= 40:
                terms.append(statement)
        seen = set()
        return [x for x in terms if x and not (x in seen or seen.add(x))]

    async def _analyze_and_finish_topic(self, slug: str, new_claims: list[dict], new_evidence: list[dict], full_scan: bool) -> dict:
        cfg = self.topics_config[slug]
        db_topic = self.db.get_topic(slug) or {}
        try:
            stage_obj = json.loads(db_topic.get("stage_state") or "{}")
        except Exception:
            stage_obj = cfg.get("stage_state", {})
        topic_for_ai = {**cfg, **db_topic, "stage_state_obj": stage_obj}
        baseline = self.baseline_store.relevant(cfg.get("context_keywords", []), limit=8)
        recent = self.db.recent_summaries(slug, 3)
        due_watch = self.db.due_watch_nodes(slug, date.today().isoformat())
        history = await self.knowledge_pipeline.relevant_claims(
            slug,
            self._history_terms(cfg, new_claims),
            limit=self.settings.max_history_claims,
        )
        new_eids = {int(e["id"]) for e in new_evidence if e and e.get("id")}
        history = [x for x in history if int(x.get("evidence_id") or -1) not in new_eids]

        analysis = await self.analyzer.analyze(topic_for_ai, baseline, recent, new_claims, history, due_watch)
        # Persist explicit lifecycle relations. Human overrides are protected inside the DB method.
        updates = list(analysis.get("knowledge_updates") or [])
        for conflict in analysis.get("evidence_conflicts") or []:
            ids = [int(x) for x in conflict.get("claim_ids", []) if str(x).isdigit()]
            if len(ids) >= 2:
                updates.append({
                    "old_claim_id": ids[0], "new_claim_id": ids[1], "relation": "conflicts",
                    "confidence": 0.8, "reason": conflict.get("topic") or "AI融合发现冲突",
                })
        self.db.apply_knowledge_updates(slug, updates)

        summary = analysis.get("conclusion") or ("本周期无新增实质进展。" if not new_claims else f"新增知识 {len(new_claims)} 条。")
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

        self.db.finish_topic(slug, summary, risk, full_scan=full_scan, state=analysis, stage_state=new_stage_state, current_state=current_state)
        return analysis

    def _record_search(self, run_id: int, slug: str, query: str, outcome) -> None:
        attempts = getattr(outcome, "attempts", None) or []
        if not attempts:
            self.db.add_search_attempt(run_id, slug, query, outcome.provider, len(outcome.hits), outcome.error, getattr(outcome, "duration_ms", 0), bool(outcome.hits) and not outcome.error)
            return
        for item in attempts:
            self.db.add_search_attempt(
                run_id, slug, query, item.get("provider", ""), int(item.get("hit_count") or 0), item.get("error", ""),
                int(item.get("duration_ms") or 0), bool(item.get("success")),
            )

    async def _search_candidates(self, run_id: int, slug: str, queries: list[str], seen_urls: set[str], *, progress_start: int, progress_span: int, limit: int) -> list[dict]:
        urls: dict[str, dict] = {}
        self.db.set_run_step(run_id, "search", "running", current=0, total=len(queries), detail="多引擎检索")
        for idx, q in enumerate(queries):
            outcome = await self.searcher.search(q)
            self._record_search(run_id, slug, q, outcome)
            for h in outcome.hits:
                canonical = canonicalize_url(h.url)
                if canonical in seen_urls:
                    continue
                urls.setdefault(canonical, {"title": h.title, "url": h.url, "snippet": h.snippet, "canonical_url": canonical})
            pct = progress_start + int(progress_span * (idx + 1) / max(1, len(queries)))
            msg = f"检索：{q[:32]}" + ("（无结果）" if not outcome.hits else "")
            self.db.update_run(run_id, progress=pct, message=msg)
            self.db.set_run_step(run_id, "search", "running", current=idx + 1, total=len(queries), detail=msg)
            if len(urls) >= limit:
                break
        self.db.set_run_step(run_id, "search", "done", current=min(len(queries), idx + 1 if queries else 0), total=len(queries), detail=f"候选 {len(urls)}")
        return list(urls.values())[:limit]

    async def _fetch_new_evidence(self, run_id: int, slug: str, cfg: dict, items: list[dict], *, progress: int = 35) -> tuple[list[dict], dict[int, str], set[str]]:
        self.db.set_run_step(run_id, "fetch", "running", current=0, total=len(items), detail="抓取并做页面版本/转载判定")
        sem = asyncio.Semaphore(self.settings.max_fetch_concurrency)

        async def one(meta):
            async with sem:
                try:
                    page = await self.fetcher.fetch(meta["url"])
                    return meta, page, None
                except Exception as e:
                    return meta, None, str(e)

        results = await asyncio.gather(*(one(x) for x in items)) if items else []
        new_evidence: list[dict] = []
        process_text_by_id: dict[int, str] = {}
        seen_urls: set[str] = set()
        official_domains = cfg.get("official_domains", [])
        for idx, (meta, page, err) in enumerate(results, start=1):
            if err or page is None:
                self.db.set_run_step(run_id, "fetch", "running", current=idx, total=len(results), detail=(err or "抓取失败")[:180])
                continue
            decision = classify_source(self.db, slug, page.url, page.text, page.content_hash)
            seen_urls.add(decision.canonical_url)
            # Exact or near-identical syndicated copies are recorded as aliases, not independent evidence.
            if decision.duplicate_evidence_id and decision.change_kind in {"exact-copy", "syndicated-copy"}:
                self.db.add_source_alias(
                    slug, decision.duplicate_evidence_id, decision.canonical_url,
                    urlparse(page.url).hostname or "", page.title or meta.get("title", ""), decision.change_kind,
                )
                self.db.set_run_step(run_id, "fetch", "running", current=idx, total=len(results), detail=f"去重：{decision.change_kind}")
                continue

            if decision.parent_evidence_id:
                previous = self.db.get_evidence(decision.parent_evidence_id) or {}
                old_text = self.knowledge_pipeline.read_archived_text(previous)
                decision.change_ratio = change_ratio(old_text, page.text)
                decision.change_excerpt = changed_excerpt(old_text, page.text)
                decision.change_kind = "minor-update" if decision.change_ratio <= 0.22 else "major-update"

            excerpt = page.text[:7000]
            grade = source_grade(page.url, official_domains)
            dates = extract_date_candidates(f"{page.title}\n{excerpt}")
            archive_path = self._archive(slug, page)
            process_text = page.text
            processed_scope = "full"
            if decision.change_kind == "minor-update" and len(decision.change_excerpt.strip()) >= 120:
                # changedetection-style incremental analysis: archive full page, analyze only the delta.
                process_text = decision.change_excerpt
                processed_scope = "delta"
            ev = {
                "topic_slug": slug,
                "url": page.url,
                "title": page.title or meta.get("title", ""),
                "source_domain": urlparse(page.url).hostname or "",
                "source_grade": grade,
                "source_kind": "web",
                "content_hash": page.content_hash,
                "excerpt": excerpt,
                "analysis": {
                    "rule_material_score": material_score(process_text),
                    "date_candidates": dates,
                    "processed_scope": processed_scope,
                    "change_kind": decision.change_kind,
                    "change_ratio": decision.change_ratio,
                },
                "date_candidates": dates,
                "is_material": material_score(process_text) > 0,
                "archive_path": archive_path,
                "processing_status": "pending",
                "canonical_url": decision.canonical_url,
                "parent_evidence_id": decision.parent_evidence_id,
                "change_ratio": decision.change_ratio,
                "change_kind": decision.change_kind,
                "change_excerpt": decision.change_excerpt,
                "source_group_id": decision.source_group_id,
                "simhash": decision.simhash,
            }
            eid = self.db.add_evidence(ev)
            if eid:
                ev["id"] = eid
                new_evidence.append(ev)
                process_text_by_id[eid] = process_text
            self.db.set_run_step(run_id, "fetch", "running", current=idx, total=len(results), detail=f"{decision.change_kind} · 新增 {len(new_evidence)}")
        self.db.set_run_step(run_id, "fetch", "done", current=len(results), total=len(results), detail=f"新增原始证据 {len(new_evidence)}")
        self.db.update_run(run_id, progress=progress, new_count=len(new_evidence), message=f"新增 {len(new_evidence)} 条原始证据")
        return new_evidence, process_text_by_id, seen_urls

    async def _extract_batch(self, run_id: int, slug: str, evidence: list[dict], text_by_id: dict[int, str], *, include_backfill: bool = False) -> None:
        processing_ids: list[int] = []
        if include_backfill:
            evidence = evidence + self.db.list_unprocessed_evidence(slug, self.settings.backfill_evidence_per_run)
        for e in evidence:
            eid = int(e["id"])
            if eid not in processing_ids:
                processing_ids.append(eid)
        self.db.set_run_step(run_id, "extract", "running", current=0, total=len(processing_ids), detail="分块完整性账本")
        for eidx, eid in enumerate(processing_ids, start=1):
            async def cb(done: int, total: int, state: str, eidx=eidx):
                pct = 42 + int(30 * ((eidx - 1) + done / max(1, total)) / max(1, len(processing_ids)))
                self.db.update_run(run_id, progress=min(74, pct), message=f"知识提炼：证据 {eidx}/{len(processing_ids)} · 分块 {done}/{total} · {state}")
                self.db.set_run_step(run_id, "extract", "running", current=eidx - 1, total=len(processing_ids), detail=f"证据 {eidx} · chunk {done}/{total} · {state}")
            await self.knowledge_pipeline.process_evidence(eid, text_by_id.get(eid), progress_cb=cb)
            self.db.set_run_step(run_id, "extract", "running", current=eidx, total=len(processing_ids), detail=f"证据 {eid} 完整处理")
        self.db.set_run_step(run_id, "extract", "done", current=len(processing_ids), total=len(processing_ids), detail="所有分块完成")

    async def _refresh_topic_unlocked(self, slug: str, mode: str, run_id: int | None = None) -> int:
        cfg = self.topics_config[slug]
        run_id = run_id or self.db.create_run(slug, mode)
        try:
            today = date.today().isoformat()
            due_watch = self.db.due_watch_nodes(slug, today)
            self.db.set_run_step(run_id, "plan", "running", detail="基础词 + 自定义词 + 到期节点")
            self.db.update_run(run_id, progress=3, message="生成增量检索任务")

            seen_urls: set[str] = set()
            seed_items = []
            for u in cfg.get("seed_urls", []):
                c = canonicalize_url(u)
                if c not in seen_urls:
                    seed_items.append({"title": "固定监测入口", "url": u, "snippet": "", "canonical_url": c})
                    seen_urls.add(c)

            queries = list(cfg.get("queries", [])) + self.db.enabled_custom_queries(slug)
            for node in due_watch:
                queries.extend(node.get("queries", []))
            seen_q = set()
            queries = [q for q in queries if q and not (q in seen_q or seen_q.add(q))]
            self.db.set_run_step(run_id, "plan", "done", current=len(queries), total=len(queries), detail=f"首轮 {len(queries)} 个查询")

            candidates = await self._search_candidates(
                run_id, slug, queries, seen_urls,
                progress_start=4, progress_span=20, limit=max(1, self.settings.max_candidates_per_topic - len(seed_items)),
            )
            items = (seed_items + candidates)[: self.settings.max_candidates_per_topic]
            self.db.update_run(run_id, found_count=len(items), progress=26, message=f"抓取 {len(items)} 个候选页面")
            initial_ev, text_by_id, newly_seen = await self._fetch_new_evidence(run_id, slug, cfg, items, progress=40)
            seen_urls.update(newly_seen)
            await self._extract_batch(run_id, slug, initial_ev, text_by_id, include_backfill=True)

            new_evidence = list(initial_ev)
            new_claims: list[dict] = []
            for e in new_evidence:
                new_claims.extend(self.db.claims_for_evidence(int(e["id"]), include_duplicates=False))

            # GPT-Researcher/LDR-style single bounded reflection round: ask what key evidence is still missing,
            # then search only those gaps. One round prevents runaway work on this NAS.
            if getattr(self.settings, "enable_gap_search", False) and (new_claims or due_watch):
                self.db.set_run_step(run_id, "followup", "running", detail="识别知识缺口")
                followups = await self.analyzer.plan_followup_queries(
                    {**cfg, **(self.db.get_topic(slug) or {})}, new_claims, due_watch, queries,
                    max_queries=int(getattr(self.settings, "max_followup_queries", 2)),
                )
                if followups:
                    fqueries = [x["query"] for x in followups]
                    fcandidates = await self._search_candidates(run_id, slug, fqueries, seen_urls, progress_start=74, progress_span=4, limit=min(8, self.settings.max_candidates_per_topic))
                    fev, ftext, fseen = await self._fetch_new_evidence(run_id, slug, cfg, fcandidates, progress=78)
                    seen_urls.update(fseen)
                    await self._extract_batch(run_id, slug, fev, ftext, include_backfill=False)
                    new_evidence.extend(fev)
                    for e in fev:
                        new_claims.extend(self.db.claims_for_evidence(int(e["id"]), include_duplicates=False))
                    self.db.set_run_step(run_id, "followup", "done", current=len(fqueries), total=len(fqueries), detail=f"补搜新增 {len(fev)} 条证据")
                else:
                    self.db.set_run_step(run_id, "followup", "skipped", detail="未发现值得追加搜索的关键缺口")

            if not new_claims and not due_watch:
                self.db.set_run_step(run_id, "analysis", "skipped", detail="无新增知识/到期节点")
                self.db.update_run(run_id, status="done", progress=100, finished_at=now_iso(), message="无新增证据；完成检索并跳过27B重复分析", new_count=0)
                return run_id

            self.db.set_run_step(run_id, "analysis", "running", detail=f"新知识 {len(new_claims)} 条 · 混合检索历史知识")
            self.db.update_run(run_id, progress=82, message=f"Qwen3.8综合判断 · 新知识 {len(new_claims)} 条")
            full_scan = mode in {"manual", "scheduled", "manual-all", "queued", "queued-all"}
            await self._analyze_and_finish_topic(slug, new_claims, new_evidence, full_scan=full_scan)
            self.db.set_run_step(run_id, "analysis", "done", detail="阶段/风险/预测/知识生命周期已更新")
            self.db.update_run(run_id, status="done", progress=100, finished_at=now_iso(), message="完成：变化检测 + 完整分块 + 长期知识 + 缺口补搜 + 综合判断", new_count=len(new_evidence))
            return run_id
        except Exception as e:
            self.db.set_run_step(run_id, "error", "failed", detail=str(e))
            self.db.update_run(run_id, status="failed", progress=100, finished_at=now_iso(), message="失败；队列/分块断点已保留，可自动续跑", error=str(e))
            raise
