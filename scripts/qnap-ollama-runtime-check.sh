#!/bin/sh
set -eu
C="${1:-internetboard-ollama}"

echo "===== InternetBoard FINAL UVM Runtime Check ====="
echo

echo "=== Effective Ollama container policy ==="
docker exec "$C" sh -c '
for k in \
  GGML_CUDA_ENABLE_UNIFIED_MEMORY \
  LLAMA_ARG_LOAD_MODE \
  LLAMA_ARG_N_GPU_LAYERS \
  LLAMA_ARG_FIT \
  LLAMA_ARG_FIT_TARGET \
  LLAMA_ARG_MMPROJ_OFFLOAD \
  LLAMA_ARG_OP_OFFLOAD \
  OLLAMA_GPU_OVERHEAD \
  OLLAMA_CONTEXT_LENGTH \
  OLLAMA_KEEP_ALIVE \
  OLLAMA_MAX_LOADED_MODELS; do
  eval "v=\${$k-}"
  printf "%-34s %s\n" "$k" "${v:-<unset>}"
done
'

echo
echo "=== Loaded model ==="
docker exec "$C" ollama ps || true

echo
echo "=== llama-server command / placement ==="
docker logs "$C" 2>&1 | grep -E "starting llama-server|system memory|gpu memory|fit params|memory breakdown|offloaded|CUDA0.*layers|load_tensors|mmproj|Unified|unified" | tail -n 160 || true

echo
echo "=== Host RAM view of llama-server ==="
docker exec "$C" sh -c '
for d in /proc/[0-9]*; do
  [ -r "$d/comm" ] || continue
  [ "$(cat "$d/comm" 2>/dev/null)" = "llama-server" ] || continue
  echo "PID=${d#/proc/}"
  grep -E "^(VmSize|VmRSS|VmLck|VmSwap):" "$d/status" || true
done
' || true

echo
echo "=== NVIDIA snapshot ==="
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu --format=csv,noheader || true
else
  echo "nvidia-smi is not available in the host shell"
fi
