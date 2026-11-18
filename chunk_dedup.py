"""检索期去重：把"重复解释同一件事"的块从证据里剔掉。

为什么是检索期而不是重建语料
    真正的"合并语料再重建索引"要重跑 LightRAG 的实体抽取（每块一次 LLM 调用），
    122 块 ≈ 十几万 token，当前免费额度已经见底。而**对答题有影响的只是"进提示词的那几块"**，
    所以先在检索结果上去重——它衡量的是同一个问题："少了重复块，分会不会掉"。
    语料层面的合并（122 → N）留作 TODO，等额度恢复再做，成本估在 README 里。

口径
    相似度用**字符三元组 Jaccard**，不用向量：
      1) 向量相似度高不等于"同一件事"（同一张表被滑窗切开，相邻块相似度 0.98，
         但它们是不同行，合并会丢数据）；
      2) 字符三元组对"同一段文字被切两次"极其敏感，对"讲同一主题的不同段落"不敏感，
         正好是这里要的区分。
    实测本语料：insightpilot-docs-plus.md 那张结果表被切成 32 块，
    相邻块字符三元组 Jaccard 在 0.55~0.75 之间——**它们不是重复，是同一张表的不同行**，
    所以去重阈值定在 0.85 以上，只砍真正重复的。

不做的两件事
    * 不做摘要式合并（要 LLM，会改内容，分数不可比）；
    * 不跨文档合并（不同文档对同一概念的不同措辞，往往各有信息）。
"""

from __future__ import annotations

import os
import re

DEFAULT_THRESHOLD = 0.85
_GRAM = 3


def _grams(text: str) -> set[str]:
    body = re.sub(r"\s+", "", text or "")
    if len(body) <= _GRAM:
        return {body} if body else set()
    return {body[i : i + _GRAM] for i in range(len(body) - _GRAM + 1)}


def similarity(a: str, b: str) -> float:
    """字符三元组 Jaccard。0 = 无公共片段，1 = 完全相同。"""
    ga, gb = _grams(a), _grams(b)
    if not ga or not gb:
        return 0.0
    inter = len(ga & gb)
    return inter / (len(ga) + len(gb) - inter)


def threshold_from_env() -> float | None:
    """``DEDUP=1`` 才启用；阈值 ``DEDUP_THRESHOLD``（默认 0.85）。

    写错一律当关闭——一个拼错的环境变量不该悄悄改变作答行为。
    """
    if (os.environ.get("DEDUP") or "0").strip() != "1":
        return None
    raw = (os.environ.get("DEDUP_THRESHOLD") or "").strip()
    try:
        val = float(raw) if raw else DEFAULT_THRESHOLD
    except ValueError:
        val = DEFAULT_THRESHOLD
    return min(max(val, 0.5), 0.999)


def dedupe(chunks: list[dict], threshold: float | None = None) -> tuple[list[dict], dict]:
    """按相似度剔除重复块，保留排序更靠前的那个。

    只在**同一文档内部**比较：跨文档的相似文本通常是"同一概念的不同表述"，
    删掉谁都是信息损失；而同一文档里高度相似的块，基本都是切分造成的重复。
    """
    stats = {"enabled": threshold is not None, "n_in": len(chunks), "n_out": len(chunks), "dropped": []}
    if threshold is None or len(chunks) < 2:
        return chunks, stats

    kept: list[dict] = []
    for chunk in chunks:
        body = chunk.get("content") or ""
        path = chunk.get("file_path") or ""
        dup_of = None
        for k in kept:
            if (k.get("file_path") or "") != path:
                continue
            sim = similarity(body, k.get("content") or "")
            if sim >= threshold:
                dup_of = (k.get("file_path"), round(sim, 4))
                break
        if dup_of:
            stats["dropped"].append({"file_path": path, "sim": dup_of[1]})
            continue
        kept.append(chunk)

    stats["n_out"] = len(kept)
    return kept, stats
