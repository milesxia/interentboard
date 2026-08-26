#!/bin/sh
set -u
cd /share/Container/internetboard 2>/dev/null || cd "$(dirname "$0")" || exit 1
if docker compose version >/dev/null 2>&1; then COMPOSE="docker compose"; else COMPOSE="docker-compose"; fi
PORT=$(awk -F= '/^WEB_PORT=/{print $2}' .env 2>/dev/null)
echo "===== InternetBoard Doctor ====="
echo "Date: $(date)"
echo "Kernel: $(uname -a)"
echo "Memory:"; free -h 2>/dev/null || cat /proc/meminfo | head
echo "Disk:"; df -h /share/Container 2>/dev/null || true
echo "vm.overcommit_memory: $(cat /proc/sys/vm/overcommit_memory 2>/dev/null || echo unknown) (Redis recommends 1)"
echo
echo "DNS / Docker Hub registry:"
nslookup registry-1.docker.io 2>/dev/null || true
curl -4 -IsS --connect-timeout 8 --max-time 12 https://registry-1.docker.io/v2/ 2>/dev/null | head -1 || echo "Docker Hub HTTPS unavailable"
echo
echo "NVIDIA host:"; nvidia-smi 2>/dev/null || echo "host nvidia-smi unavailable"
echo "Compose:"; $COMPOSE version 2>/dev/null || true
echo "Containers:"; $COMPOSE ps 2>/dev/null || true
echo "Worker ping:"; docker exec internetboard-worker sh -lc 'celery -A app.tasks.celery_app inspect ping -d celery@$HOSTNAME --timeout=5' 2>/dev/null || echo "worker ping failed"
echo "Redis queue depth:"; docker exec internetboard-redis redis-cli llen celery 2>/dev/null || true
echo "Ollama GPU:"; docker exec internetboard-ollama nvidia-smi 2>/dev/null || echo "GPU unavailable in container"
echo "Ollama models:"; docker exec internetboard-ollama ollama list 2>/dev/null || true
echo "Ollama processor split:"; docker exec internetboard-ollama ollama ps 2>/dev/null || true
echo "Frontend health:"; curl -fsS "http://127.0.0.1:${PORT:-8733}/health" 2>/dev/null || true
echo
echo "Runtime status:"; curl -fsS "http://127.0.0.1:${PORT:-8733}/api/system/status" 2>/dev/null || true
echo
echo "Recent logs:"; $COMPOSE logs --tail=100 backend worker scheduler redis ollama 2>/dev/null || true
