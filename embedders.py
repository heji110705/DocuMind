"""统一封装两种 embedding 后端：云端（百炼）与本地（fastembed）。

为什么要有本地后端：
- 百炼 text-embedding-v4 需要开通且无免费额度时按量计费，虽然只要几分钱，
  但会卡住"今天必须跑通"这件事。
- 本地模型 0 费用、断网可用，且能写进简历作为"双后端可切换"的工程点。

用法：
    from embedders import build_embedder
    fn, dim, name = build_embedder("local")
"""

import asyncio
import os
import sys

import numpy as np

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_LOCAL_CACHE = {}


def build_embedder(kind: str = "bailian"):
    """返回 (async embed_func, dim, display_name)。"""
    kind = (kind or "bailian").lower()

    if kind in ("local", "fastembed", "offline"):
        model_name = os.getenv("LOCAL_EMBED_MODEL", "BAAI/bge-small-zh-v1.5")
        dim = int(os.getenv("LOCAL_EMBED_DIM", "512"))
        # 镜像：国内直连 HuggingFace 常失败，默认走 hf-mirror
        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

        if model_name not in _LOCAL_CACHE:
            from fastembed import TextEmbedding

            _LOCAL_CACHE[model_name] = TextEmbedding(model_name)
        model = _LOCAL_CACHE[model_name]

        async def embed_local(texts):
            # fastembed 是同步 CPU 计算，放线程池避免阻塞事件循环
            def run():
                vecs = list(model.embed(list(texts)))
                return np.array(vecs, dtype=np.float32)

            return await asyncio.to_thread(run)

        return embed_local, dim, f"local:{model_name}"

    if kind in ("bailian", "cloud", "openai"):
        from lightrag.llm.openai import openai_embed

        dim = int(os.getenv("EMBEDDING_DIM", "1024"))
        model_name = os.getenv("EMBEDDING_MODEL", "text-embedding-v4")

        async def embed_cloud(texts):
            return await openai_embed.func(
                list(texts),
                model=model_name,
                base_url=os.getenv("BAILIAN_BASE_URL"),
                api_key=os.getenv("BAILIAN_API_KEY"),
                dimensions=dim,
            )

        return embed_cloud, dim, f"cloud:{model_name}"

    raise ValueError(f"未知的 embedding 后端：{kind}（可选 bailian / local）")


def default_backend() -> str:
    return os.getenv("EMBED_BACKEND", "bailian")
