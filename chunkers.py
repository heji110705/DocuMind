"""Markdown 感知切块（替换 LightRAG 默认的定长滑窗切块）。

默认切块为什么不够
------------------
`chunking_by_token_size` 是按 token 定长硬切。实测在含表格的文档上会出两类问题：

1. **表格被从中间切断**。一行 `| 规则模式 | 1289 | 4800 |` 被切开后，前半截在没有表头，
   后半截只剩数字，两半都答不出"p50 延迟是多少"。检索命中率随之掉下来。
2. **块失去上下文**。切出来的块只有裸数据，没有所属章节，向量里也就没有"这是延迟表"
   这层语义，查询"延迟"时自然排不到它。

这个切块器做三件事
------------------
1. **原子块**：表格（连续以 `|` 开头的行）、围栏代码块、标题行，永远不被切开。
   只有单个块本身就超过预算时才退化处理（超长表格按行分片，但每片重复表头）。
2. **章节路径**：每个块开头带上 `# 一级 > ## 二级 > ### 三级`，让表格里的数字带上语义。
3. **块级重叠**：把上一块的尾部完整块（默认 1 个）搬到下一块开头，
   保证跨块的指代（"上表的 full 行"）不会丢；重叠按块而不是按字符，避免切断句子。

token 计数走 LightRAG 传进来的 tokenizer，与默认切块保持同一套口径，便于对比。
"""

from __future__ import annotations

import re
from typing import Any

HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
TABLE_ROW = re.compile(r"^\s*\|")
FENCE = re.compile(r"^\s*(```|~~~)")


def _split_blocks(text: str) -> list[dict[str, Any]]:
    """把 Markdown 切成不可再分的块，并记下每块所属的标题层级。"""
    lines = text.splitlines()
    blocks: list[dict[str, Any]] = []
    stack: list[tuple[int, str]] = []  # (level, title)
    buf: list[str] = []
    mode: str | None = None  # None / "table" / "fence"

    def flush(kind: str = "para") -> None:
        if not buf:
            return
        body = "\n".join(buf).strip("\n")
        if body.strip():
            path = " > ".join(t for _, t in stack)
            blocks.append({"kind": kind, "text": body, "path": path})
        buf.clear()

    i = 0
    while i < len(lines):
        line = lines[i]

        if mode == "fence":
            buf.append(line)
            if FENCE.match(line):
                mode = None
                flush("code")
            i += 1
            continue

        m = HEADING.match(line)
        if m:
            flush()
            level = len(m.group(1))
            title = m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            blocks.append({"kind": "heading", "text": line, "path": " > ".join(t for _, t in stack)})
            i += 1
            continue

        if FENCE.match(line):
            flush()
            mode = "fence"
            buf.append(line)
            i += 1
            continue

        if TABLE_ROW.match(line):
            if mode != "table":
                flush()
                mode = "table"
            buf.append(line)
            i += 1
            continue

        if mode == "table":
            mode = None
            flush("table")
            continue  # 本轮不消费，交给下面的普通行处理

        if not line.strip():
            flush()
            i += 1
            continue

        buf.append(line)
        i += 1

    flush("table" if mode == "table" else "code" if mode == "fence" else "para")
    return blocks


def _split_oversize_table(text: str, tokenizer, budget: int) -> list[str]:
    """超长表格按行分片，每片都带上表头两行，保证任意一片单独看都能对上列。"""
    rows = text.splitlines()
    header, rest = rows[:2], rows[2:]
    head = "\n".join(header)
    if not rest:
        return [text]
    out: list[str] = []
    cur: list[str] = []
    for r in rest:
        cand = head + "\n" + "\n".join(cur + [r])
        if cur and len(tokenizer.encode(cand)) > budget:
            out.append(head + "\n" + "\n".join(cur))
            cur = [r]
        else:
            cur.append(r)
    if cur:
        out.append(head + "\n" + "\n".join(cur))
    return out


def markdown_chunker(
    tokenizer,
    content: str,
    split_by_character: str | None = None,
    split_by_character_only: bool = False,
    chunk_overlap_token_size: int = 100,
    chunk_token_size: int = 1200,
    isolate_tables: bool = True,
    **_ignored,
) -> list[dict[str, Any]]:
    """签名与 `lightrag.chunker.token_size.chunking_by_token_size` 一致，可直接注入。

    isolate_tables=True 时表格自成一块，不与正文混排（实测更利于"取表里某个数"）。
    """
    blocks = _split_blocks(content)
    if not blocks:
        return []

    budget = max(chunk_token_size, 200)
    # 表格允许略微超预算，宁可长一点也不切断（+25% 容忍度）
    soft_limit = int(budget * 1.25)

    chunks: list[dict[str, Any]] = []  # {tokens, text, is_table}
    cur: list[dict[str, Any]] = []
    cur_tokens = 0
    cur_path = ""

    def emit() -> None:
        nonlocal cur, cur_tokens, cur_path
        if not cur:
            return
        body = "\n\n".join(b["text"] for b in cur)
        head = f"[章节] {cur_path}\n\n" if cur_path else ""
        text = head + body
        chunks.append({"tokens": len(tokenizer.encode(text)), "text": text, "is_table": False})
        cur = []
        cur_tokens = 0
        cur_path = ""

    for b in blocks:
        # 标题行本身也进正文（章节标题是有语义的检索信号），但不单独成块：
        # 只有当前块已经装了六成以上才切，避免每个小节都产出一个小碎块。
        if b["kind"] == "heading":
            if cur and cur_tokens > budget * 0.6:
                emit()
            if not cur:
                cur_path = b["path"]
            cur.append({"kind": "heading", "text": b["text"], "path": b["path"]})
            cur_tokens += len(tokenizer.encode(b["text"]))
            continue

        t = len(tokenizer.encode(b["text"]))

        # 表格单独成块（isolate_tables）：实测把表格和正文塞进同一块时，
        # 一块里三四张表会把向量摊薄，查询"某行某个数"反而排不上；
        # 让表格自成一块（带上表头与章节路径）后，块向量就只代表这张表。
        if b["kind"] == "table" and isolate_tables and t <= soft_limit:
            emit()
            head = f"[章节] {b['path']}\n\n" if b["path"] else ""
            t2 = len(tokenizer.encode(head + b["text"]))
            chunks.append({"tokens": t2, "text": head + b["text"], "is_table": True})
            continue

        if b["kind"] == "table" and t > soft_limit:
            emit()
            for piece in _split_oversize_table(b["text"], tokenizer, budget):
                head = f"[章节] {b['path']}\n\n" if b["path"] else ""
                t3 = len(tokenizer.encode(head + piece))
                chunks.append({"tokens": t3, "text": head + piece, "is_table": True})
            continue

        if t > soft_limit and b["kind"] != "table":
            # 超长段落（罕见）：先按句子粗切，再逐段塞
            emit()
            for seg in re.split(r"(?<=[。！？；\n])", b["text"]):
                seg = seg.strip()
                if not seg:
                    continue
                st = len(tokenizer.encode(seg))
                if cur_tokens + st > budget and cur:
                    emit()
                cur.append({"kind": "para", "text": seg, "path": b["path"]})
                cur_tokens += st
                cur_path = cur_path or b["path"]
            continue

        if cur and cur_tokens + t > budget:
            emit()

        if not cur:
            cur_path = b["path"]
        cur.append(b)
        cur_tokens += t

    emit()

    # 合并过小的相邻块：短块单独送去检索，向量信息量不足、排序也排不上，
    # 合成一块反而更容易被命中。表格块不参与合并——它自成一块正是为了保持语义纯度。
    if len(chunks) > 1:
        merged_ok: list[dict[str, Any]] = []
        for c in chunks:
            if merged_ok and c["tokens"] < budget * 0.35 and not c["is_table"] and not merged_ok[-1]["is_table"]:
                cand = merged_ok[-1]["text"] + "\n\n" + c["text"]
                ct = len(tokenizer.encode(cand))
                if ct <= budget:
                    merged_ok[-1] = {"tokens": ct, "text": cand, "is_table": False}
                    continue
            merged_ok.append(c)
        chunks = merged_ok

    # 块级重叠：把上一块的最后一段搬到下一块开头（表格块不参与，避免表格被重复计入）
    if chunk_overlap_token_size > 0 and len(chunks) > 1:
        overlapped: list[dict[str, Any]] = [chunks[0]]
        for idx in range(1, len(chunks)):
            prev, cur_c = chunks[idx - 1], chunks[idx]
            if cur_c["is_table"] or prev["is_table"]:
                overlapped.append(cur_c)
                continue
            tail = prev["text"].split("\n\n")[-1]
            tt = len(tokenizer.encode(tail))
            if tt <= chunk_overlap_token_size + 60 and tt < prev["tokens"]:
                merged = tail + "\n\n" + cur_c["text"]
                overlapped.append({"tokens": len(tokenizer.encode(merged)), "text": merged, "is_table": False})
            else:
                overlapped.append(cur_c)
        chunks = overlapped

    return [
        {"tokens": c["tokens"], "content": c["text"], "chunk_order_index": i}
        for i, c in enumerate(chunks)
    ]
