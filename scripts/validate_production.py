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
config = text("backend/app/config.py")
env = text(".env.example")
diag = text("scripts/qnap-ollama-runtime-check.sh")

# Production/release invariants.
must("build:" not in compose, "Production Compose must not build on the NAS")
for image in (
    "milesxia/internetboard-backend:latest",
    "milesxia/internetboard-worker:latest",
    "milesxia/internetboard-scheduler:latest",
    "milesxia/internetboard-frontend:latest",
    "ollama/ollama:latest",
):
    must(image in compose, f"Missing production image: {image}")

runtime_text = "\n".join((main, frontend, index, compose, env))
must("INTERNETBOARD_API_KEY" not in runtime_text, "API-key UI/runtime dependency must be removed")
must("X-API-Key" not in runtime_text, "API-key header must be removed")
must("bootstrap_defaults_once" in main, "One-time bootstrap is not wired into startup")
must("/api/export/handoff" in main, "Handoff export endpoint is missing")
# INTERNETBOARD V4.13.2 HANDOFF MIGRATION
# Legacy management functions moved from / to /classic.html in V4.13.
# Validate the handoff control across both UI surfaces without loading legacy app.js on the dark home.
_handoff_surface_v4132 = (
    open("frontend/index.html", encoding="utf-8").read()
    + "\n"
    + open("frontend/classic.html", encoding="utf-8").read()
)
must("exportBtn" in _handoff_surface_v4132 and "exportBtn" in frontend, "Handoff export UI is missing")
must("COPY config /app/config" in dockerfile, "Backend image does not contain config defaults")
must("COPY seed /app/seed" in dockerfile, "Backend image does not contain seed assets")
must("context: ." in workflow and "file: ./backend/Dockerfile" in workflow, "Backend CI build context is not repository root")
must(topics.count("- slug:") >= 5, "Expected built-in topic definitions are missing")

# Visual/search pipeline invariants.
must("extract_visual_assets(document)" in pipeline, "Visual pipeline is not wired into fetched evidence")
must("def analyze_visual(" in ollama and '"images"' in ollama, "Ollama vision request support is missing")
must("VISUAL_ENABLED" in compose and "VISUAL_ENABLED" in env, "Visual runtime settings are missing")
must("Pillow" in requirements, "Pillow is required for bounded image normalization")
must("VisualAsset" in visual and "visual_max_assets_per_run" in config, "Visual evidence limits are missing")
must("feedparser.loads" not in text("backend/app/search.py") and "feedparser.parse" in text("backend/app/search.py"), "Bing RSS parser regression")

# Runtime self-healing invariants.
must("RunLease" in runtime and "reserve_run_queue" in runtime, "Redis run lease/queue markers are missing")
must("runtime_watchdog" in tasks and "ensure_run_enqueued" in tasks, "Task self-healing watchdog is missing")
must("clear_transient_runtime_state" in runtime and "clear_transient_runtime_state()" in tasks, "Worker restart recovery is missing")
must("worker_cancel_long_running_tasks_on_connection_loss" in tasks, "Celery broker-loss recovery is missing")
must("run_runtime_state" in main and "/api/runs/{run_id}/recover" in main, "Runtime status/recovery API is missing")
must("recoverRun" in frontend and "僵尸任务" in frontend, "Runtime recovery UI is missing")
must('"--save", ""' in compose, "Redis periodic RDB snapshots must remain disabled")
must('ollama show "$${OLLAMA_MODEL}"' in compose, "model-init must use cached model before pull")

# CI invariants.
must(frontend.count("const rt = state.system?.runtime || {};") == 1, "Frontend runtime state must be declared exactly once")
must("node --check frontend/app.js" in workflow, "CI must syntax-check frontend JavaScript")
must("DATA_DIR: /tmp/internetboard-ci-data" in workflow, "CI import check must use writable data directory")
must("working-directory: backend" in workflow, "CI backend imports must run from backend/")
must("wrong app package" in workflow and "backend app package:" in workflow, "CI must assert backend/app import resolution")
must("actions/checkout@v7" in workflow and "actions/setup-python@v7" in workflow, "CI action majors are not current")

# FINAL performance policy: GPU-first + CUDA Unified Memory + RAM spillover.
for literal in (
    'GGML_CUDA_ENABLE_UNIFIED_MEMORY: "1"',
    'LLAMA_ARG_LOAD_MODE: "mmap"',
    'LLAMA_ARG_N_GPU_LAYERS: "all"',
    'LLAMA_ARG_FIT: "off"',
    'LLAMA_ARG_FIT_TARGET: "0"',
    'LLAMA_ARG_MMPROJ_OFFLOAD: "true"',
    'LLAMA_ARG_OP_OFFLOAD: "true"',
    'OLLAMA_GPU_OVERHEAD: "0"',
    'OLLAMA_KEEP_ALIVE: "-1"',
    'OLLAMA_CONTEXT_LENGTH: "8192"',
):
    must(literal in compose, f"Missing final Ollama performance setting: {literal}")

must("${LLAMA_ARG_" not in compose, "QNAP performance settings must be literal, not Compose default expressions")
must("${GGML_CUDA_ENABLE_UNIFIED_MEMORY" not in compose, "QNAP UVM setting must be literal")
must("OLLAMA_NUM_PARALLEL:" not in compose, "Do not impose an Ollama parallel override; qwen35 scheduler decides supported concurrency")
must("OLLAMA_MAX_QUEUE:" not in compose, "Do not impose a reduced Ollama queue limit")
must('LLAMA_ARG_LOAD_MODE=mmap' in env, "env example load mode mismatch")
must('LLAMA_ARG_N_GPU_LAYERS=all' in env, "env example GPU layer policy mismatch")
must('LLAMA_ARG_FIT=off' in env and 'LLAMA_ARG_FIT_TARGET=0' in env, "env example fit policy mismatch")
must('LLAMA_ARG_MMPROJ_OFFLOAD=true' in env, "env example projector policy mismatch")
must('GGML_CUDA_ENABLE_UNIFIED_MEMORY=1' in env, "env example UVM policy missing")
must('llama_arg_load_mode: str = "mmap"' in config, "backend load-mode default mismatch")
must("llama_arg_fit_target: int = 0" in config, "backend fit-target default mismatch")
must("llama_arg_mmproj_offload: bool = True" in config, "backend projector default mismatch")

# No artificial compute/container caps added by this release.
pass  # V4.10.2: legacy anti-serial rule superseded by canonical serial queue contract
must("--max-tasks-per-child" not in compose, "Do not recycle workers on an artificial task-count cap")
must("--without-mingle" in compose and "--without-gossip" in compose and "--without-heartbeat" in compose, "Single-node Celery restart hardening missing")
must("task_ignore_result=True" in tasks, "Celery task results must not add unnecessary Redis traffic")
must("cpus:" not in compose and "mem_limit:" not in compose and "memory:" not in compose, "Container CPU/RAM resource limits are not allowed")
must("limits:" not in compose, "Compose resource limits are not allowed")
must("VmRSS" in diag and "GGML_CUDA_ENABLE_UNIFIED_MEMORY" in diag and "LLAMA_ARG_N_GPU_LAYERS" in diag, "QNAP UVM runtime diagnostic missing")

print("InternetBoard FINAL GPU-first UVM production invariants: PASS")

# BEGIN INTERNETBOARD V4.10.2 SERIAL PRODUCTION CONTRACT
# Heavy local-AI work shares one Celery research queue consumed by one process.
# Control/watchdog work remains independent. Ollama/qwen35 scheduling is not overridden.
import yaml as _v4102_yaml
from pathlib import Path as _V4102Path
_v4102_compose = _v4102_yaml.safe_load(_V4102Path("docker-compose.yml").read_text(encoding="utf-8"))
_v4102_services = _v4102_compose["services"]
_v4102_worker = " ".join(str(x) for x in _v4102_services["worker"]["command"])
assert "--concurrency=1" in _v4102_worker, "V4.10.2 research worker must be serial"
assert "--queues=research" in _v4102_worker, "V4.10.2 research worker must consume research queue"
assert "--prefetch-multiplier=1" in _v4102_worker, "V4.10.2 research worker prefetch must remain one"
assert "monitor" in _v4102_services, "V4.10.2 monitor service missing"
_v4102_monitor = " ".join(str(x) for x in _v4102_services["monitor"]["command"])
assert "--concurrency=1" in _v4102_monitor, "V4.10.2 monitor worker must be single-process"
assert "--queues=control" in _v4102_monitor, "V4.10.2 monitor worker must consume control queue"
assert _v4102_services["worker"].get("networks") == _v4102_services["monitor"].get("networks"), "V4.10.2 monitor networks must match research worker"
assert _v4102_services["worker"].get("network_mode") == _v4102_services["monitor"].get("network_mode"), "V4.10.2 monitor network_mode must match research worker"
_v4102_ollama_env = _v4102_services["ollama"].get("environment") or {}
assert "OLLAMA_NUM_PARALLEL" not in _v4102_ollama_env, "Ollama scheduler parallelism must not be overridden"
assert "OLLAMA_MAX_QUEUE" not in _v4102_ollama_env, "Ollama scheduler queue limit must not be overridden"
assert "OLLAMA_MAX_LOADED_MODELS" not in _v4102_ollama_env, "Ollama loaded-model scheduler must not be overridden"
print("InternetBoard V4.10.2 serial production contract: PASS")
# END INTERNETBOARD V4.10.2 SERIAL PRODUCTION CONTRACT

# BEGIN INTERNETBOARD V4.12 RELEVANT EVIDENCE PRODUCTION CONTRACT
import yaml as _v412_yaml
from pathlib import Path as _V412Path
_v412_compose = _v412_yaml.safe_load(_V412Path("docker-compose.yml").read_text(encoding="utf-8"))
_v412_services = _v412_compose["services"]
assert "collector" in _v412_services, "V4.12 Shanghai collector service missing"
_v412_collector = " ".join(str(x) for x in _v412_services["collector"]["command"])
assert "--queues=collect" in _v412_collector and "--concurrency=1" in _v412_collector, "V4.12 collector queue contract broken"
_v412_worker = " ".join(str(x) for x in _v412_services["worker"]["command"])
assert "--queues=research" in _v412_worker and "--concurrency=1" in _v412_worker, "V4.12 research AI must remain serial"
_v412_monitor = " ".join(str(x) for x in _v412_services["monitor"]["command"])
assert "--queues=control" in _v412_monitor, "V4.12 monitor must remain independent"
assert _v412_services["collector"].get("networks") == _v412_services["worker"].get("networks"), "V4.12 collector networks must match worker"
assert _v412_services["collector"].get("network_mode") == _v412_services["worker"].get("network_mode"), "V4.12 collector network_mode must match worker"
_v412_ollama = _v412_services["ollama"].get("environment") or {}
for _v412_key in ("OLLAMA_NUM_PARALLEL", "OLLAMA_MAX_QUEUE", "OLLAMA_MAX_LOADED_MODELS"):
    assert _v412_key not in _v412_ollama, f"V4.12 must not override Ollama scheduler: {_v412_key}"
_v412_local = _V412Path("backend/app/shanghai_intel.py").read_text(encoding="utf-8")
assert "江宁路街道" in _v412_local and "三乐里居民区" in _v412_local and "In江宁" in _v412_local, "V4.12 Sanle/Jiangning local policy missing"
assert "local_source_evidence" in _v412_local and "EXCLUDED_HOSTS" in _v412_local, "V4.12 Shanghai local evidence/filter layer missing"
assert "_filter_relevant_content" in _v412_local and "raw_content" in _v412_local, "V4.12 article-internal evidence filter missing"
assert "_cluster_local_events" in _v412_local and "supplementary_sources" in _v412_local, "V4.12 same-event merge missing"
print("InternetBoard V4.12 Shanghai-local production contract: PASS")
# END INTERNETBOARD V4.12 RELEVANT EVIDENCE PRODUCTION CONTRACT
