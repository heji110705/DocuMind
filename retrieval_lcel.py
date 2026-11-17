"""mix 链路的 LCEL 编排（外层重写，不碰 LightRAG 源码）。

一句话：把「三种检索 → 合并去重 → 重排 → 编号」这四步写成一条 LCEL 链，
用 ``RunnableParallel`` 让 naive / local / global 三条检索真正并发跑。

和 LightRAG 内置 mix 的关系（重要，别被"等价"骗了）
----------------------------------------------------
LightRAG 的 mix **不是**"并行跑三个 mode"。读 ``lightrag/operate.py`` 的 mix 分支可以看到
它是：**一次关键词抽取** → local 走实体、global 走关系、再加一路纯向量的 chunks，
然后在内部按 chunk 出现次数/顺序做合并。

所以本模块产出的候选集**和 mix 不完全相同**，它更接近"三个 mode 各自独立召回再取并集"：
- 好处：三条召回互不干扰，任何一条的新增证据都不会被内部的合并策略稀释；
- 代价：三个 mode 各自会做一次关键词抽取，冷缓存下**多两次模型调用**。
这个代价会被如实算进 A/B 的「单题成本」，不做隐藏。

开关
----
``RETRIEVAL_IMPL=lcel``（默认）走本模块；``RETRIEVAL_IMPL=lightrag`` 一键回退原 mix。
只对 ``mode == "mix"`` 生效，其它 mode 仍然交给 LightRAG（naive/local/global 单跑没有编排价值）。

设计约束
--------
1. **输出形状必须和原来一致**：``[{"chunk_id","content","file_path",...}]``，
   因为下游 ``app/main.py`` 用 ``chunks[i-1]["file_path"]`` 与 ``["content"][:600]``
   拼引用，字段一变界面和评测脚本同时断。
2. **重排复用 ``rerankers.local_rerank``**，不引入第二套模型。
3. **纯函数可单测**：合并/去重/裁剪不依赖 LightRAG，CI 的最小依赖环境能跑。
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

__all__ = [
    "build_chain",
    "content_hash",
    "merge_branches",
    "order_by_rerank",
    "IMPL_ENV",
]

#: 切换实现的环境变量。默认 lcel；设成 lightrag 即回到改动前的行为。
IMPL_ENV = "RETRIEVAL_IMPL"


def content_hash(chunk: dict) -> str:
    """按正文算去重键。

    为什么不用 chunk_id：三条检索来自不同的库（chunks_vdb / entities_vdb /
    relationships_vdb），同一个原文块在不同分支里可能带着不同的 id 前缀，
    按 id 去重会漏。正文才是"同一块证据"的唯一标识。
    """
    text = (chunk.get("content") or "").strip()
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def merge_branches(
    branches: list[list[dict]], limit: int, *, keep_order: bool = True
) -> list[dict]:
    """多路召回取并集，按正文去重，保序（先到先得）。

    ``keep_order=True`` 时保留"分支顺序 + 分支内顺序"：naive 是纯向量召回，
    通常最贴题，排前面能让后续重排的截断更稳。重排开启时这个顺序只是候选池顺序，
    不影响最终排序。
    """
    seen: set[str] = set()
    out: list[dict] = []
    for branch in branches:
        for chunk in branch or []:
            if not isinstance(chunk, dict):
                continue
            if not (chunk.get("content") or "").strip():
                continue          # 空正文没有引用价值，也拼不出 snippet
            key = content_hash(chunk)
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(chunk)
            if limit > 0 and len(out) >= limit:
                return out
    return out


def order_by_rerank(chunks: list[dict], ranked: list[dict], top_n: int) -> list[dict]:
    """按重排结果重排并截断；``ranked`` 是 ``[{"index","relevance_score"}]``。

    越界的 index 直接丢掉（重排实现换了也不会把整个请求带崩），
    没被重排覆盖到的块按原顺序接在后面——宁可多给一条，也不要因为
    重排只返回了前 N 条就把证据截没。
    """
    n = len(chunks)
    picked: list[dict] = []
    used: set[int] = set()
    for item in ranked or []:
        try:
            idx = int(item.get("index", -1))
        except (TypeError, ValueError):
            continue
        if 0 <= idx < n and idx not in used:
            used.add(idx)
            picked.append(chunks[idx])
            if len(picked) >= top_n:
                return picked
    for idx, chunk in enumerate(chunks):
        if idx in used:
            continue
        picked.append(chunk)
        if len(picked) >= top_n:
            break
    return picked


def build_chain(
    *,
    rag: Any,
    top_k: int,
    chunk_top_k: int,
    enable_rerank: bool,
    rerank_func: Callable[..., Awaitable[list[dict]]] | None = None,
    modes: tuple[str, ...] = ("naive", "local", "global"),
):
    """构造 LCEL 链。``rag`` 是已初始化的 LightRAG 实例。

    langchain 与 lightrag 都在函数内 import：这样单元测试只测纯函数时，
    不需要在 CI 的最小依赖环境里装上这两坨东西。
    """
    from langchain_core.runnables import RunnableLambda, RunnableParallel
    from lightrag import QueryParam

    async def _branch(mode: str, question: str) -> list[dict]:
        param = QueryParam(
            mode=mode,
            top_k=top_k,
            chunk_top_k=chunk_top_k,
            # 分支内不重排：重排放在合并之后做一次，否则三个分支各排一次
            # 既浪费 CPU，又拿不到"跨分支比较"的效果。
            enable_rerank=False,
            include_references=True,
        )
        data = await rag.aquery_data(question, param=param)
        payload = (data or {}).get("data") or {}
        return payload.get("chunks") or []

    def _reader(mode: str):
        async def _run(payload: dict) -> list[dict]:
            return await _branch(mode, payload["question"])

        return RunnableLambda(_run)

    parallel = RunnableParallel(
        question=RunnableLambda(lambda payload: payload["question"]),
        **{mode: _reader(mode) for mode in modes},
    )

    pool_size = max(top_k, chunk_top_k) * len(modes)

    async def _merge(payload: dict) -> dict[str, Any]:
        branches = [payload.get(mode) or [] for mode in modes]
        merged = merge_branches(branches, limit=pool_size)
        return {"question": payload["question"], "chunks": merged}

    async def _rerank(payload: dict) -> dict[str, Any]:
        chunks = payload["chunks"]
        if not chunks:
            return {"chunks": [], "used_rerank": False}
        if not (enable_rerank and rerank_func):
            return {"chunks": chunks[:chunk_top_k], "used_rerank": False}
        docs = [(c.get("content") or "") for c in chunks]
        ranked = await rerank_func(payload["question"], docs, top_n=chunk_top_k)
        return {
            "chunks": order_by_rerank(chunks, ranked, chunk_top_k),
            "used_rerank": True,
        }

    return parallel | RunnableLambda(_merge) | RunnableLambda(_rerank)
