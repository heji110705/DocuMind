"""一次提问里，时间到底花在哪。

现象：mix_rr 单请求 p50 50.8s，但独立测 cross-encoder 给 14 条候选打分只要几秒。
怀疑是 LightRAG 的 rerank 包装层（priority_limit_async_func_call，
RERANK_TIMEOUT 默认 30s，超时会重试）把一次 3 秒的调用放大成 30+20 秒。

这里把 local_rerank 包一层计时，打印每次调用的入参规模与耗时，
同时打印 ask() 的整体耗时，看两者能不能对上。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import logging

logging.basicConfig(level=logging.WARNING)   # 压掉 lightrag 的 INFO 噪声

import rerankers
import query as qmod

_raw = rerankers.local_rerank
_calls: list[tuple[int, float]] = []


async def timed_rerank(query, documents, top_n=None, extra_body=None):
    n = len(documents)
    lens = [len(d) for d in documents]
    t0 = time.perf_counter()
    print(f"  → rerank 开始：{n} 条候选，长度 {min(lens)}~{max(lens)}")
    out = await _raw(query, documents, top_n=top_n, extra_body=extra_body)
    dt = (time.perf_counter() - t0) * 1000
    _calls.append((n, dt))
    print(f"  ← rerank 结束：{dt:.0f} ms")
    return out


qmod.local_rerank = timed_rerank


async def main() -> None:
    t_import = time.perf_counter()
    dm = qmod.DocuMind(tag="prod", embed="local", use_rerank=True)
    await dm.open()
    print(f"索引就绪 {time.perf_counter() - t_import:.1f}s\n")

    qs = [
        "订单事实表的粒度是什么，一单多书会重复计数吗",
        "精装书和平装书的价差是多少",
        "错误码 42901 是什么含义，怎么处理",
    ]
    for q in qs:
        _calls.clear()
        t0 = time.perf_counter()
        ans = await dm.ask(q, mode="mix")
        dt = time.perf_counter() - t0
        rr = sum(c[1] for c in _calls) / 1000
        print(f"\n问题：{q}")
        print(f"总耗时 {dt:.1f}s，其中 rerank {rr:.1f}s（{len(_calls)} 次调用）")
        print(f"答案前 80 字：{ans.text[:80].replace(chr(10), ' ')}")
        print("-" * 70)


if __name__ == "__main__":
    asyncio.run(main())
