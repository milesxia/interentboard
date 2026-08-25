from __future__ import annotations

import json

from app.services.rules import conservative_stage_hint, material_score

SYSTEM_PROMPT = """你是运行在NAS本地的长期项目情报分析器。你的工作是比较历史基线与本次新增证据，输出可追溯的结构化结论。
必须谨慎：计划不等于已发生；规划红线不等于征收；中标不等于开工；名称相近不自动等同；弱背景不得升级项目阶段。
严格区分官方已确认、用户确认、暂未发现、分析推断。只有新证据足够时才能建议阶段变化。预测必须写触发条件和置信度。
输出必须是合法JSON，不要输出Markdown。"""


class Analyzer:
    def __init__(self, ollama, context_limit: int = 18000):
        self.ollama = ollama
        self.context_limit = context_limit

    async def analyze(self, topic: dict, baseline: str, recent_summaries: list[str], evidence: list[dict], due_watch: list[dict]) -> dict:
        compact = []
        for e in evidence:
            compact.append(
                {
                    "id": e.get("id"),
                    "title": e.get("title"),
                    "url": e.get("url"),
                    "source_grade": e.get("source_grade"),
                    "date_candidates": e.get("date_candidates", []),
                    "excerpt": e.get("excerpt", "")[:3000],
                    "rule_material_score": material_score(e.get("excerpt", "")),
                    "rule_stage_hint": conservative_stage_hint(e.get("excerpt", "")),
                }
            )
        current_stage = topic.get("stage_state_obj") or topic.get("stage_state") or {}
        user = f"""专题：{topic['name']}
当前有效状态：{topic.get('current_state','')}
当前摘要：{topic.get('last_summary') or topic.get('current_summary','')}
当前阶段：{json.dumps(current_stage, ensure_ascii=False)}
检索纪律：{topic.get('discipline','')}
最近摘要：{json.dumps(recent_summaries, ensure_ascii=False)}
到期/待复核节点：{json.dumps(due_watch, ensure_ascii=False)}

历史基线节选：
{baseline[: self.context_limit // 2]}

本次新增候选证据：
{json.dumps(compact, ensure_ascii=False)[: self.context_limit]}

请输出JSON字段：
current_state: 1句话概括当前有效状态，不要只写“无新增”；
conclusion: 2-4句，先说是否有实质变化；
changes: 数组，只列真正新增/补漏/到期复核/状态修正，每项含 title,type,reason,evidence_ids；
risk_direction: up/down/unchanged/not_applicable；
stage_changes: 数组，每项含 object,old_stage,suggested_stage,reason,confidence,evidence_ids；
predictions: 仅分析推断数组，每项含 window,judgement,confidence,trigger；
next_watch: 1-3项；
watch_updates: 对到期节点的建议结果数组，每项含 id,status(completed/adjusted/unconfirmed),reason,evidence_ids；
confidence: 0-1。
若没有实质变化，changes必须为空，不要用旧闻填充。"""
        result = await self.ollama.chat_json(SYSTEM_PROMPT, user)
        if not isinstance(result, dict):
            raise ValueError("AI result is not an object")
        result.setdefault("current_state", topic.get("current_state", ""))
        result.setdefault("changes", [])
        result.setdefault("stage_changes", [])
        result.setdefault("predictions", [])
        result.setdefault("next_watch", [])
        result.setdefault("watch_updates", [])
        result.setdefault("risk_direction", "unchanged")
        return result
