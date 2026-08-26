#!/bin/sh
set -eu

TARGET="/share/Container/internetboard"
SOURCE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
fail() { echo "[FAIL] $*" >&2; exit 1; }
ok() { echo "[ OK ] $*"; }
info() { echo "[....] $*"; }

command -v docker >/dev/null 2>&1 || fail "Docker/Container Station command not found"
if docker compose version >/dev/null 2>&1; then COMPOSE="docker compose"; elif command -v docker-compose >/dev/null 2>&1; then COMPOSE="docker-compose"; else fail "Docker Compose command not found"; fi
ARCH=$(uname -m)
[ "$ARCH" = "x86_64" ] || [ "$ARCH" = "amd64" ] || fail "Unsupported architecture: $ARCH"
MEM_KB=$(awk '/MemTotal/ {print $2}' /proc/meminfo)
[ "${MEM_KB:-0}" -ge 35000000 ] || fail "At least 35GB RAM is required for this production profile"

mkdir -p "$TARGET"
if [ "$SOURCE" != "$TARGET" ]; then
  OLD_ENV=""
  if [ -f "$TARGET/.env" ]; then OLD_ENV=$(mktemp); cp "$TARGET/.env" "$OLD_ENV"; fi
  cp -a "$SOURCE/." "$TARGET/"
  if [ -n "$OLD_ENV" ]; then cp "$OLD_ENV" "$TARGET/.env"; rm -f "$OLD_ENV"; fi
fi
cd "$TARGET"
mkdir -p data/source data/chunk data/knowledge data/vector data/history data/conflict data/exports data/.bootstrap postgres redis ollama backups

if [ ! -f .env ]; then
  if [ -f "$TARGET/postgres/PG_VERSION" ]; then
    fail "Existing PostgreSQL data detected but .env is missing. Create .env with the existing database password before using install.sh."
  fi
  cp .env.example .env
  DBPASS=$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')
  sed -i "s/REPLACE_WITH_RANDOM_PASSWORD/${DBPASS}/" .env
  chmod 600 .env || true
fi

info "Validating Compose"
$COMPOSE config -q
ok "Compose syntax valid"

info "Pulling production images"
$COMPOSE pull

info "Starting PostgreSQL, Redis and Ollama"
$COMPOSE up -d postgres redis ollama

info "Starting model initialization"
$COMPOSE up -d model-init
MODEL=$(awk -F= '/^OLLAMA_MODEL=/{print $2}' .env)
MODEL_OK=0
I=0
while [ "$I" -lt 480 ]; do
  if docker exec internetboard-ollama ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -Fx "$MODEL" >/dev/null 2>&1; then MODEL_OK=1; break; fi
  I=$((I+1)); sleep 30
done
[ "$MODEL_OK" -eq 1 ] || fail "Model pull did not complete successfully"

info "Starting InternetBoard"
$COMPOSE up -d backend worker scheduler frontend
BACKEND_OK=0
I=0
while [ "$I" -lt 180 ]; do
  STATUS=$(docker inspect -f '{{.State.Health.Status}}' internetboard-backend 2>/dev/null || true)
  if [ "$STATUS" = "healthy" ]; then BACKEND_OK=1; break; fi
  I=$((I+1)); sleep 5
done
[ "$BACKEND_OK" -eq 1 ] || { $COMPOSE logs --tail=160 backend model-init ollama >&2 || true; fail "Backend did not become healthy"; }
PORT=$(awk -F= '/^WEB_PORT=/{print $2}' .env)
echo
echo "InternetBoard Production is ready."
echo "Web: http://<NAS-IP>:${PORT}"
echo "Default topics are seeded only once. Future image upgrades never overwrite user edits."
echo "Use the Export Handoff button in the web UI to create an LLM-ready Markdown handoff."
