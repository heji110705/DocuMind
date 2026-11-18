"""上下文预算：给证据块加可配上限，默认关闭。

为什么要它
    当前一次问答固定塞进 6 个块，实测块中位数 972 字、均值 939 字，也就是
    单题光证据就 5,600 字上下（约 3,500 token），而一次问答总共 4,272 token。
    问题的形状是：**证据占大头，而且不管问题难易都用同一个量**。
    于是"你答一句就够的问题"也在为 6 个块付费。

设计上的三条约束
1. **默认关闭**。默认值一动，README §2.1 那批数字全部作废，而那是简历上的口径。
   所以这里只加开关（``CTX_BUDGET=1``），关着的时候行为与改动前逐字节相同。
2. **裁剪必须发生在编号之前**。答案里的 ``[n]`` 是按证据块下标编的，
   如果只在拼提示词时裁、返回的 chunks 还是 6 个，引用编号就会错位——
   这是这类改动最容易踩的坑，也是本模块只在 retrieve 之后、judge 之前调一次的原因。
3. **至少保留一块**。全裁光会让 judge 直接判"证据不足"，
   看起来像"预算没省成，反而拒答率上升"，实际是裁错了。

判定顺序
    先按单块截断（长块压到 ``chunk_max_chars``）→ 再按总量装填（装不下就停）
    → 最后按块数封顶。先截断再装箱，避免"一个 1,900 字的长块把预算吃光"。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# 句末标点：截断尽量落在句子边界，不要切在词中间
_SENTENCE_ENDS = "。！？；\n"


@dataclass(frozen=True)
class Budget:
    """一组上限。``None`` 表示该项不限。"""

    max_chunks: int | None = 4
    max_total_chars: int | None = 3000
    chunk_max_chars: int | None = 800
    # 检索期就生效的上限：请求里要多少块，也不能超过它。
    # 默认 6 = DEFAULT_CHUNK_TOP_K，即"开预算也不改变默认检索量"；
    # 调小它才是真的往上游要更少，比事后裁剪更省（少召回、少重排）。
    max_chunk_top_k: int | None = 6


def _int_env(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def budget_from_env() -> Budget | None:
    """读环境变量。``CTX_BUDGET`` 不为 1 时返回 ``None``，表示不启用。

    容忍写错：值解析不出来就取默认，``CTX_BUDGET`` 写错则当关闭——
    宁可少省一点，也不要在生产上因为一个拼错的环境变量改变作答行为。
    """
    if (os.environ.get("CTX_BUDGET") or "0").strip() != "1":
        return None
    return Budget(
        max_chunks=_int_env("CTX_MAX_CHUNKS", 4),
        max_total_chars=_int_env("CTX_MAX_TOTAL_CHARS", 3000),
        chunk_max_chars=_int_env("CTX_CHUNK_MAX_CHARS", 800),
        max_chunk_top_k=_int_env("CTX_MAX_CHUNK_TOP_K", 6),
    )


def cap_chunk_top_k(chunk_top_k: int, budget: Budget | None) -> int:
    """把请求里的 chunk_top_k 压到上限以内。

    这是"往上游要更少"，与 apply_budget 的"事后少塞"不同：
    裁得早能连重排一起省，裁得晚只是提示词短了。
    """
    if budget is None or budget.max_chunk_top_k is None:
        return chunk_top_k
    return max(1, min(int(chunk_top_k), int(budget.max_chunk_top_k)))


def _clip(text: str, limit: int) -> str:
    """截到 limit 字符以内，尽量落在句子边界。

    边界回溯最多回退 120 字：再远就不如直接硬截——
    回退太狠会让一个 800 字的块变成 300 字，白白丢信息。
    """
    if limit <= 0 or len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind(ch) for ch in _SENTENCE_ENDS)
    if cut >= limit - 120:
        return head[: cut + 1]
    return head


def apply_budget(
    chunks: list[dict],
    budget: Budget | None = None,
) -> tuple[list[dict], dict]:
    """按预算裁剪证据块，返回 (新列表, 统计)。

    不修改入参（浅拷贝每一块，只替换 ``content``），
    因为调用方在响应里还要用原始对象。
    """
    stats = {
        "enabled": budget is not None,
        "n_in": len(chunks),
        "chars_in": sum(len(c.get("content") or "") for c in chunks),
        "n_out": len(chunks),
        "chars_out": sum(len(c.get("content") or "") for c in chunks),
        "clipped": 0,
        "dropped": 0,
    }
    if budget is None or not chunks:
        return chunks, stats

    kept: list[dict] = []
    total = 0
    for chunk in chunks:
        if budget.max_chunks is not None and len(kept) >= budget.max_chunks:
            break
        body = chunk.get("content") or ""
        if budget.chunk_max_chars is not None:
            clipped = _clip(body, budget.chunk_max_chars)
            if len(clipped) < len(body):
                stats["clipped"] += 1
            body = clipped

        # 至少留一块：即便第一块就已经超预算，也带一块进去，否则 judge 必然拒答
        if budget.max_total_chars is not None and kept and total + len(body) > budget.max_total_chars:
            break

        item = dict(chunk)
        item["content"] = body
        kept.append(item)
        total += len(body)

    stats["n_out"] = len(kept)
    stats["chars_out"] = total
    stats["dropped"] = len(chunks) - len(kept)
    return kept, stats
