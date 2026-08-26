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
must("run: |" in workflow and "backend runtime imports PASS" in workflow, "CI import command must use a YAML block scalar")
must("working-directory: backend" in workflow, "CI backend imports must run from backend/ to avoid the legacy root app package shadowing production app")
must("wrong app package" in workflow and "backend app package:" in workflow, "CI must assert that imports resolve to backend/app")
must("actions/checkout@v7" in workflow and "actions/setup-python@v7" in workflow, "CI action majors are not current")
print("InternetBoard V4.1 CI/frontend invariants: PASS")


# INTERNETBOARD_V4_3_RESTART_RECOVERY_CHECKS
from pathlib import Path as _V43Path
_runtime_v43 = _V43Path("backend/app/runtime.py").read_text(encoding="utf-8")
_tasks_v43 = _V43Path("backend/app/tasks.py").read_text(encoding="utf-8")
_compose_v43 = _V43Path("docker-compose.yml").read_text(encoding="utf-8")
assert "def clear_transient_runtime_state" in _runtime_v43, "missing worker restart transient-state reset"
assert "clear_transient_runtime_state()" in _tasks_v43, "worker_ready must reset old runtime metadata"
assert "Worker startup recovery result" in _tasks_v43, "worker startup recovery must be observable"
assert '"--save", ""' in _compose_v43, "Redis periodic RDB snapshots must be disabled when AOF is authoritative"
assert 'ollama show "$${OLLAMA_MODEL}"' in _compose_v43, "model-init must use cached Ollama model before network pull"
print("InternetBoard V4.3 restart-recovery invariants: PASS")


# INTERNETBOARD_V4_4_MEMORY_RESIDENCY_CHECKS
from pathlib import Path as _V44Path
_cfg_v44 = _V44Path("backend/app/config.py").read_text(encoding="utf-8")
_ollama_v44 = _V44Path("backend/app/ollama_client.py").read_text(encoding="utf-8")
_search_v44 = _V44Path("backend/app/search.py").read_text(encoding="utf-8")
_main_v44 = _V44Path("backend/app/main.py").read_text(encoding="utf-8")
_front_v44 = _V44Path("frontend/app.js").read_text(encoding="utf-8")
_compose_v44 = _V44Path("docker-compose.yml").read_text(encoding="utf-8")
_env_v44 = _V44Path(".env.example").read_text(encoding="utf-8")
assert "ollama_use_mlock" in _cfg_v44 and "ollama_pin_model" in _cfg_v44, "RAM residency settings missing"
assert '"use_mlock": settings.ollama_use_mlock' in _ollama_v44, "Ollama mlock option missing"
assert '"use_mmap": settings.ollama_use_mmap' in _ollama_v44, "Ollama mmap option missing"
assert 'keep_alive": -1 if settings.ollama_pin_model' in _ollama_v44, "Ollama model pinning missing"
assert "def resource_summary" in _ollama_v44 and "model_runtime" in _main_v44, "Ollama memory split status missing"
assert "feedparser.loads" not in _search_v44 and "feedparser.parse" in _search_v44, "Bing RSS parser regression"
assert "IPC_LOCK" in _compose_v44 and "memlock:" in _compose_v44, "Ollama container memlock capability missing"
assert "OLLAMA_USE_MLOCK" in _compose_v44 and "OLLAMA_PIN_MODEL" in _env_v44, "Ollama residency env controls missing"
assert "模型CPU/RAM侧" in _front_v44 and "模型显存" in _front_v44, "Frontend model memory split missing"
print("InternetBoard V4.4 memory-residency invariants: PASS")
