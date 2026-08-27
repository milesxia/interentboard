from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import inspect, text

from . import models as db_models
from .db import engine, session_scope
from .tasks import ensure_run_enqueued
from .queue_runtime import build_queue_view, reset_recovery_state

logger = logging.getLogger("internetboard.intelligence")
router = APIRouter(prefix="/api/intelligence", tags=["intelligence"])

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
SUMMARY_DIR = DATA_DIR / "daily_summaries"
CHAT_DIR = DATA_DIR / "knowledge_chat"
SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
CHAT_DIR.mkdir(parents=True, exist_ok=True)

TZ = ZoneInfo(os.getenv("TZ", "Asia/Shanghai"))
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.8:27b-q4_K_M")
OLLAMA_TIMEOUT_SECONDS = int(os.getenv("OLLAMA_TIMEOUT_SECONDS", "900"))
OLLAMA_CONTEXT_LENGTH = int(os.getenv("OLLAMA_CONTEXT_LENGTH", "8192"))
OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "-1")

_TABLE_HINTS = ("run", "topic", "source", "claim", "entity", "relation", "chunk", "note")
_TIME_KEYS = (
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
    "completed_at",
    "discovered_at",
    "fetched_at",
    "published_at",
    "timestamp",
    "time",
    "date",
)


class KnowledgeQuestion(BaseModel):
    question: str = Field(min_length=2, max_length=2000)
    max_evidence: int = Field(default=60, ge=10, le=120)
    topic_id: int | None = None
    days: int = Field(default=3650, ge=1, le=3650)


class ResumeResponse(BaseModel):
    run_id: int
    status: str
    enqueue_result: str | None


def _status_text(value: Any) -> str:
    if value is None:
        return "unknown"
    raw = getattr(value, "value", value)
    raw = str(raw)
    if "." in raw:
        raw = raw.rsplit(".", 1)[-1]
    return raw.strip().lower()


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (list, tuple, set)):
        return [_json_value(x) for x in value]
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    enum_value = getattr(value, "value", None)
    if enum_value is not None:
        return _json_value(enum_value)
    return str(value)


def _model_to_dict(obj: Any) -> dict[str, Any]:
    mapper = inspect(obj).mapper
    out: dict[str, Any] = {}
    for attr in mapper.column_attrs:
        try:
            out[attr.key] = _json_value(getattr(obj, attr.key))
        except Exception:
            continue
    return out


def _research_run_model():
    model = getattr(db_models, "ResearchRun", None)
    if model is None:
        raise HTTPException(status_code=500, detail="ResearchRun model is unavailable")
    return model


def _run_rows(limit: int = 100) -> list[dict[str, Any]]:
    Run = _research_run_model()
    with session_scope() as session:
        rows = session.query(Run).order_by(Run.id.desc()).limit(limit).all()
        return [_model_to_dict(row) for row in rows]


def _classify_runs(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    active_names = {
        "waiting",
        "queued",
        "pending",
        "running",
        "searching",
        "fetching",
        "chunking",
        "analyzing",
        "synthesizing",
        "processing",
    }
    failed_names = {"failed", "error"}
    completed_names = {"completed", "complete", "success", "succeeded", "done"}
    active: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    completed: list[dict[str, Any]] = []
    other: list[dict[str, Any]] = []
    for row in rows:
        status = _status_text(row.get("status"))
        row = {**row, "status": status}
        if status in active_names:
            active.append(row)
        elif status in failed_names:
            failed.append(row)
        elif status in completed_names:
            completed.append(row)
        else:
            other.append(row)
    return {"active": active, "failed": failed, "completed": completed, "other": other}


@router.get("/tasks")
def intelligence_tasks(limit: int = Query(default=200, ge=10, le=500)):
    rows = _run_rows(limit)
    grouped = _classify_runs(rows)
    queue = build_queue_view(rows)
    return {
        "counts": {key: len(value) for key, value in grouped.items()},
        "queue": queue,
        **grouped,
    }


def _enum_waiting(current: Any) -> Any:
    enum_cls = type(current)
    for name in ("WAITING", "QUEUED", "PENDING"):
        value = getattr(enum_cls, name, None)
        if value is not None:
            return value
    raw = getattr(current, "value", current)
    if isinstance(raw, str) and raw.isupper():
        return "WAITING"
    return "waiting"


@router.post("/runs/{run_id}/resume", response_model=ResumeResponse)
def resume_failed_run(run_id: int):
    Run = _research_run_model()
    with session_scope() as session:
        run = session.get(Run, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
        status = _status_text(getattr(run, "status", None))
        if status not in {"failed", "error"}:
            raise HTTPException(status_code=409, detail=f"Run {run_id} is {status}, not failed")
        run.status = _enum_waiting(getattr(run, "status", None))
        for field, value in (
            ("message", "Manual resume queued from intelligence center"),
            ("error", None),
            ("error_message", None),
            ("finished_at", None),
            ("completed_at", None),
        ):
            if hasattr(run, field):
                try:
                    setattr(run, field, value)
                except Exception:
                    pass
    reset_recovery_state(run_id)
    result = ensure_run_enqueued(run_id, reason="manual failed-run resume from intelligence center")
    if not result:
        raise HTTPException(status_code=503, detail=f"Run {run_id} reset but could not enter Celery queue")
    return ResumeResponse(run_id=run_id, status="waiting", enqueue_result=str(result))


def _safe_identifier(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"Unsafe SQL identifier: {name}")
    return name


def _choose_order_column(columns: set[str]) -> str | None:
    for candidate in (
        "updated_at",
        "created_at",
        "finished_at",
        "completed_at",
        "started_at",
        "discovered_at",
        "fetched_at",
        "id",
    ):
        if candidate in columns:
            return candidate
    return None


def _interesting_tables() -> list[str]:
    inspector = inspect(engine)
    names = inspector.get_table_names()
    selected = [name for name in names if any(hint in name.lower() for hint in _TABLE_HINTS)]
    return sorted(selected)


def _fetch_table_rows(table_name: str, limit: int = 160) -> list[dict[str, Any]]:
    table_name = _safe_identifier(table_name)
    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns(table_name)}
    order_col = _choose_order_column(columns)
    order_sql = f' ORDER BY "{_safe_identifier(order_col)}" DESC' if order_col else ""
    sql = text(f'SELECT * FROM "{table_name}"{order_sql} LIMIT :limit')
    with engine.connect() as conn:
        rows = conn.execute(sql, {"limit": limit}).mappings().all()
    return [{str(k): _json_value(v) for k, v in dict(row).items()} for row in rows]


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=TZ)
    elif isinstance(value, str):
        raw = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def _row_dates(row: dict[str, Any]) -> list[date]:
    dates: list[date] = []
    for key, value in row.items():
        key_l = key.lower()
        if not (key_l.endswith("_at") or key_l in _TIME_KEYS or "date" in key_l or "time" in key_l):
            continue
        dt = _parse_datetime(value)
        if dt is not None:
            dates.append(dt.date())
    return dates


def _snapshot(target_date: date | None = None, per_table_limit: int = 160) -> dict[str, Any]:
    data: dict[str, list[dict[str, Any]]] = {}
    today_run_ids: set[int] = set()

    for table in _interesting_tables():
        try:
            rows = _fetch_table_rows(table, per_table_limit)
        except Exception as exc:
            logger.warning("snapshot table failed table=%s error=%s", table, exc)
            continue
        data[table] = rows

    if target_date is not None:
        for table, rows in data.items():
            if "run" not in table.lower():
                continue
            for row in rows:
                if target_date in _row_dates(row):
                    rid = row.get("id") or row.get("run_id")
                    if isinstance(rid, int):
                        today_run_ids.add(rid)

        filtered: dict[str, list[dict[str, Any]]] = {}
        for table, rows in data.items():
            selected: list[dict[str, Any]] = []
            for row in rows:
                dates = _row_dates(row)
                linked_run = row.get("run_id") in today_run_ids or row.get("research_run_id") in today_run_ids
                if target_date in dates or linked_run:
                    selected.append(row)
            if selected:
                filtered[table] = selected
        data = filtered

    return {
        "date": target_date.isoformat() if target_date else None,
        "tables": data,
        "table_counts": {name: len(rows) for name, rows in data.items()},
    }


def _compact_row(table: str, row: dict[str, Any], max_chars: int = 1400) -> str:
    preferred_keys = (
        "id",
        "topic_id",
        "run_id",
        "research_run_id",
        "status",
        "name",
        "title",
        "url",
        "summary",
        "claim",
        "text",
        "content",
        "message",
        "error",
        "error_message",
        "created_at",
        "updated_at",
        "finished_at",
    )
    ordered: dict[str, Any] = {}
    for key in preferred_keys:
        if key in row and row[key] not in (None, "", [], {}):
            ordered[key] = row[key]
    for key, value in row.items():
        if key not in ordered and value not in (None, "", [], {}):
            ordered[key] = value
        if len(json.dumps(ordered, ensure_ascii=False, default=str)) >= max_chars:
            break
    raw = json.dumps(ordered, ensure_ascii=False, default=str)
    if len(raw) > max_chars:
        raw = raw[: max_chars - 3] + "..."
    return f"{table}: {raw}"


def _ollama_chat(system_prompt: str, user_prompt: str, temperature: float = 0.2) -> tuple[str, float]:
    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "keep_alive": OLLAMA_KEEP_ALIVE,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "options": {
            "num_ctx": OLLAMA_CONTEXT_LENGTH,
            "temperature": temperature,
        },
    }
    request = urllib.request.Request(
        f"{OLLAMA_BASE_URL}/api/chat",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=OLLAMA_TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise HTTPException(status_code=502, detail=f"Ollama HTTP {exc.code}: {detail}") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Ollama request failed: {exc}") from exc
    elapsed = round(time.monotonic() - started, 2)
    try:
        body = json.loads(raw)
        content = body["message"]["content"].strip()
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Ollama returned an invalid /api/chat response") from exc
    return content, elapsed


def _summary_file(target_date: date) -> Path:
    return SUMMARY_DIR / f"{target_date.isoformat()}.json"


@router.get("/daily/{day}")
def get_daily_summary(day: str):
    try:
        target = date.fromisoformat(day)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Date must be YYYY-MM-DD") from exc
    path = _summary_file(target)
    snapshot = _snapshot(target)
    if not path.exists():
        return {
            "exists": False,
            "date": target.isoformat(),
            "summary": None,
            "snapshot_counts": snapshot["table_counts"],
        }
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Daily summary cache is unreadable: {exc}") from exc
    return {"exists": True, **cached, "snapshot_counts": snapshot["table_counts"]}


@router.post("/daily/{day}/generate")
def generate_daily_summary(day: str):
    try:
        target = date.fromisoformat(day)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Date must be YYYY-MM-DD") from exc
    snapshot = _snapshot(target)
    evidence_lines: list[str] = []
    for table, rows in snapshot["tables"].items():
        for row in rows[:80]:
            evidence_lines.append(_compact_row(table, row, 1100))
    if not evidence_lines:
        return {
            "exists": False,
            "date": target.isoformat(),
            "summary": "当天尚未发现可用于生成总结的入库数据。",
            "snapshot_counts": snapshot["table_counts"],
        }
    context = "\n".join(f"[D{i}] {line}" for i, line in enumerate(evidence_lines, 1))[:22000]
    system_prompt = (
        "你是 InternetBoard 的本地情报分析员。只能依据提供的本地数据库证据总结，不要补造事实。"
        "输出中文 Markdown。所有重要判断尽量使用 [D编号] 标注证据。"
    )
    user_prompt = (
        f"请对 {target.isoformat()} 的本地情报库生成当日总结。\n\n"
        "固定结构：\n"
        "# 当日结论\n"
        "## 1. 今日发生了什么\n"
        "## 2. 关键新增 Claim / Entity / Relation\n"
        "## 3. 趋势与变化\n"
        "## 4. 风险、矛盾与信息缺口\n"
        "## 5. 明日/后续重点监控\n"
        "## 6. 任务运行质量（包括失败任务对结论完整性的影响）\n\n"
        "不要把抓取到的每条内容机械罗列；优先提炼跨来源共同趋势。\n\n"
        f"本地证据：\n{context}"
    )
    summary, elapsed = _ollama_chat(system_prompt, user_prompt, temperature=0.15)
    payload = {
        "date": target.isoformat(),
        "generated_at": datetime.now(TZ).isoformat(),
        "model": OLLAMA_MODEL,
        "elapsed_seconds": elapsed,
        "summary": summary,
        "snapshot_counts": snapshot["table_counts"],
    }
    _summary_file(target).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"exists": True, **payload}


def _terms(text_value: str) -> set[str]:
    text_value = text_value.lower().strip()
    ascii_words = set(re.findall(r"[a-z0-9_.-]{2,}", text_value))
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]", text_value))
    grams = {cjk[i : i + 2] for i in range(max(0, len(cjk) - 1))}
    chars = set(cjk) if len(cjk) <= 10 else set()
    return ascii_words | grams | chars


def _flatten_snapshot(snapshot: dict[str, Any]) -> list[tuple[str, dict[str, Any], str]]:
    docs: list[tuple[str, dict[str, Any], str]] = []
    for table, rows in snapshot["tables"].items():
        for row in rows:
            raw = _compact_row(table, row, 1800)
            docs.append((table, row, raw))
    return docs


def _retrieve(question: str, max_evidence: int, topic_id: int | None = None, days: int = 90) -> list[dict[str, Any]]:
    snapshot = _snapshot(None, per_table_limit=600)
    cutoff = datetime.now(TZ).date().toordinal() - max(1, days)
    q_terms = _terms(question)
    scored: list[tuple[float, str, dict[str, Any], str]] = []
    for table, row, raw in _flatten_snapshot(snapshot):
        if topic_id is not None:
            row_topic = row.get("topic_id")
            if row_topic is not None and row_topic != topic_id:
                continue
        row_dates = _row_dates(row)
        if row_dates and max(d.toordinal() for d in row_dates) < cutoff:
            continue
        r_terms = _terms(raw)
        overlap = len(q_terms & r_terms)
        table_bonus = 0.0
        lower_table = table.lower()
        if "claim" in lower_table:
            table_bonus += 2.5
        if "source" in lower_table:
            table_bonus += 2.0
        if "relation" in lower_table or "entity" in lower_table:
            table_bonus += 1.5
        if "run" in lower_table:
            table_bonus += 0.8
        if overlap == 0 and q_terms:
            continue
        score = overlap * 4.0 + table_bonus
        rid = row.get("id")
        if isinstance(rid, int):
            score += min(rid / 1_000_000.0, 0.5)
        scored.append((score, table, row, raw))

    if not scored:
        for table, row, raw in _flatten_snapshot(snapshot)[:max_evidence]:
            scored.append((0.1, table, row, raw))

    scored.sort(key=lambda item: item[0], reverse=True)
    selected = scored[:max_evidence]
    return [
        {
            "ref": f"K{idx}",
            "score": round(score, 3),
            "table": table,
            "row": row,
            "text": raw,
        }
        for idx, (score, table, row, raw) in enumerate(selected, 1)
    ]


@router.post("/ask")
def ask_knowledge_base(body: KnowledgeQuestion):
    question = body.question.strip()
    evidence = _retrieve(question, body.max_evidence, topic_id=body.topic_id, days=body.days)
    context_parts: list[str] = []
    for item in evidence:
        context_parts.append(f"[{item['ref']}] {item['text']}")
    context = "\n".join(context_parts)[:24000]
    system_prompt = (
        "你是 InternetBoard 的本地知识库问答助手。你只能基于给出的本地证据回答。"
        "如果证据不足，明确写‘现有知识库不足以确认’，不要用常识补齐。"
        "重要事实、趋势判断和预测必须尽量引用 [K编号]。"
        "需要区分：事实、推断、预测。回答使用中文。"
    )
    user_prompt = (
        f"用户问题：{question}\n\n"
        "请先给直接结论，再给证据链；如果涉及时间变化，要做历史对比；如果存在互相矛盾的来源，要指出。\n\n"
        f"本地知识库证据：\n{context}"
    )
    answer, elapsed = _ollama_chat(system_prompt, user_prompt, temperature=0.2)
    now = datetime.now(TZ)
    log_path = CHAT_DIR / f"{now.date().isoformat()}.jsonl"
    log_entry = {
        "created_at": now.isoformat(),
        "question": question,
        "answer": answer,
        "model": OLLAMA_MODEL,
        "elapsed_seconds": elapsed,
        "evidence_refs": [item["ref"] for item in evidence[:20]],
    }
    with log_path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
    return {
        "question": question,
        "answer": answer,
        "model": OLLAMA_MODEL,
        "elapsed_seconds": elapsed,
        "evidence": [
            {"ref": item["ref"], "table": item["table"], "score": item["score"], "row": item["row"]}
            for item in evidence[:20]
        ],
    }
