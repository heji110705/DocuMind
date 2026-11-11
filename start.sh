#!/usr/bin/env bash
# 一条命令起服务：缺 .env 就复制模板，缺索引就建，然后起 uvicorn。
set -e
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "[!] 已生成 .env，请先填入 LLM_API_KEY 再重跑"
  exit 1
fi

if ! python -c "import fastapi, uvicorn, lightrag" 2>/dev/null; then
  echo "[*] 安装依赖 ..."
  pip install -r requirements.txt -q
fi

TAG="${TAG:-prod}"
if [ ! -d "rag_storage/$TAG" ]; then
  echo "[*] 没有索引，先建（约 3 分钟，¥1 左右）..."
  python run_index.py --input seed --pages 0 --tag "$TAG" --gleaning 0
fi

PORT="${PORT:-8000}"
echo "[*] http://127.0.0.1:$PORT"
exec python -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT"
