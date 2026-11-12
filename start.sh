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
  # 优先用 lock（精确复现已知可用的环境）；lock 缺失时退回 requirements.txt。
  # 只退不回，绝不静默跳过——缺依赖时宁可报错，也不起一个半残的服务。
  if [ -f requirements.lock ]; then
    pip install -r requirements.lock -q
  else
    pip install -r requirements.txt -q
  fi
fi

# 索引目录名要和 app 读到的 TAG 一致：.env 里写了 TAG 就得用它，
# 否则脚本检查 rag_storage/prod，服务却去打开 rag_storage/<别的 tag>，起来就报索引不存在。
TAG="${TAG:-$(sed -n 's/^TAG=//p' .env | tail -1)}"
TAG="${TAG:-prod}"
export TAG

if [ ! -d "rag_storage/$TAG" ]; then
  echo "[*] 没有索引，先建（约 3 分钟，¥1 左右）..."
  python run_index.py --input seed --pages 0 --tag "$TAG" --gleaning 0
fi

PORT="${PORT:-$(sed -n 's/^PORT=//p' .env | tail -1)}"
PORT="${PORT:-8000}"
echo "[*] http://127.0.0.1:$PORT （索引 tag=$TAG）"
exec python -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT"
