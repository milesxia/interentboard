from pathlib import Path


def must(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")

compose = text("docker-compose.yml")
main = text("backend/app/main.py")
dockerfile = text("backend/Dockerfile")
frontend = text("frontend/app.js")
index = text("frontend/index.html")
workflow = text(".github/workflows/dockerhub.yml")
topics = text("config/topics.yml")
visual = text("backend/app/visual.py")
ollama = text("backend/app/ollama_client.py")
pipeline = text("backend/app/pipeline.py")
requirements = text("backend/requirements.txt")
runtime = text("backend/app/runtime.py")
tasks = text("backend/app/tasks.py")

must("build:" not in compose, "Production Compose must not build on the NAS")
for image in (
    "milesxia/internetboard-backend:latest",
    "milesxia/internetboard-worker:latest",
    "milesxia/internetboard-scheduler:latest",
    "milesxia/internetboard-frontend:latest",
    "ollama/ollama:latest",
):
    must(image in compose, f"Missing production image: {image}")

runtime_text = "\n".join((main, frontend, index, compose, text(".env.example")))
must("INTERNETBOARD_API_KEY" not in runtime_text, "API-key UI/runtime dependency must be removed")
must("X-API-Key" not in runtime_text, "API-key header must be removed")
must("bootstrap_defaults_once" in main, "One-time bootstrap is not wired into startup")
must("/api/export/handoff" in main, "Handoff export endpoint is missing")
must("exportBtn" in index and "exportBtn" in frontend, "Handoff export UI is missing")
must("COPY config /app/config" in dockerfile, "Backend image does not contain config defaults")
must("COPY seed /app/seed" in dockerfile, "Backend image does not contain seed assets")
must("context: ." in workflow and "file: ./backend/Dockerfile" in workflow, "Backend CI build context is not repository root")
must(topics.count("- slug:") >= 5, "Expected built-in topic definitions are missing")
must("extract_visual_assets(document)" in pipeline, "Visual pipeline is not wired into fetched evidence")
must("def analyze_visual(" in ollama and "\"images\"" in ollama, "Ollama vision request support is missing")
must("VISUAL_ENABLED" in compose and "VISUAL_ENABLED" in text(".env.example"), "Visual runtime settings are missing")
must("Pillow" in requirements, "Pillow is required for bounded image normalization")
must("VisualAsset" in visual and "visual_max_assets_per_run" in text("backend/app/config.py"), "Visual evidence limits are missing")
must("RunLease" in runtime and "reserve_run_queue" in runtime, "Redis run lease/queue markers are missing")
must("runtime_watchdog" in tasks and "ensure_run_enqueued" in tasks, "Task self-healing watchdog is missing")
must("worker_cancel_long_running_tasks_on_connection_loss" in tasks, "Celery broker-loss recovery setting is missing")
must("run_runtime_state" in main and "/api/runs/{run_id}/recover" in main, "Runtime status/recovery API is missing")
must("recoverRun" in frontend and "僵尸任务" in frontend, "Runtime recovery UI is missing")
must("inspect ping" in compose, "Worker Docker healthcheck is missing")
must("RUN_HEARTBEAT_INTERVAL_SECONDS" in compose, "Runtime heartbeat Compose settings are missing")
print("InternetBoard production invariants: PASS")


# V4.1 CI/frontend invariants
must(frontend.count("const rt = state.system?.runtime || {};") == 1, "Frontend runtime state must be declared exactly once in renderStats")
must("node --check frontend/app.js" in workflow, "CI must syntax-check frontend JavaScript")
must("DATA_DIR: /tmp/internetboard-ci-data" in workflow, "CI import check must use a writable data directory")
must("run: |" in workflow and "runtime imports PASS" in workflow, "CI import command must use a YAML block scalar")
must("actions/checkout@v7" in workflow and "actions/setup-python@v7" in workflow, "CI action majors are not current")
print("InternetBoard V4.1 CI/frontend invariants: PASS")
