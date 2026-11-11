"""本地 cross-encoder 重排，零费用、CPU 可跑。

LightRAG 1.5.x 允许注入 rerank_model_func，签名：
    async def f(query: str, documents: list[str], top_n: int | None, extra_body: dict | None)
返回 [{"index": int, "relevance_score": float}, ...]（按分数降序）。

用 bge-reranker-v2-m3-int8：多语种、支持中文、量化后 0.57GB。
"""

from __future__ import annotations

import asyncio
import os
import threading

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")  # 国内下载走镜像

_MODEL = None
_LOCK = threading.Lock()

MODEL_NAME = os.environ.get("LOCAL_RERANK_MODEL", "BAAI/bge-reranker-v2-m3-int8")

# 单篇送进 cross-encoder 的最大字符数。
# 实测（本机 CPU）：7 篇 1800 字要 22s，截到 800 字 5.2s、500 字 2.7s、300 字 1.2s。
# 评测要跑 80 次重排，不截断根本跑不完；代价是超长段落的尾部信息不参与打分，
# 所以最终答案是拿完整 chunk 生成的，截断只影响排序、不影响引用原文。
MAX_CHARS = int(os.environ.get("RERANK_MAX_CHARS", "512"))


def get_model():
    global _MODEL
    if _MODEL is None:
        with _LOCK:
            if _MODEL is None:
                from fastembed.rerank.cross_encoder import TextCrossEncoder

                _MODEL = TextCrossEncoder(model_name=MODEL_NAME)
    return _MODEL


def _score_sync(query: str, documents: list[str]) -> list[tuple[int, float]]:
    trimmed = [d[:MAX_CHARS] for d in documents]
    out = list(get_model().rerank(query, trimmed))
    if not out:
        return []
    first = out[0]
    if isinstance(first, (tuple, list)) and len(first) == 2:
        return [(int(i), float(s)) for i, s in out]
    return [(i, float(s)) for i, s in enumerate(out)]


async def local_rerank(
    query: str,
    documents: list[str],
    top_n: int | None = None,
    extra_body: dict | None = None,
) -> list[dict]:
    if not documents:
        return []
    scored = await asyncio.to_thread(_score_sync, query, list(documents))
    scored.sort(key=lambda x: x[1], reverse=True)
    if top_n:
        scored = scored[:top_n]
    return [{"index": i, "relevance_score": s} for i, s in scored]


if __name__ == "__main__":
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    q = "精装书比平装书贵多少"
    docs = [
        "精装与平装的价差为 18.65 元，平装样本 173 本。",
        "今天广州多云，气温 22 到 28 度。",
        "全书共 250 条记录，其中分类字段有 12 种取值。",
    ]
    res = asyncio.run(local_rerank(q, docs))
    print("模型:", MODEL_NAME)
    for r in res:
        print(f"  {r['relevance_score']:+.3f}  {docs[r['index']][:32]}")
