#!/bin/sh
set -eu

echo "===== InternetBoard v0.4 QNAP Preflight ====="
echo "Architecture: $(uname -m)"
echo "Kernel: $(uname -r)"
MEM_KB=$(awk '/MemTotal/ {print $2}' /proc/meminfo)
MEM_GB=$((MEM_KB / 1024 / 1024))
echo "RAM: ${MEM_GB} GB"

echo "Final model: qwen3.8:27b-q4_K_M (18GB)"
echo "Extractor:   qwen3:4b-instruct-2507-q4_K_M (2.5GB, text-only)"
echo "Embedding:   qwen3-embedding:0.6b (on-demand after claim threshold)"
echo "Strategy: source/version dedup -> chunks -> 4B extraction -> hybrid RAG -> 27B final reasoning"

echo ""
echo "GPU check:"
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi || true
else
  echo "Host shell may not expose nvidia-smi. Confirm GTX 1650 is assigned to Container Station."
fi

echo ""
echo "Persistent paths:"
echo "  /share/Container/internetboard/data"
echo "  /share/Container/internetboard/ollama"
echo "Recommended free storage: >= 60GB for two models + archives + backups."
