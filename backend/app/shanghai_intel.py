from __future__ import annotations

import hashlib
import html as html_lib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import redis
from sqlalchemy import inspect, text

from . import models as db_models
from .db import engine, session_scope
from .queue_runtime import (
    COMPLETED_NAMES,
    FAILED_NAMES,
    QUEUED_NAMES,
    RUNNING_NAMES,
    complete_ai_job,
    fail_ai_job,
    get_ai_job,
    list_ai_jobs,
    mark_ai_job_running,
)

logger = logging.getLogger("internetboard.shanghai_intel")
TZ = ZoneInfo(os.getenv("TZ", "Asia/Shanghai"))
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
SEARXNG_URL = os.getenv("SEARXNG_URL", "").strip().rstrip("/")
LOCAL_INTEL_TIME_RANGE = os.getenv("LOCAL_INTEL_TIME_RANGE", "day").strip() or "day"
LOCAL_INTEL_RESULTS_PER_QUERY = max(1, min(8, int(os.getenv("LOCAL_INTEL_RESULTS_PER_QUERY", "3"))))
LOCAL_INTEL_MAX_TOPICS = max(1, min(30, int(os.getenv("LOCAL_INTEL_MAX_TOPICS", "12"))))
LOCAL_INTEL_FETCH_TIMEOUT_SECONDS = max(5, min(60, int(os.getenv("LOCAL_INTEL_FETCH_TIMEOUT_SECONDS", "20"))))
LOCAL_INTEL_MIN_SCORE = max(30, min(100, int(os.getenv("LOCAL_INTEL_MIN_SCORE", "60"))))
LOCAL_INTEL_MAX_CONTENT_CHARS = max(2000, min(30000, int(os.getenv("LOCAL_INTEL_MAX_CONTENT_CHARS", "12000"))))
LOCAL_INTEL_MAX_QUERIES_PER_TOPIC = max(16, min(60, int(os.getenv("LOCAL_INTEL_MAX_QUERIES_PER_TOPIC", "40"))))
LOCAL_INTEL_QUERY_DELAY_SECONDS = max(0.0, min(3.0, float(os.getenv("LOCAL_INTEL_QUERY_DELAY_SECONDS", "0.35"))))
DAILY_REPORT_NOT_BEFORE_MINUTE = max(0, min(59, int(os.getenv("DAILY_REPORT_NOT_BEFORE_MINUTE", "15"))))

DISTRICTS = (
    "浦东新区", "黄浦区", "静安区", "徐汇区", "长宁区", "普陀区", "虹口区", "杨浦区",
    "宝山区", "闵行区", "嘉定区", "金山区", "松江区", "青浦区", "奉贤区", "崇明区",
)

MUNICIPAL_SOURCE_QUERIES = (
    'site:shanghai.gov.cn "上海市人民政府" {topic}',
    'site:shanghai.gov.cn "上海市经济和信息化委员会" {topic}',
    'site:shanghai.gov.cn "上海市发展和改革委员会" {topic}',
    'site:shanghai.gov.cn "上海市商务委员会" {topic}',
    'site:shanghai.gov.cn "上海市科学技术委员会" {topic}',
    'site:shanghai.gov.cn "上海市数据局" {topic}',
    'site:shanghai.gov.cn "上海市国有资产监督管理委员会" {topic}',
)

LOCAL_MEDIA_QUERIES = (
    'site:shobserver.cn 上海 {topic}',
    'site:eastday.com 上海 {topic}',
    'site:kankanews.com 上海 {topic}',
)

OFFICIAL_WECHAT_QUERIES = (
    'site:mp.weixin.qq.com "上海发布" "{topic}"',
)

SANLE_SPECIAL_QUERIES = (
    'site:jingan.gov.cn "江宁路街道" "三乐"',
    'site:jingan.gov.cn "江宁路街道" "三乐里"',
    'site:jingan.gov.cn "三乐里居民区"',
    'site:jingan.gov.cn "三乐小区"',
    'site:jingan.gov.cn "江宁路街道" {topic}',
    'site:mp.weixin.qq.com "In江宁" "三乐"',
    'site:mp.weixin.qq.com "In江宁" "三乐里"',
    'site:mp.weixin.qq.com "上海静安" "三乐"',
    'site:mp.weixin.qq.com "江宁路街道" "三乐"',
    '"江宁路街道" "三乐里" 上海',
)

# National/general sources are intentionally excluded from the Shanghai-local primary pool.
EXCLUDED_HOSTS = (
    "xinhuanet.com", "news.cn", "cctv.com", "cctv.cn", "people.com.cn", "chinanews.com.cn",
    "www.gov.cn", "gov.cn", "china.com.cn", "gmw.cn", "thepaper.cn",
)

LOCAL_MEDIA_HOSTS = ("shobserver.cn", "shobserver.com", "eastday.com", "kankanews.com")

# Official Shanghai domains seen in the municipal / district government ecosystem. Generic *.gov.cn
# can still be accepted when title/body clearly anchors to Shanghai or one of the 16 districts.
OFFICIAL_HOST_HINTS = (
    "shanghai.gov.cn", "jingan.gov.cn", "shpt.gov.cn", "shhk.gov.cn", "shmh.gov.cn",
    "shbsq.gov.cn", "pudong.gov.cn", "shqp.gov.cn", "fengxian.gov.cn", "chongming.gov.cn",
    "jiading.gov.cn", "jinshan.gov.cn", "songjiang.gov.cn", "xuhui.gov.cn",
)

SANLE_TERMS = ("三乐", "sanle", "三乐里", "三乐里居民区", "三乐小区")
SANLE_GEO_TERMS = ("静安区", "江宁路街道", "三乐里", "三乐小区", "昌化路")


@dataclass(frozen=True)
class TopicSpec:
    topic_id: int | None
    name: str
    terms: tuple[str, ...]
    sanle: bool


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"}:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            value = re.sub(r"\s+", " ", data).strip()
            if value:
                self.parts.append(value)


def _redis() -> redis.Redis:
    return redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=4, socket_connect_timeout=4)


def _now() -> datetime:
    return datetime.now(TZ)


def _safe_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def ensure_local_tables() -> None:
    ddl = """
    CREATE TABLE IF NOT EXISTS local_source_evidence (
        id BIGSERIAL PRIMARY KEY,
        collected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        published_at TIMESTAMPTZ NULL,
        evidence_date DATE NOT NULL,
        topic_id BIGINT NULL,
        topic_name TEXT NOT NULL,
        source_name TEXT NOT NULL,
        source_level TEXT NOT NULL,
        region TEXT NOT NULL DEFAULT '上海市',
        district TEXT NULL,
        street TEXT NULL,
        title TEXT NOT NULL,
        url TEXT NOT NULL,
        snippet TEXT NULL,
        content TEXT NULL,
        relevance_score INTEGER NOT NULL,
        query_text TEXT NULL,
        content_hash TEXT NOT NULL UNIQUE,
        metadata_json TEXT NULL
    )
    """
    with engine.begin() as conn:
        conn.execute(text(ddl))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_local_source_evidence_date ON local_source_evidence(evidence_date DESC)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_local_source_evidence_topic ON local_source_evidence(topic_name, evidence_date DESC)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_local_source_evidence_region ON local_source_evidence(district, street, evidence_date DESC)"))


def _normalize_terms(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        raw = [str(x) for x in value]
    elif isinstance(value, dict):
        raw = [str(x) for x in value.values()]
    else:
        s = str(value).strip()
        try:
            parsed = json.loads(s)
            if parsed != value:
                return _normalize_terms(parsed)
        except Exception:
            pass
        raw = re.split(r"[,，;；|\n]+", s)
    out: list[str] = []
    for item in raw:
        item = re.sub(r"\s+", " ", str(item)).strip()
        if 1 < len(item) <= 80 and item not in out:
            out.append(item)
    return out[:12]


def _load_topics() -> list[TopicSpec]:
    inspector = inspect(engine)
    table_names = inspector.get_table_names()
    candidates = sorted([name for name in table_names if "topic" in name.lower()], key=lambda x: (0 if x.lower() == "topics" else 1, x))
    specs: list[TopicSpec] = []
    for table_name in candidates:
        cols = {c["name"] for c in inspector.get_columns(table_name)}
        if "id" not in cols:
            continue
        preferred = [c for c in ("name", "title", "topic", "slug", "keywords", "query", "queries", "search_terms") if c in cols]
        if not preferred:
            continue
        order = ' ORDER BY "id" ASC' if "id" in cols else ""
        try:
            with engine.connect() as conn:
                rows = conn.execute(text(f'SELECT * FROM "{table_name}"{order} LIMIT :lim'), {"lim": LOCAL_INTEL_MAX_TOPICS * 2}).mappings().all()
        except Exception:
            continue
        for row in rows:
            if "enabled" in cols and row.get("enabled") is False:
                continue
            if "is_active" in cols and row.get("is_active") is False:
                continue
            name = str(row.get("name") or row.get("title") or row.get("topic") or row.get("slug") or f"topic-{row.get('id')}").strip()
            terms = [name]
            for field in ("keywords", "query", "queries", "search_terms"):
                if field in cols:
                    terms.extend(_normalize_terms(row.get(field)))
            dedup: list[str] = []
            for term in terms:
                term = re.sub(r"\s+", " ", str(term)).strip()
                if term and term not in dedup:
                    dedup.append(term)
            hay = " ".join(dedup).lower()
            sanle = any(term.lower() in hay for term in SANLE_TERMS)
            specs.append(TopicSpec(int(row.get("id")) if isinstance(row.get("id"), int) else None, name, tuple(dedup[:6]), sanle))
            if len(specs) >= LOCAL_INTEL_MAX_TOPICS:
                return specs
        if specs:
            break
    if not specs:
        specs = [TopicSpec(None, "上海本地综合", ("上海", "政策", "产业", "社区"), False)]
    return specs


def _topic_anchor(spec: TopicSpec) -> str:
    # Use the shortest meaningful topic label to reduce search noise.
    terms = sorted((t for t in spec.terms if 1 < len(t) <= 32), key=len)
    return terms[0] if terms else spec.name


def build_query_plan(spec: TopicSpec) -> list[dict[str, Any]]:
    topic = _topic_anchor(spec).replace('"', "")
    plan: list[dict[str, Any]] = []

    for template in MUNICIPAL_SOURCE_QUERIES:
        plan.append({"query": template.format(topic=topic), "level": "municipal", "district": None, "street": None})

    # All 16 district-level pools are queried every day.  Query by the official government
    # name rather than hard-coding 16 portal domains, so portal/domain migrations do not break
    # coverage.  The relevance filter then strongly prefers *.gov.cn / Shanghai official hosts.
    for district in DISTRICTS:
        plan.append({
            "query": f'"上海市{district}人民政府" "{topic}"',
            "level": "district",
            "district": district,
            "street": None,
        })

    for template in LOCAL_MEDIA_QUERIES:
        plan.append({"query": template.format(topic=topic), "level": "local_media", "district": None, "street": None})

    for template in OFFICIAL_WECHAT_QUERIES:
        plan.append({"query": template.format(topic=topic), "level": "official_wechat_search", "district": None, "street": None})

    if spec.sanle:
        # 三乐是稀疏的街镇级信息源，因此扩大到最近一周，避免错过江宁路街道/居民区发布。
        for template in SANLE_SPECIAL_QUERIES:
            plan.append({
                "query": template.format(topic=topic),
                "level": "street",
                "district": "静安区",
                "street": "江宁路街道",
                "time_range": "week",
            })

    # Deduplicate and cap to a predictable daily load.
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in plan:
        q = item["query"].strip()
        if q and q not in seen:
            seen.add(q)
            out.append(item)
        if len(out) >= LOCAL_INTEL_MAX_QUERIES_PER_TOPIC:
            break
    return out


def _strip_html_fragment(value: str) -> str:
    value = html_lib.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _rss_published(value: str) -> str | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt.astimezone(TZ).isoformat()
    except Exception:
        return None


def _search_bing_rss(query: str) -> list[dict[str, Any]]:
    """Best-effort no-key fallback for Shanghai local discovery.

    SearXNG remains preferred when configured.  The RSS fallback prevents a
    missing/403/temporarily suspended SearXNG engine from cancelling the whole
    03:00 collector.  Web RSS is tried first because district/government pages
    are frequently not classified as news; News RSS supplements it.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 InternetBoard/4.11 ShanghaiLocalCollector",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    endpoints = (
        ("bing_web_rss", "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query, "format": "rss", "setlang": "zh-hans"})),
        ("bing_news_rss", "https://www.bing.com/news/search?" + urllib.parse.urlencode({"q": query, "format": "rss", "setlang": "zh-cn"})),
    )
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    errors: list[str] = []
    for engine_name, url in endpoints:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=LOCAL_INTEL_FETCH_TIMEOUT_SECONDS) as resp:
                raw = resp.read(1_500_000)
            root = ET.fromstring(raw)
            for item in root.findall(".//item"):
                title = _strip_html_fragment(item.findtext("title") or "")
                link = (item.findtext("link") or "").strip()
                desc = _strip_html_fragment(item.findtext("description") or "")
                pub = _rss_published(item.findtext("pubDate") or "")
                if not title or not link or link in seen:
                    continue
                seen.add(link)
                out.append({
                    "title": title,
                    "url": link,
                    "content": desc,
                    "snippet": desc,
                    "publishedDate": pub,
                    "engine": engine_name,
                    "engines": [engine_name],
                    "category": "web" if engine_name == "bing_web_rss" else "news",
                })
                if len(out) >= LOCAL_INTEL_RESULTS_PER_QUERY:
                    return out
        except Exception as exc:
            errors.append(f"{engine_name}:{type(exc).__name__}:{exc}")
    if not out and errors:
        raise RuntimeError("; ".join(errors)[:1000])
    return out[:LOCAL_INTEL_RESULTS_PER_QUERY]


def _search(query: str, time_range: str | None = None) -> list[dict[str, Any]]:
    # Prefer the user's SearXNG when available, but never make it a single point
    # of failure.  Previous deployments have seen upstream engines suspended by
    # 403/rate limits, so a no-key RSS fallback is part of the production path.
    if SEARXNG_URL:
        try:
            params = {
                "q": query,
                "format": "json",
                "language": "zh-CN",
                "safesearch": "0",
                "time_range": (time_range or LOCAL_INTEL_TIME_RANGE),
            }
            url = f"{SEARXNG_URL}/search?{urllib.parse.urlencode(params)}"
            req = urllib.request.Request(url, headers={"User-Agent": "InternetBoard/4.11 ShanghaiLocalCollector"})
            with urllib.request.urlopen(req, timeout=LOCAL_INTEL_FETCH_TIMEOUT_SECONDS) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            rows = [row for row in (data.get("results") or []) if isinstance(row, dict)]
            if rows:
                return rows[:LOCAL_INTEL_RESULTS_PER_QUERY]
            logger.warning("SearXNG returned no rows for %s; falling back to Bing RSS", query[:120])
        except Exception as exc:
            logger.warning("SearXNG search failed for %s: %s; falling back to Bing RSS", query[:120], exc)
    return _search_bing_rss(query)


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().strip(".")


def _is_excluded_host(host: str) -> bool:
    if host == "shanghai.gov.cn" or host.endswith(".shanghai.gov.cn"):
        return False
    return any(host == x or host.endswith("." + x) for x in EXCLUDED_HOSTS)


def _is_official_host(host: str) -> bool:
    if any(host == x or host.endswith("." + x) for x in OFFICIAL_HOST_HINTS):
        return True
    return host.endswith(".gov.cn") and host not in {"gov.cn", "www.gov.cn"}


def _district_in_text(text_value: str) -> str | None:
    for district in DISTRICTS:
        if district in text_value:
            return district
    return None


def _base_score(url: str, title: str, snippet: str, spec: TopicSpec, meta: dict[str, Any]) -> tuple[int, str, str | None, str | None, str]:
    host = _host(url)
    text_value = f"{title} {snippet} {meta.get('query','')}"
    if _is_excluded_host(host):
        return 0, "excluded", None, None, host

    district = meta.get("district") or _district_in_text(text_value)
    street = meta.get("street")
    level = str(meta.get("level") or "web")
    score = 0

    if host == "shanghai.gov.cn" or host.endswith(".shanghai.gov.cn"):
        score += 72 if level == "district" else 68
        level = "district_official" if district else "municipal_official"
    elif _is_official_host(host):
        score += 76 if district else 66
        level = "district_official" if district else "municipal_official"
    elif host == "mp.weixin.qq.com":
        score += 48
        level = "official_wechat_search"
    elif any(host == x or host.endswith("." + x) for x in LOCAL_MEDIA_HOSTS):
        score += 42
        level = "local_media"
    else:
        score += 18

    if "上海" in text_value:
        score += 12
    if district and district in text_value:
        score += 14
    if "政策" in text_value or "公示" in text_value or "公告" in text_value or "发布" in text_value:
        score += 6

    topic_hits = sum(1 for term in spec.terms if term and term.lower() in text_value.lower())
    score += min(18, topic_hits * 6)

    if spec.sanle:
        if any(term in text_value for term in ("三乐", "三乐里", "三乐小区")):
            score += 24
        if "江宁路街道" in text_value:
            score += 18
            district = "静安区"
            street = "江宁路街道"
            level = "street_official" if _is_official_host(host) else level
        if "In江宁" in text_value and host == "mp.weixin.qq.com":
            score += 22
            district = "静安区"
            street = "江宁路街道"
            level = "street_official_wechat"

    return min(100, score), level, district, street, host


def _fetch_text(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 InternetBoard/4.11",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=LOCAL_INTEL_FETCH_TIMEOUT_SECONDS) as resp:
            ctype = str(resp.headers.get("content-type") or "").lower()
            if "pdf" in ctype:
                return ""
            raw = resp.read(2_000_000).decode("utf-8", errors="replace")
    except Exception:
        return ""
    parser = _HTMLText()
    try:
        parser.feed(raw)
    except Exception:
        return ""
    text_value = re.sub(r"\s+", " ", " ".join(parser.parts)).strip()
    return text_value[:LOCAL_INTEL_MAX_CONTENT_CHARS]


def _score_with_content(base: int, content: str, spec: TopicSpec, district: str | None, street: str | None) -> int:
    if not content:
        return base
    score = base
    if "上海" in content:
        score += 4
    if district and district in content:
        score += 6
    if street and street in content:
        score += 8
    if spec.sanle and any(term in content for term in ("三乐里", "三乐小区", "三乐里居民区")):
        score += 12
    topic_hits = sum(1 for term in spec.terms if term and term.lower() in content.lower())
    score += min(10, topic_hits * 2)
    return min(100, score)


def _hash_row(url: str, title: str, content: str, snippet: str) -> str:
    base = (url.strip() + "\n" + title.strip() + "\n" + (content or snippet)[:3000]).encode("utf-8", errors="ignore")
    return hashlib.sha256(base).hexdigest()


def _parse_published(value: Any) -> datetime | None:
    if not value:
        return None
    raw = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def _insert_evidence(target: date, spec: TopicSpec, item: dict[str, Any]) -> bool:
    sql = text("""
    INSERT INTO local_source_evidence (
        collected_at, published_at, evidence_date, topic_id, topic_name, source_name, source_level,
        region, district, street, title, url, snippet, content, relevance_score, query_text,
        content_hash, metadata_json
    ) VALUES (
        :collected_at, :published_at, :evidence_date, :topic_id, :topic_name, :source_name, :source_level,
        '上海市', :district, :street, :title, :url, :snippet, :content, :score, :query_text,
        :content_hash, :metadata_json
    ) ON CONFLICT (content_hash) DO NOTHING
    RETURNING id
    """)
    params = {
        "collected_at": _now(),
        "published_at": item.get("published_at"),
        "evidence_date": target,
        "topic_id": spec.topic_id,
        "topic_name": spec.name,
        "source_name": item.get("source_name") or item.get("host") or "unknown",
        "source_level": item["source_level"],
        "district": item.get("district"),
        "street": item.get("street"),
        "title": item["title"][:1000],
        "url": item["url"][:3000],
        "snippet": (item.get("snippet") or "")[:5000],
        "content": (item.get("content") or "")[:LOCAL_INTEL_MAX_CONTENT_CHARS],
        "score": int(item["score"]),
        "query_text": (item.get("query") or "")[:2000],
        "content_hash": item["content_hash"],
        "metadata_json": _safe_json(item.get("metadata") or {}),
    }
    with engine.begin() as conn:
        row = conn.execute(sql, params).first()
    return row is not None


def collect_local_evidence(day: str | None = None) -> dict[str, Any]:
    target = date.fromisoformat(day) if day else _now().date()
    lock_key = f"ib:v411:collector:lock:{target.isoformat()}"
    lock_acquired = False
    try:
        lock_acquired = bool(_redis().set(lock_key, "1", nx=True, ex=4 * 3600))
    except Exception:
        lock_acquired = True  # Redis outage is reported elsewhere; do not silently skip collection.
    if not lock_acquired:
        return {"date": target.isoformat(), "skipped": True, "reason": "collector-already-running"}
    ensure_local_tables()
    topics = _load_topics()
    report: dict[str, Any] = {
        "date": target.isoformat(),
        "topics": len(topics),
        "queries": 0,
        "results": 0,
        "accepted": 0,
        "inserted": 0,
        "duplicates": 0,
        "errors": [],
        "districts": {},
        "sanle_special": {"enabled": any(t.sanle for t in topics), "accepted": 0},
    }

    for spec in topics:
        plan = build_query_plan(spec)
        for meta in plan:
            query = meta["query"]
            report["queries"] += 1
            try:
                results = _search(query, str(meta.get("time_range") or LOCAL_INTEL_TIME_RANGE))
            except Exception as exc:
                report["errors"].append(f"search {query[:80]}: {exc}")
                continue
            if LOCAL_INTEL_QUERY_DELAY_SECONDS:
                time.sleep(LOCAL_INTEL_QUERY_DELAY_SECONDS)
            for result in results:
                report["results"] += 1
                url = str(result.get("url") or "").strip()
                title = re.sub(r"\s+", " ", str(result.get("title") or "")).strip()
                snippet = re.sub(r"\s+", " ", str(result.get("content") or result.get("snippet") or "")).strip()
                if not url or not title:
                    continue
                score, source_level, district, street, host = _base_score(url, title, snippet, spec, meta)
                if score < max(40, LOCAL_INTEL_MIN_SCORE - 12):
                    continue
                content = _fetch_text(url)
                score = _score_with_content(score, content, spec, district, street)
                if score < LOCAL_INTEL_MIN_SCORE:
                    continue
                report["accepted"] += 1
                if district:
                    report["districts"][district] = int(report["districts"].get(district, 0)) + 1
                if spec.sanle and (street == "江宁路街道" or any(x in (title + snippet + content) for x in ("三乐", "三乐里"))):
                    report["sanle_special"]["accepted"] += 1
                evidence = {
                    "published_at": _parse_published(result.get("publishedDate") or result.get("published_at")),
                    "source_name": host,
                    "source_level": source_level,
                    "district": district,
                    "street": street,
                    "title": title,
                    "url": url,
                    "snippet": snippet,
                    "content": content,
                    "score": score,
                    "query": query,
                    "content_hash": _hash_row(url, title, content, snippet),
                    "metadata": {
                        "engine": result.get("engine"),
                        "engines": result.get("engines"),
                        "category": result.get("category"),
                        "collector": "v4.11-shanghai-local",
                    },
                }
                try:
                    if _insert_evidence(target, spec, evidence):
                        report["inserted"] += 1
                    else:
                        report["duplicates"] += 1
                except Exception as exc:
                    report["errors"].append(f"insert {url[:80]}: {exc}")

    try:
        r = _redis()
        r.set(f"ib:v411:collector:done:{target.isoformat()}", _safe_json(report), ex=7 * 86400)
        r.delete(lock_key)
    except Exception as exc:
        report["errors"].append(f"redis done marker: {exc}")
    return report


def local_coverage(day: str | None = None) -> dict[str, Any]:
    target = date.fromisoformat(day) if day else _now().date()
    ensure_local_tables()
    with engine.connect() as conn:
        total = int(conn.execute(text("SELECT COUNT(*) FROM local_source_evidence WHERE evidence_date=:d"), {"d": target}).scalar_one())
        by_level = {str(r[0]): int(r[1]) for r in conn.execute(text("SELECT source_level, COUNT(*) FROM local_source_evidence WHERE evidence_date=:d GROUP BY source_level ORDER BY COUNT(*) DESC"), {"d": target}).all()}
        by_district = {str(r[0]): int(r[1]) for r in conn.execute(text("SELECT district, COUNT(*) FROM local_source_evidence WHERE evidence_date=:d AND district IS NOT NULL GROUP BY district ORDER BY COUNT(*) DESC"), {"d": target}).all()}
        sanle = int(conn.execute(text("""
            SELECT COUNT(*) FROM local_source_evidence
            WHERE evidence_date=:d AND (topic_name ILIKE '%三乐%' OR topic_name ILIKE '%sanle%' OR street='江宁路街道' OR title ILIKE '%三乐%')
        """), {"d": target}).scalar_one())
    marker = None
    try:
        marker_raw = _redis().get(f"ib:v411:collector:done:{target.isoformat()}")
        marker = json.loads(marker_raw) if marker_raw else None
    except Exception:
        marker = None
    return {
        "date": target.isoformat(),
        "total": total,
        "by_level": by_level,
        "by_district": by_district,
        "sanle_jiangning_count": sanle,
        "collector": marker,
        "policy": {
            "mode": "shanghai-local-first",
            "municipal": True,
            "districts": list(DISTRICTS),
            "national_general_media_primary": False,
            "automatic_generic_national_research": False,
            "sanle_special": ["静安区", "江宁路街道", "三乐里居民区", "三乐小区", "In江宁"],
        },
    }


def _local_rows(target: date, limit: int = 180) -> list[dict[str, Any]]:
    ensure_local_tables()
    sql = text("""
        SELECT id, collected_at, published_at, topic_id, topic_name, source_name, source_level,
               region, district, street, title, url, snippet, content, relevance_score
        FROM local_source_evidence
        WHERE evidence_date=:d AND source_level <> 'local_daily_digest'
        ORDER BY relevance_score DESC, id DESC
        LIMIT :lim
    """)
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(sql, {"d": target, "lim": limit}).mappings().all()]


def execute_local_digest_job(job_id: str, day: str) -> dict[str, Any]:
    mark_ai_job_running(job_id)
    try:
        target = date.fromisoformat(day)
        rows = _local_rows(target)
        if not rows:
            result = {"date": day, "summary": "当天上海本地采集池暂无有效证据。", "evidence_count": 0}
            complete_ai_job(job_id, result)
            return result
        lines: list[str] = []
        for idx, row in enumerate(rows, 1):
            raw = {
                "topic": row.get("topic_name"), "level": row.get("source_level"),
                "district": row.get("district"), "street": row.get("street"),
                "title": row.get("title"), "url": row.get("url"),
                "snippet": row.get("snippet"), "content": (row.get("content") or "")[:1200],
                "score": row.get("relevance_score"),
            }
            lines.append(f"[L{idx}] {_safe_json(raw)}")
        context = "\n".join(lines)[:30000]
        from .intelligence import _ollama_chat
        system = (
            "你是上海本地情报分析员。只能依据提供的证据，禁止补造。全国泛资讯不是重点。"
            "优先判断上海市级政策、16区区级政策、街镇社区事项及其对专题的实际影响。"
            "针对三乐专题，要把静安区、江宁路街道、三乐里居民区/三乐小区作为同一地理链条分析。"
            "重要结论必须尽量引用[L编号]。"
        )
        prompt = (
            f"请分析 {day} 上海本地采集池。输出中文 Markdown：\n"
            "# 上海本地新增情报\n## 市级政策\n## 16区重要动态\n## 街镇/社区重要动态\n"
            "## 三乐专项（静安区→江宁路街道→三乐里）\n## 对现有专题的影响\n## 低价值/重复信息说明\n\n"
            f"证据：\n{context}"
        )
        summary, elapsed = _ollama_chat(system, prompt, temperature=0.1)
        digest_hash = hashlib.sha256(f"digest:{day}:{summary}".encode("utf-8")).hexdigest()
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO local_source_evidence (
                    collected_at, evidence_date, topic_name, source_name, source_level, region,
                    title, url, snippet, content, relevance_score, content_hash, metadata_json
                ) VALUES (
                    NOW(), :d, '上海本地综合', 'InternetBoard Qwen3.8', 'local_daily_digest', '上海市',
                    :title, :url, :snippet, :content, 100, :hash, :meta
                ) ON CONFLICT (content_hash) DO NOTHING
            """), {
                "d": target, "title": f"{day} 上海本地情报分析", "url": f"local://shanghai-digest/{day}",
                "snippet": summary[:1500], "content": summary, "hash": digest_hash,
                "meta": _safe_json({"model_job_id": job_id, "elapsed_seconds": elapsed, "evidence_count": len(rows)}),
            })
        result = {"date": day, "summary": summary, "elapsed_seconds": elapsed, "evidence_count": len(rows)}
        complete_ai_job(job_id, result)
        return result
    except Exception as exc:
        fail_ai_job(job_id, str(exc))
        raise


def _status_text(value: Any) -> str:
    raw = getattr(value, "value", value)
    raw = str(raw or "unknown")
    if "." in raw:
        raw = raw.rsplit(".", 1)[-1]
    return raw.strip().lower()


def _row_day(obj: Any) -> date | None:
    for name in ("created_at", "started_at", "queued_at", "updated_at"):
        value = getattr(obj, name, None)
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=TZ)
            return value.astimezone(TZ).date()
    return None


def daily_report_ready(day: str | None = None) -> tuple[bool, dict[str, Any]]:
    target = date.fromisoformat(day) if day else _now().date()
    now = _now()
    state: dict[str, Any] = {"date": target.isoformat()}
    if target == now.date() and (now.hour < 3 or (now.hour == 3 and now.minute < DAILY_REPORT_NOT_BEFORE_MINUTE)):
        return False, {**state, "reason": "not-before-window"}
    try:
        r = _redis()
        if not r.exists(f"ib:v411:collector:done:{target.isoformat()}"):
            return False, {**state, "reason": "collector-not-done"}
        if r.exists(f"ib:v411:daily-report-enqueued:{target.isoformat()}"):
            return False, {**state, "reason": "already-enqueued"}
    except Exception as exc:
        return False, {**state, "reason": f"redis:{exc}"}

    Run = getattr(db_models, "ResearchRun", None)
    today_runs = 0
    active_runs = 0
    if Run is not None:
        try:
            with session_scope() as session:
                rows = session.query(Run).order_by(Run.id.desc()).limit(3000).all()
                for obj in rows:
                    if _row_day(obj) != target:
                        continue
                    today_runs += 1
                    if _status_text(getattr(obj, "status", None)) in (RUNNING_NAMES | QUEUED_NAMES):
                        active_runs += 1
        except Exception as exc:
            return False, {**state, "reason": f"run-query:{exc}"}
    if active_runs:
        return False, {**state, "reason": "research-active", "today_runs": today_runs, "active_runs": active_runs}

    active_jobs = []
    for job in list_ai_jobs(200):
        status = str(job.get("status") or "").lower()
        payload = job.get("payload") or {}
        created = str(job.get("created_at") or "")[:10]
        job_day = str(payload.get("day") or created)[:10]
        if job_day != target.isoformat():
            continue
        kind = str(job.get("kind") or "")
        if kind.startswith("daily_summary") and status in {"queued", "running", "completed"}:
            return False, {**state, "reason": "daily-summary-job-exists", "job_id": job.get("job_id"), "status": status}
        if status in {"queued", "running"}:
            active_jobs.append(job.get("job_id"))
    if active_jobs:
        return False, {**state, "reason": "ai-jobs-active", "active_jobs": active_jobs[:10]}

    coverage = local_coverage(target.isoformat())
    if today_runs == 0 and int(coverage.get("total") or 0) == 0:
        return False, {**state, "reason": "no-daily-data"}
    summary_path = os.path.join(os.getenv("DATA_DIR", "/data"), "daily_summaries", f"{target.isoformat()}.json")
    if os.path.exists(summary_path):
        return False, {**state, "reason": "summary-exists"}
    return True, {**state, "reason": "ready", "today_runs": today_runs, "local_evidence": coverage.get("total", 0)}


def mark_daily_report_enqueued(day: str) -> None:
    _redis().set(f"ib:v411:daily-report-enqueued:{day}", "1", ex=7 * 86400)
