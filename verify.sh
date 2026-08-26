#!/bin/sh
set -eu
cd /share/Container/internetboard 2>/dev/null || cd "$(dirname "$0")"
. ./.env

if docker compose version >/dev/null 2>&1; then COMPOSE="docker compose"; else COMPOSE="docker-compose"; fi
MODEL="$OLLAMA_MODEL"
PORT="$WEB_PORT"

echo "[1/6] Compose services"
$COMPOSE ps

echo "[2/6] Exact model"
docker exec internetboard-ollama ollama list | awk 'NR>1 {print $1}' | grep -Fx "$MODEL" >/dev/null

echo "[3/6] NVIDIA visibility"
docker exec internetboard-ollama nvidia-smi >/dev/null

echo "[4/6] GPU processor participation"
SPLIT=$(docker exec internetboard-ollama ollama ps 2>/dev/null || true)
echo "$SPLIT"
echo "$SPLIT" | grep -qi GPU

echo "[5/6] Core HTTP health"
curl -fsS "http://127.0.0.1:${PORT}/health"
echo

echo "[6/6] Authenticated API status"
curl -fsS -H "X-API-Key: ${INTERNETBOARD_API_KEY}" "http://127.0.0.1:${PORT}/api/system/status"
echo
echo "InternetBoard verification: PASS"
