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
#
# 这里踩过一个必须写下来的坑。最初观察到"160 字符 91ms/条、192 字符 799ms/条"，
# 以为是序列长度跨过阈值后换了慢路径，于是把 MAX_CHARS 压到 160。延迟确实下来了，
# 但 40 题 A/B 显示可答率从 95.0% 掉到 67.5%——因为 chunk 开头 160 字往往只有小标题，
# 真正区分两块内容的信息都在后面，截断等于让重排失去判断依据。
#
# 后来用 tools/bench_warm.py 同一份输入重复 12 次才发现真相：前 10 次稳定 350ms，
# 第 11、12 次突然 4710/7461ms。同输入、同进程、同 shape，不存在"长度断崖"，
# 是这台 16GB 机器上的周期性卡顿（同时跑着编辑器、Defender 等）。
# 之前的"拐点"就是把这种抖动当成了长度效应。
#
# 结论：不要为了躲一个不存在的断崖去砍上下文。chunk 在语料里中位数 973 字，
# 512 字能覆盖前半部分，这是在"看得够多"和"跑得动"之间唯一站得住的位置。
MAX_CHARS = int(os.environ.get("RERANK_MAX_CHARS", "512"))

# 单次最多给多少个候选打分。默认 40 = 实际不截断（mix 模式在 122 块语料上召回 28~32 个）。
#
# 这里走过一段弯路，值得留着：为了降延迟，先把候选上限压到 20，再压到 14。
# 同一批 40 题的结果是 14 → 85.0%、20 → 90.0%、不截断 → 95.0%。
# 原因很直接：**重排存在的理由就是把"向量排低了但语义对"的那块捞上来**，
# 只让前 14 个有资格被打分，等于把前 14 名之外的所有正确答案直接判死——
# 而向量排在第 15~32 名、本该靠重排翻盘的块，恰恰是重排唯一能贡献价值的那部分。
#
# 所以这个参数不是"性能旋钮"，是"功能旋钮"。真要降延迟，只能换更快的模型或上 GPU；
# 用调小它来降延迟，本质是在悄悄关掉功能。演示时想快可以临时设小，
# 但要知道代价是 5-10pp。
MAX_DOCS = int(os.environ.get("RERANK_MAX_DOCS", "40"))

# 一次送 8 条进模型。fastembed 默认 batch_size=64，会把整批 padding 到批内最长那条；
# 实测 batch_size=20 时偶发稳定复现的 41s（5 次重复全部 41~44s），这是真的病态路径，
# 不是抖动。8 是实测下来方差最小的一档。
BATCH_SIZE = int(os.environ.get("RERANK_BATCH_SIZE", "8"))

# 关于并发：试过两种串行化方案，都不好。
#   1) threading.Lock + asyncio.to_thread：4 个 worker 各占一个默认线程等锁，
#      把默认 executor 占满，检索从 20s 劣化到 99s。
#   2) 专用单线程 ThreadPoolExecutor：独立脚本里正常，接进 LightRAG 的 worker 池后
#      直接不返回（卡在 "Rerank func: 4 new workers initialized" 之后）。
# 最后回到最朴素的做法：不加锁、用 asyncio.to_thread。安全性由 fastembed 的
# 会话内部保证。之前那次死锁大概率是"一次喂 50~60 条 + 批内 padding 到最长"共同造成的，
# 现在候选稳定在 32 条以内、batch 固定 8，再没复现过。


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
    out = list(get_model().rerank(query, trimmed, batch_size=BATCH_SIZE))
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
    docs = list(documents)
    if not docs:
        return []
    head, tail = docs[:MAX_DOCS], docs[MAX_DOCS:]
    scored = await asyncio.to_thread(_score_sync, query, head)
    scored.sort(key=lambda x: x[1], reverse=True)
    out = [{"index": i, "relevance_score": s} for i, s in scored]
    # 没参与打分的按原顺序接在后面，给一个低于任何实数分数的值，
    # 保证它们不会被排到前面，但也不会被丢掉（top_n 可能会取到它们）
    floor = min((r["relevance_score"] for r in out), default=0.0) - 1.0
    out += [{"index": MAX_DOCS + k, "relevance_score": floor} for k in range(len(tail))]
    if top_n:
        out = out[:top_n]
    return out


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
