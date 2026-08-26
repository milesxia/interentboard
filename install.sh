#!/bin/sh
set -eu

TARGET="/share/Container/internetboard"
SOURCE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

fail() { echo "[FAIL] $*" >&2; exit 1; }
ok() { echo "[ OK ] $*"; }
info() { echo "[....] $*"; }

command -v docker >/dev/null 2>&1 || fail "Docker/Container Station command not found"
if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
else
  fail "Docker Compose command not found"
fi

ARCH=$(uname -m)
[ "$ARCH" = "x86_64" ] || [ "$ARCH" = "amd64" ] || fail "Unsupported architecture: $ARCH"
ok "Architecture: $ARCH"

MEM_KB=$(awk '/MemTotal/ {print $2}' /proc/meminfo)
[ "${MEM_KB:-0}" -ge 35000000 ] || fail "At least 35GB RAM is required for this production profile"
ok "RAM preflight passed"

mkdir -p "$TARGET"
FREE_KB=$(df -Pk /share/Container | awk 'NR==2 {print $4}')
[ "${FREE_KB:-0}" -ge 36700160 ] || fail "At least 35GB free space under /share/Container is required before first model pull"
ok "Storage preflight passed"

if [ "$SOURCE" != "$TARGET" ]; then
  info "Installing project files into $TARGET"
  OLD_ENV=""
  if [ -f "$TARGET/.env" ]; then
    OLD_ENV=$(mktemp)
    cp "$TARGET/.env" "$OLD_ENV"
  fi
  cp -a "$SOURCE/." "$TARGET/"
  if [ -n "$OLD_ENV" ]; then
    cp "$OLD_ENV" "$TARGET/.env"
    rm -f "$OLD_ENV"
  fi
fi

cd "$TARGET"
mkdir -p data/source data/chunk data/knowledge data/vector data/history data/conflict postgres redis ollama backups
chmod 700 postgres redis ollama backups || true
chmod 600 .env || true

if command -v nvidia-smi >/dev/null 2>&1; then
  DRIVER=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -n1 || true)
  [ -n "$DRIVER" ] && ok "Host NVIDIA driver detected: $DRIVER"
else
  info "Host nvidia-smi is not in PATH; GPU access will be verified inside the Ollama container"
fi

info "Validating Compose"
$COMPOSE config -q
ok "Compose syntax valid"

info "Pulling pinned base images"
$COMPOSE pull postgres redis ollama model-init >/dev/null

info "Building InternetBoard backend/frontend images"
$COMPOSE build --pull backend frontend

info "Starting data services and Ollama"
$COMPOSE up -d postgres redis ollama

info "Verifying NVIDIA GPU visibility inside Ollama"
GPU_OK=0
I=0
while [ "$I" -lt 60 ]; do
  if docker exec internetboard-ollama nvidia-smi >/dev/null 2>&1; then GPU_OK=1; break; fi
  I=$((I+1)); sleep 5
done
if [ "$GPU_OK" -ne 1 ]; then
  $COMPOSE logs --tail=120 ollama >&2 || true
  fail "Ollama container cannot access NVIDIA GPU. Confirm QNAP GPU is assigned to Container Station and NVIDIA/NvKernelDriver packages are installed."
fi
ok "NVIDIA GPU visible inside Ollama"

info "Starting pinned model initialization"
MODEL=$(awk -F= '/^OLLAMA_MODEL=/{print $2}' .env)
$COMPOSE up -d model-init
MODEL_OK=0
I=0
while [ "$I" -lt 480 ]; do
  if docker exec internetboard-ollama ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -Fx "$MODEL" >/dev/null 2>&1; then MODEL_OK=1; break; fi
  I=$((I+1)); sleep 30
done
if [ "$MODEL_OK" -ne 1 ]; then
  $COMPOSE logs --tail=120 model-init ollama >&2 || true
  fail "Model pull did not complete successfully"
fi
ok "Model available: $MODEL"

info "Starting InternetBoard application services"
$COMPOSE up -d backend worker scheduler frontend

info "Waiting for backend readiness"
BACKEND_OK=0
I=0
while [ "$I" -lt 180 ]; do
  STATUS=$(docker inspect -f '{{.State.Health.Status}}' internetboard-backend 2>/dev/null || true)
  if [ "$STATUS" = "healthy" ]; then BACKEND_OK=1; break; fi
  I=$((I+1)); sleep 5
done
if [ "$BACKEND_OK" -ne 1 ]; then
  $COMPOSE logs --tail=160 backend model-init ollama >&2 || true
  fail "Backend did not become healthy after model initialization"
fi
ok "Backend core dependencies are healthy"

info "Running end-to-end structured-output self-test"
docker exec -e TEST_MODEL="$MODEL" -i internetboard-backend python - <<'PY'
import json, os, httpx
payload = {
    "model": os.environ["TEST_MODEL"],
    "messages": [{"role":"user","content":"Return ok=true and name=InternetBoard."}],
    "stream": False,
    "think": False,
    "format": {
        "type":"object",
        "properties":{"ok":{"type":"boolean"},"name":{"type":"string"}},
        "required":["ok","name"]
    },
    "options":{"temperature":0,"num_ctx":2048,"num_predict":64}
}
r = httpx.post("http://ollama:11434/api/chat", json=payload, timeout=900)
r.raise_for_status()
content = r.json()["message"]["content"]
data = json.loads(content)
assert data.get("ok") is True, data
assert data.get("name") == "InternetBoard", data
print("structured-output: PASS")
PY
ok "Structured output self-test passed"

info "Verifying that Ollama actually offloaded work to the GPU"
SPLIT=$(docker exec internetboard-ollama ollama ps 2>/dev/null || true)
echo "$SPLIT"
echo "$SPLIT" | grep -qi 'GPU' || fail "The model is running CPU-only. InternetBoard requires partial NVIDIA GPU offload on this production profile."
ok "Ollama reports GPU participation"

info "Checking frontend/backend health"
PORT=$(awk -F= '/^WEB_PORT=/{print $2}' .env)
HEALTH_OK=0
I=0
while [ "$I" -lt 60 ]; do
  if curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then HEALTH_OK=1; break; fi
  I=$((I+1)); sleep 5
done
[ "$HEALTH_OK" -eq 1 ] || fail "Web health endpoint did not become ready"
ok "Web/API health check passed"

APIKEY=$(awk -F= '/^INTERNETBOARD_API_KEY=/{print $2}' .env)
echo
echo "InternetBoard v1.0 Production is ready."
echo "Web: http://<NAS-IP>:${PORT}"
echo "API Key: ${APIKEY}"
echo "Project: ${TARGET}"
