#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import re
import yaml

ROOT = Path.cwd()

def text(rel: str) -> str:
    p = ROOT / rel
    if not p.exists():
        raise SystemExit(f"V4.12 VALIDATION FAILED: missing {rel}")
    return p.read_text(encoding="utf-8")

def must(cond: bool, msg: str) -> None:
    if not cond:
        raise SystemExit(f"V4.12 VALIDATION FAILED: {msg}")

local = text("backend/app/shanghai_intel.py")
tasks = text("backend/app/tasks.py")
intel = text("backend/app/intelligence.py")
main = text("backend/app/main.py")
html = text("frontend/insights.html")
js = text("frontend/insights.js")
compose = yaml.safe_load(text("docker-compose.yml")) or {}
services = compose.get("services") or {}

# 16 districts, municipal/district/street hierarchy and Sanle/Jiangning special chain.
districts = ["浦东新区","黄浦区","静安区","徐汇区","长宁区","普陀区","虹口区","杨浦区","宝山区","闵行区","嘉定区","金山区","松江区","青浦区","奉贤区","崇明区"]
for district in districts:
    must(district in local, f"missing Shanghai district: {district}")
for token in ("江宁路街道", "三乐里居民区", "三乐小区", "In江宁", "上海发布"):
    must(token in local, f"missing local source token: {token}")
for host in ("xinhuanet.com", "news.cn", "cctv.com", "people.com.cn", "www.gov.cn"):
    must(host in local, f"national/general exclusion missing: {host}")
must("local_source_evidence" in local, "local evidence table missing")
must("LOCAL_INTEL_MIN_SCORE" in local and "relevance_score" in local, "local relevance prefilter missing")
must("_filter_relevant_content" in local and "LOCAL_INTEL_RELEVANT_MAX_CHARS" in local, "article-internal relevant evidence filter missing")
must("raw_content" in local and "compression_ratio" in local, "raw evidence audit / compression metadata missing")
must("_cluster_local_events" in local and "LOCAL_INTEL_EVENT_SIMILARITY" in local, "same-event merge before AI missing")
must("supplementary_sources" in local, "same-event supplementary source payload missing")
must("site:shanghai.gov.cn" in local, "Shanghai official source query pool missing")
must("site:jingan.gov.cn" in local, "Jing'an/Jiangning official source query missing")
must("https://www.bing.com/search?" in local and "https://www.bing.com/news/search?" in local, "Bing RSS fallback missing")
must("SearXNG search failed" in local and "falling back to Bing RSS" in local, "SearXNG failover contract missing")

# Daily automatic flow: generic national run_all_topics schedule is retired; local sweep begins at 03:00.
for token in (
    "internetboard.local_source_sweep_v411",
    "internetboard.local_intel_digest_v411",
    "internetboard.daily_report_finalize_v411",
    "internetboard.run_all_topics",
    "_v412_crontab(hour=3, minute=0)",
    'if (v or {}).get("task") != "internetboard.run_all_topics"',
):
    must(token in tasks, f"tasks contract missing: {token}")
must('"internetboard.local_source_sweep_v411": {"queue": "collect"}' in tasks, "local collector route missing")
must('"internetboard.local_intel_digest_v411": {"queue": "research"}' in tasks, "local digest must use serial research queue")
must('"internetboard.daily_report_finalize_v411": {"queue": "control"}' in tasks, "daily finalizer must use control queue")
must("daily_report_ready" in tasks and "mark_daily_report_enqueued" in tasks, "automatic final report gate missing")
must("create_ai_job" in tasks and "set_ai_job_task_id" in tasks, "AI job tracking missing")

# Compose workers: AI serial remains one; collection gets independent non-AI queue; monitor remains independent.
for svc in ("worker", "monitor", "collector"):
    must(svc in services, f"compose service missing: {svc}")
worker = " ".join(str(x) for x in services["worker"].get("command") or [])
monitor = " ".join(str(x) for x in services["monitor"].get("command") or [])
collector = " ".join(str(x) for x in services["collector"].get("command") or [])
must("--concurrency=1" in worker and "--queues=research" in worker, "research worker not serial")
must("--concurrency=1" in monitor and "--queues=control" in monitor, "monitor queue contract broken")
must("--concurrency=1" in collector and "--queues=collect" in collector, "collector queue contract broken")
must(services["worker"].get("networks") == services["collector"].get("networks"), "collector networks must match worker")
must(services["worker"].get("network_mode") == services["collector"].get("network_mode"), "collector network_mode must match worker")
ollama_env = services.get("ollama", {}).get("environment") or {}
for key in ("OLLAMA_NUM_PARALLEL", "OLLAMA_MAX_QUEUE", "OLLAMA_MAX_LOADED_MODELS"):
    must(key not in ollama_env, f"Ollama scheduler override forbidden: {key}")

# Local API / report / UI.
for token in ("/local/coverage", "/local/collect", "上海每日情报报告", "江宁路街道", "三乐里居民区"):
    must(token in intel, f"intelligence local contract missing: {token}")
must('"release": "v4.12-relevant-evidence-pipeline"' in main, "build release not V4.12")
for token in ("上海本地采集", "localTotal", "localDistricts", "localSanle", "loadLocalCoverage"):
    must(token in html + js, f"frontend local coverage missing: {token}")

print("V4.12 SHANGHAI LOCAL INTELLIGENCE VALIDATION PASSED")
print("Shanghai municipal -> 16 districts -> street/community collector; article-internal relevant evidence extraction -> same-event merge -> serial AI; Sanle -> Jing'an -> Jiangning Road -> Sanleli")
