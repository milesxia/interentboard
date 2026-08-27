#!/usr/bin/env python3
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
errors = []

def require_text(rel: str, *needles: str) -> str:
    p = ROOT / rel
    if not p.exists():
        errors.append(f"missing {rel}")
        return ""
    text = p.read_text(encoding="utf-8")
    for needle in needles:
        if needle not in text:
            errors.append(f"{rel}: missing {needle!r}")
    return text

# The original V4.7 manual topic refresh UI is intentionally preserved in
# /classic.html after V4.13 moved the primary UI to the dark command center.
appjs = require_text("frontend/app.js", "runTopic", "/api/topics/${id}/run")
post_forms = (
    "method:'POST'",
    'method:"POST"',
    "method: 'POST'",
    'method: "POST"',
)
if not any(form in appjs for form in post_forms):
    errors.append("frontend/app.js: run API POST method contract not found")

require_text(
    "backend/app/main.py",
    "/api/topics/{topic_id}/run",
    'enqueue_run(run_id, reason="manual refresh")',
    "/api/build",
    "manual-refresh request received",
)
require_text(
    "backend/app/tasks.py",
    "def enqueue_run(",
    "apply_async",
    "internetboard.run_research",
    "enqueue accepted run_id=",
)
require_text(
    "frontend/nginx.conf",
    "proxy_pass http://backend:8000",
    "INTERNETBOARD V4.7 NO-CACHE",
    "no-store, no-cache",
)

# V4.13 migration contract:
# - primary / is the dark command center and must use command-center.js
# - legacy topic-management/manual-refresh entry remains in /classic.html
# - task-overlay stays on the dark command center for live queue status
require_text(
    "frontend/index.html",
    "V4.13 DARK COMMAND CENTER",
    "/command-center.js?v=4.13-dark-command-center",
    "/task-overlay.js",
    "运行看板",
)
require_text(
    "frontend/classic.html",
    "app.js?v=4.7-refresh-chain",
    '<script src="/build.js?v=4.7-refresh-chain"></script>',
)
require_text(
    "frontend/command-center.js",
    "/api/intelligence/local/collect",
    "method:'POST'",
    "cache:'no-store'",
)
require_text(
    "frontend/Dockerfile",
    "INTERNETBOARD V4.7 FRONTEND BUILD FILES",
    "COPY build.js /usr/share/nginx/html/build.js",
    "COPY build.json /usr/share/nginx/html/build.json",
    "COPY classic.html /usr/share/nginx/html/classic.html",
    "COPY command-center.js /usr/share/nginx/html/command-center.js",
)
require_text("backend/Dockerfile", "INTERNETBOARD_BUILD_SHA", "BUILD_SHA")

compose = require_text("docker-compose.yml")
for image in (
    "milesxia/internetboard-backend:latest",
    "milesxia/internetboard-worker:latest",
    "milesxia/internetboard-scheduler:latest",
    "milesxia/internetboard-frontend:latest",
):
    if image not in compose:
        errors.append(f"docker-compose.yml: missing {image}")

if errors:
    print("REFRESH CHAIN VALIDATION FAILED")
    for err in errors:
        print(" -", err)
    sys.exit(1)

print("REFRESH CHAIN VALIDATION PASSED")
print("dark command center + classic manual topic refresh -> backend run route -> enqueue_run -> apply_async -> run_research contract present")
