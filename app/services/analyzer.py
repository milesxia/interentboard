from __future__ import annotations

import json
from typing import Any

from app.services.chunker import estimate_tokens, pack_jsonish
from app.services.rules import conservative_stage_hint, material_score


EXTRACT_SYSTEM_PROMPT = """你是证据抽取器。只提取输入原文明确表达的信息，不做延伸推理，不把计划写成已发生，不把传闻写成事实。
必须保留不确定性和时间语气。原文没有日期就填null。输出必须是合法JSON，不要Markdown。
人工情报也只能按用户原话提炼；用户说“听说/可能/预计”时必须保留为rumor/expected，不能升级为confirmed。"""

REDUCE_SYSTEM_PROMPT = """你是证据融合器。输入是已经从原文逐块提取出的结构化事实。你的任务是去重、合并同义表述、显式保留冲突，不得新增输入中不存在的事实。
每一项必须保留 evidence_ids 和 claim_ids 作为溯源。同一个 source_group_id 的多篇转载/页面版本只能算一个独立来源，不能因为转载数量多就提高可信度。输出合法JSON，不要Markdown。"""

FINAL_SYSTEM_PROMPT = """你是运行在NAS本地的长期项目情报分析器。输入已经经过完整分块抽取和证据融合。
你的工作是比较历史状态、长期知识和本次新增证据，输出可追溯的结构化结论。
必须谨慎：计划不等于已发生；规划红线不等于征收；中标不等于开工；名称相近不自动等同；弱背景不得升级项目阶段。
严格区分官方已确认、用户输入、媒体报道、暂未发现、分析推断。只有证据足够时才能建议阶段变化。预测必须写触发条件和置信度。
人工修改过的长期知识优先级最高；若新证据与其冲突，只能指出冲突，不得静默覆盖。
输出必须是合法JSON，不要Markdown。"""


class Analyzer:
    def __init__(
        self,
        ollama,
        *,
        chunk_tokens: int = 2400,
        extract_predict: int = 320,
        reduce_tokens: int = 3200,
        reduce_predict: int = 500,
        final_tokens: int = 5000,
        final_predict: int = 1200,
    ):
        self.ollama = ollama
        self.chunk_tokens = chunk_tokens
        self.extract_predict = extract_predict
        self.reduce_tokens = reduce_tokens
        self.reduce_predict = reduce_predict
        self.final_tokens = final_tokens
        self.final_predict = final_predict

    async def extract_chunk(self, evidence: dict, chunk: dict) -> dict:
        manual_meta = {}
        try:
            raw_meta = evidence.get("analysis_json")
            if isinstance(raw_meta, str) and raw_meta:
                manual_meta = json.loads(raw_meta)
            elif isinstance(raw_meta, dict):
                manual_meta = raw_meta
        except Exception:
            manual_meta = {}
        source_context = {
            "evidence_id": evidence.get("id"),
            "title": evidence.get("title"),
            "url": evidence.get("url"),
            "source_kind": evidence.get("source_kind", "web"),
            "source_grade": evidence.get("source_grade", "C"),
            "user_confidence_label": manual_meta.get("confidence_label"),
            "publish_date": evidence.get("publish_date"),
            "event_date": evidence.get("event_date"),
            "chunk": f"{int(chunk.get('chunk_index', 0)) + 1}/{chunk.get('total_chunks', 1)}",
        }
        user = f"""来源元数据：
{json.dumps(source_context, ensure_ascii=False)}

原文分块：
{chunk.get('content', '')}

只从这段原文抽取信息。输出JSON：
{{
  "claims": [
    {{
      "statement": "完整但简洁的一条信息",
      "type": "fact|plan|forecast|rumor|denial|background",
      "event_date": "YYYY-MM-DD或null",
      "certainty": "confirmed|reported|expected|rumor|unknown",
      "confidence": 0到1,
      "entities": ["实体"],
      "tags": ["关键词"]
    }}
  ],
  "chunk_summary": "最多2句，只概括本块",
  "has_material_change": true或false
}}
最多抽取12条claims；没有有效信息时claims为空。"""
        if estimate_tokens(user) > self.chunk_tokens + 700:
            # The splitter is conservative, but metadata/system prompt also consume context.
            raise ValueError(f"chunk prompt exceeds local budget: {estimate_tokens(user)} tokens")
        result = await self.ollama.chat_json(
            EXTRACT_SYSTEM_PROMPT,
            user,
            purpose="extract",
            num_ctx=max(4096, self.chunk_tokens + self.extract_predict + 1200),
            num_predict=self.extract_predict,
            temperature=0.0,
            think=False,
        )
        claims = result.get("claims")
        if not isinstance(claims, list):
            result["claims"] = []
        return result

    @staticmethod
    def _compact_claim(claim: dict) -> dict:
        return {
            "claim_id": claim.get("id"),
            "evidence_id": claim.get("evidence_id"),
            "statement": claim.get("statement", ""),
            "type": claim.get("claim_type") or claim.get("type") or "fact",
            "event_date": claim.get("event_date"),
            "certainty": claim.get("certainty", "unknown"),
            "confidence": claim.get("confidence", 0.5),
            "source_grade": claim.get("source_grade", "C"),
            "source_kind": claim.get("source_kind", "web"),
            "human_override": bool(claim.get("human_override")),
            "source_group_id": claim.get("source_group_id", ""),
            "entities": claim.get("entities") or [],
        }

    async def _reduce_batch(self, compact_items: list[dict]) -> dict:
        user = f"""待融合事实：
{json.dumps(compact_items, ensure_ascii=False, separators=(',', ':'))}

输出JSON：
{{
  "items": [
    {{
      "statement": "融合后的陈述",
      "event_date": "YYYY-MM-DD或null",
      "certainty": "confirmed|reported|expected|rumor|unknown",
      "confidence": 0到1,
      "evidence_ids": [1,2],
      "claim_ids": [10,11],
      "source_groups": ["src-xxx"],
      "human_override": false
    }}
  ],
  "conflicts": [
    {{"topic":"冲突点","sides":["说法A","说法B"],"evidence_ids":[1,2]}}
  ]
}}
不要为了压缩而丢掉互相冲突的说法。"""
        return await self.ollama.chat_json(
            REDUCE_SYSTEM_PROMPT,
            user,
            purpose="reduce",
            num_ctx=max(4096, self.reduce_tokens + self.reduce_predict + 1000),
            num_predict=self.reduce_predict,
            temperature=0.0,
            think=False,
        )

    async def reduce_claims(self, claims: list[dict]) -> dict:
        if not claims:
            return {"items": [], "conflicts": []}
        compact = [self._compact_claim(x) for x in claims]
        serialized = [json.dumps(x, ensure_ascii=False, separators=(",", ":")) for x in compact]
        batches = pack_jsonish(serialized, max(700, self.reduce_tokens - 700))
        merged_items: list[dict] = []
        conflicts: list[dict] = []
        for batch in batches:
            result = await self._reduce_batch([json.loads(x) for x in batch])
            if isinstance(result.get("items"), list):
                merged_items.extend(result["items"])
            if isinstance(result.get("conflicts"), list):
                conflicts.extend(result["conflicts"])

        # Hierarchical reduce: if the first-pass fusion is still too large, fuse again.
        guard = 0
        while estimate_tokens(json.dumps(merged_items, ensure_ascii=False)) > self.reduce_tokens and len(merged_items) > 1 and guard < 4:
            guard += 1
            serial = [json.dumps(x, ensure_ascii=False, separators=(",", ":")) for x in merged_items]
            next_items: list[dict] = []
            for batch in pack_jsonish(serial, max(700, self.reduce_tokens - 700)):
                result = await self._reduce_batch([json.loads(x) for x in batch])
                next_items.extend(result.get("items") or [])
                conflicts.extend(result.get("conflicts") or [])
            if len(next_items) >= len(merged_items) and estimate_tokens(json.dumps(next_items, ensure_ascii=False)) >= estimate_tokens(json.dumps(merged_items, ensure_ascii=False)):
                # Avoid pathological non-shrinking loops. The final composer will take a budgeted prefix,
                # while all raw claims remain in the DB and are never deleted.
                merged_items = next_items
                break
            merged_items = next_items
        return {"items": merged_items, "conflicts": conflicts}

    @staticmethod
    def _trim_json_list(items: list[Any], token_budget: int) -> list[Any]:
        out: list[Any] = []
        for item in items:
            candidate = out + [item]
            if out and estimate_tokens(json.dumps(candidate, ensure_ascii=False)) > token_budget:
                break
            out.append(item)
        return out

    async def plan_followup_queries(self, topic: dict, new_claims: list[dict], due_watch: list[dict], original_queries: list[str], max_queries: int = 2) -> list[dict]:
        if not new_claims and not due_watch:
            return []
        compact = [self._compact_claim(x) for x in new_claims[:45]]
        user = f"""专题：{topic.get('name','')}
当前状态：{topic.get('current_state','')}
监测纪律：{topic.get('discipline','')}
本轮已得到的新增事实：{json.dumps(compact, ensure_ascii=False, separators=(',',':'))}
到期/待核节点：{json.dumps(due_watch, ensure_ascii=False, separators=(',',':'))}
已经搜索过：{json.dumps(original_queries[:20], ensure_ascii=False)}

只寻找仍然影响阶段判断或关键节点、且本轮证据没有回答的知识缺口。输出JSON：
{{"queries":[{{"query":"精确联网检索词","reason":"缺什么证据"}}]}}
最多{max_queries}条。不要生成与已搜索词近义重复的查询；没有有价值缺口就返回空数组。"""
        result = await self.ollama.chat_json(
            EXTRACT_SYSTEM_PROMPT, user, purpose="reduce", num_ctx=4096, num_predict=260, temperature=0.0, think=False
        )
        out=[]
        seen={q.strip().lower() for q in original_queries if q}
        for item in result.get("queries") or []:
            q=str(item.get("query") or "").strip()
            if not q or q.lower() in seen:
                continue
            out.append({"query":q,"reason":str(item.get("reason") or "")[:500]})
            seen.add(q.lower())
            if len(out)>=max_queries:
                break
        return out

    async def analyze(
        self,
        topic: dict,
        baseline: str,
        recent_summaries: list[str],
        new_claims: list[dict],
        historical_claims: list[dict],
        due_watch: list[dict],
    ) -> dict:
        fused = await self.reduce_claims(new_claims)
        current_stage = topic.get("stage_state_obj") or topic.get("stage_state") or {}

        # Explicit token budget allocation. New evidence gets the largest share; history is retrieved,
        # not dumped wholesale. Nothing is silently truncated by Ollama.
        baseline_budget = max(450, int(self.final_tokens * 0.18))
        history_budget = max(800, int(self.final_tokens * 0.28))
        new_budget = max(1200, int(self.final_tokens * 0.38))
        meta_budget = max(500, self.final_tokens - baseline_budget - history_budget - new_budget)

        baseline_text = baseline
        if estimate_tokens(baseline_text) > baseline_budget:
            # BaselineStore already returns relevant chunks; char trim is only a final budget guard.
            ratio = baseline_budget / max(1, estimate_tokens(baseline_text))
            baseline_text = baseline_text[: max(500, int(len(baseline_text) * ratio * 0.92))]

        history_compact = [self._compact_claim(x) for x in historical_claims]
        history_compact = self._trim_json_list(history_compact, history_budget)
        new_items = self._trim_json_list(fused.get("items") or [], new_budget)
        conflicts = self._trim_json_list(fused.get("conflicts") or [], max(350, new_budget // 3))
        meta = {
            "topic": topic.get("name"),
            "current_state": topic.get("current_state", ""),
            "current_summary": topic.get("last_summary") or topic.get("current_summary", ""),
            "current_stage": current_stage,
            "discipline": topic.get("discipline", ""),
            "recent_summaries": recent_summaries[-3:],
            "due_watch": due_watch,
        }
        if estimate_tokens(json.dumps(meta, ensure_ascii=False)) > meta_budget:
            meta["recent_summaries"] = recent_summaries[-1:]

        user = f"""专题上下文：
{json.dumps(meta, ensure_ascii=False)}

历史基线（相关节选）：
{baseline_text}

长期知识库检索结果（历史、含人工修改优先项）：
{json.dumps(history_compact, ensure_ascii=False, separators=(',', ':'))}

本次新增证据融合结果：
{json.dumps(new_items, ensure_ascii=False, separators=(',', ':'))}

本次发现的证据冲突：
{json.dumps(conflicts, ensure_ascii=False, separators=(',', ':'))}

请输出JSON字段：
current_state: 1句话概括当前有效状态；
conclusion: 2-4句，先说是否有实质变化；
changes: 数组，只列真正新增/补漏/到期复核/状态修正，每项含 title,type,reason,evidence_ids；
risk_direction: up/down/unchanged/not_applicable；
stage_changes: 数组，每项含 object,old_stage,suggested_stage,reason,confidence,evidence_ids；
predictions: 仅分析推断数组，每项含 window,judgement,confidence,trigger；
next_watch: 1-3项；
watch_updates: 对到期节点的建议结果数组，每项含 id,status(completed/adjusted/unconfirmed),reason,evidence_ids；
knowledge_updates: 仅当新旧Claim存在明确关系时输出数组，每项含 old_claim_id,new_claim_id,relation(supports/conflicts/supersedes/duplicate),confidence,reason。只有明确的新事实替代旧事实/旧预测时才用supersedes；人工修改项发生冲突时只能conflicts，不能supersedes；
confidence: 0-1。
没有实质变化时 changes 必须为空；不得用旧闻冒充新增。"""

        estimated = estimate_tokens(user)
        if estimated > self.final_tokens + 900:
            raise ValueError(f"final prompt budget guard failed: estimated {estimated} tokens")

        result = await self.ollama.chat_json(
            FINAL_SYSTEM_PROMPT,
            user,
            purpose="final",
            num_ctx=max(8192, self.final_tokens + self.final_predict + 1600),
            num_predict=self.final_predict,
            temperature=0.1,
            think=None,
        )
        if not isinstance(result, dict):
            raise ValueError("AI result is not an object")
        result.setdefault("current_state", topic.get("current_state", ""))
        result.setdefault("changes", [])
        result.setdefault("stage_changes", [])
        result.setdefault("predictions", [])
        result.setdefault("next_watch", [])
        result.setdefault("watch_updates", [])
        result.setdefault("knowledge_updates", [])
        result.setdefault("risk_direction", "unchanged")
        result["evidence_conflicts"] = fused.get("conflicts") or []
        result["pipeline"] = {
            "new_claims": len(new_claims),
            "fused_items": len(fused.get("items") or []),
            "history_claims_used": len(history_compact),
            "estimated_final_input_tokens": estimated,
            "silent_truncation": False,
        }
        return result
