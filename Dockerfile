# 两阶段：builder 装依赖并预热模型，runtime 只带运行时产物。
# 这样做的主要收益是镜像里预置了 embedding 模型——
# 否则容器第一次查询要联网下 90MB 模型，表现为"首问卡 40 秒"。
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/opt/hf

WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt

# 预热：把 bge 模型下到 /opt/hf，运行时不需要外网
RUN PYTHONPATH=/install/lib/python3.12/site-packages \
    HF_ENDPOINT=https://hf-mirror.com \
    python -c "from fastembed import TextEmbedding; \
TextEmbedding('BAAI/bge-small-zh-v1.5'); print('embedding model cached')"

# LCEL 编排只多一个包。**必须放在所有模型预热层之后**：放在前面会让预热层的
# 下载缓存整体失效（实测多花 12 分钟，2026-11-17 踩过）。
COPY requirements-lcel.txt .
RUN pip install --prefix=/install -r requirements-lcel.txt


FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/opt/hf \
    HF_HUB_OFFLINE=1 \
    PDF_BACKEND=pymupdf \
    PORT=8000

COPY --from=builder /install /usr/local
COPY --from=builder /opt/hf /opt/hf

WORKDIR /app
COPY . .

# 不用 root 跑；/app/rag_storage 与 /app/data 由 compose 挂载，权限在 entrypoint 里处理
RUN useradd -m -u 10001 app && mkdir -p /app/rag_storage /app/data && chown -R app:app /app
USER app

EXPOSE 8000
# 探活要从 PORT 读，别写死 8000：CMD 用的是 $PORT，写死的话改了端口容器会一直 unhealthy。
HEALTHCHECK --interval=30s --timeout=3s --start-period=40s --retries=3 \
    CMD python -c "import os,urllib.request,sys; \
p=os.environ.get('PORT','8000'); \
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/healthz', timeout=2).status==200 else 1)"

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
