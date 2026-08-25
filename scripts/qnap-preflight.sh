#!/bin/sh
set -eu

echo "===== IntelBoard QNAP Preflight ====="
echo "Architecture: $(uname -m)"
echo "Kernel: $(uname -r)"
MEM_KB=$(awk '/MemTotal/ {print $2}' /proc/meminfo)
MEM_GB=$((MEM_KB / 1024 / 1024))
echo "RAM: ${MEM_GB} GB"

if [ "$(uname -m)" != "x86_64" ]; then
  echo "[WARN] 当前包按 TS-673A x86_64 设计。"
fi

if [ "$MEM_GB" -ge 36 ]; then
  echo "Model auto profile: qwen3:30b-a3b-instruct-2507-q4_K_M"
elif [ "$MEM_GB" -ge 20 ]; then
  echo "Model auto profile: qwen3.5:9b"
elif [ "$MEM_GB" -ge 12 ]; then
  echo "Model auto profile: qwen3.5:4b"
else
  echo "Model auto profile: qwen3.5:2b"
  echo "[WARN] 内存较低，分析质量会下降。"
fi

echo ""
echo "GPU check:"
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi || true
else
  echo "Host shell may not expose nvidia-smi. Confirm GTX 1650 is assigned to Container Station and NVIDIA Driver/NvKernelDriver are installed."
fi

echo ""
echo "Persistent paths:"
echo "  /share/Container/intelboard/data"
echo "  /share/Container/intelboard/ollama"
echo "Recommended free storage: at least 50GB for model + archives + backups."
