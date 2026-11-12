#!/usr/bin/env bash
# 脚本 1：安装检查 + 起服务 + 冒烟 + 指标端点检查
# 作用与验收判据见 docs/setup.md
set -euo pipefail

VENV=${VENV:-$HOME/Project/vLLM-on-GPU-test/.venv}
MODEL=${MODEL:-$HOME/models/Qwen2.5-0.5B-Instruct}
PORT=${PORT:-8000}

echo "[1/4] vllm 版本"
"$VENV/bin/python" -c "import vllm; print(vllm.__version__)"

echo "[2/4] 起 vLLM 服务（彩排模型，不出验收数据）"
"$VENV/bin/vllm" serve "$MODEL" --port "$PORT" --gpu-memory-utilization 0.9 &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT

for _ in $(seq 1 150); do
  if curl -sf "http://localhost:$PORT/health" >/dev/null; then
    echo "service ready"
    break
  fi
  sleep 2
done

echo "[3/4] 冒烟：发一条请求"
curl -s "http://localhost:$PORT/v1/completions" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"$MODEL\",\"prompt\":\"hello\",\"max_tokens\":8}" | head -c 500
echo

echo "[4/4] 指标端点：列出全部 vllm 指标名"
curl -s "http://localhost:$PORT/metrics" | grep -E '^vllm:' | awk '{print $1}' | sort -u
