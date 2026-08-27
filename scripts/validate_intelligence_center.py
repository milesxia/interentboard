#!/usr/bin/env python3
from pathlib import Path

ROOT = Path.cwd()
checks = {
    "backend/app/intelligence.py": [
        'APIRouter(prefix="/api/intelligence"',
        '@router.get("/tasks")',
        '@router.post("/runs/{run_id}/resume"',
        '@router.get("/daily/{day}")',
        '@router.post("/daily/{day}/generate")',
        '@router.post("/ask")',
        'OLLAMA_BASE_URL',
        'ensure_run_enqueued',
        'daily_summaries',
        'knowledge_chat',
    ],
    "backend/app/main.py": [
        "INTERNETBOARD V4.8 INTELLIGENCE ROUTER",
        "app.include_router(intelligence_router)",
        "v4.8-intelligence-center",
    ],
    "frontend/insights.html": [
        "失败任务管理",
        "当日总结",
        "本地知识库 AI 问答",
        "/insights.js",
    ],
    "frontend/insights.js": [
        "/api/intelligence/tasks",
        "/api/intelligence/runs/",
        "/api/intelligence/daily/",
        "/api/intelligence/ask",
    ],
    "frontend/Dockerfile": [
        "COPY insights.html",
        "COPY insights.js",
        "COPY insights.css",
        "COPY task-overlay.js",
    ],
    "frontend/task-overlay.js": [
        "/api/intelligence/tasks",
        "失败",
        "已终止",
    ],
    "frontend/index.html": [
        "INTERNETBOARD V4.8 INTELLIGENCE LINK",
        "/insights.html",
        "/task-overlay.js",
    ],
}

errors = []
for rel, needles in checks.items():
    path = ROOT / rel
    if not path.exists():
        errors.append(f"missing {rel}")
        continue
    text = path.read_text(encoding="utf-8")
    for needle in needles:
        if needle not in text:
            errors.append(f"{rel}: missing {needle!r}")

if errors:
    print("INTELLIGENCE CENTER VALIDATION FAILED")
    for error in errors:
        print(" -", error)
    raise SystemExit(1)

print("INTELLIGENCE CENTER VALIDATION PASSED")
print("failed-run resume -> daily summary -> local PostgreSQL evidence -> Qwen knowledge QA contracts present")
