#!/bin/sh
set -u
cd /share/Container/internetboard 2>/dev/null || cd "$(dirname "$0")" || exit 1
if docker compose version >/dev/null 2>&1; then COMPOSE="docker compose"; else COMPOSE="docker-compose"; fi

echo "===== InternetBoard Doctor ====="
echo "Date: $(date)"
echo "Kernel: $(uname -a)"
echo "Memory:"; free -h 2>/dev/null || cat /proc/meminfo | head
echo "Disk:"; df -h /share/Container 2>/dev/null || true
echo "NVIDIA host:"; nvidia-smi 2>/dev/null || echo "host nvidia-smi unavailable"
echo "Compose:"; $COMPOSE version 2>/dev/null || true
echo "Containers:"; $COMPOSE ps 2>/dev/null || true
echo "Ollama GPU:"; docker exec internetboard-ollama nvidia-smi 2>/dev/null || echo "GPU unavailable in container"
echo "Ollama models:"; docker exec internetboard-ollama ollama list 2>/dev/null || true
echo "Ollama processor split:"; docker exec internetboard-ollama ollama ps 2>/dev/null || true
echo "Health:"; PORT=$(awk -F= '/^WEB_PORT=/{print $2}' .env 2>/dev/null); curl -fsS "http://127.0.0.1:${PORT:-8788}/health" 2>/dev/null || true
echo
echo "Recent backend logs:"; $COMPOSE logs --tail=80 backend worker scheduler ollama 2>/dev/null || true
