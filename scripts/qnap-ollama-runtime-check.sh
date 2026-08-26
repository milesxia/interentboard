#!/bin/sh
set -eu

C="${1:-internetboard-ollama}"

echo "===== InternetBoard Ollama V4.5 Runtime Check ====="
echo "Container: $C"
echo

echo "=== Ollama/llama.cpp policy in container ==="
docker exec "$C" sh -c '
for k in LLAMA_ARG_LOAD_MODE LLAMA_ARG_FIT LLAMA_ARG_FIT_TARGET LLAMA_ARG_MMPROJ_OFFLOAD OLLAMA_CONTEXT_LENGTH OLLAMA_NUM_PARALLEL OLLAMA_MAX_LOADED_MODELS; do
  eval "v=\${$k-}"
  printf "%-30s %s\n" "$k" "${v:-<unset>}"
done
'
echo

echo "=== llama-server resident and locked RAM ==="
docker exec "$C" sh -c '
found=0
for d in /proc/[0-9]*; do
  [ -r "$d/comm" ] || continue
  name=$(cat "$d/comm" 2>/dev/null || true)
  [ "$name" = "llama-server" ] || continue
  found=1
  pid=${d#/proc/}
  echo "PID: $pid"
  grep -E "^(VmSize|VmRSS|VmLck|VmSwap):" "$d/status" || true
  if [ -r "$d/smaps_rollup" ]; then
    echo "-- smaps_rollup --"
    grep -E "^(Rss|Pss|Locked|Swap):" "$d/smaps_rollup" || true
  fi
  if [ -r "$d/environ" ]; then
    echo "-- child LLAMA_ARG_* --"
    tr "\000" "\n" < "$d/environ" | grep "^LLAMA_ARG_" | sort || true
  fi
done
if [ "$found" -eq 0 ]; then
  echo "llama-server is not loaded. Trigger one AI/research request first."
fi
'
echo

echo "=== recent placement / fit / projector logs ==="
docker logs "$C" 2>&1 | \
  grep -E "starting llama-server|offloaded [0-9]+/|CUDA0 .*layers|failed to mlock|load.mode|load_mode|mmproj|multimodal projector|fit params|common_fit_params|memory breakdown" | \
  tail -n 140 || true

echo

echo "=== NVIDIA snapshot ==="
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu --format=csv,noheader || true
else
  echo "nvidia-smi is not exposed in this host shell."
fi

echo
echo "Expected after a real V4.5 load:"
echo "  LLAMA_ARG_LOAD_MODE=mlock"
echo "  LLAMA_ARG_MMPROJ_OFFLOAD=false"
echo "  VmLck should be GiB-scale, not 0 kB"
echo "  text-layer offload should exceed the old 3/66 if auto-fit permits"
echo "  GPU utilization can still be bursty because most of 27B remains on CPU"
