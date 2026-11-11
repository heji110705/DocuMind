"""DocuMind 问答链路：检索 →（可选重排）→ 证据充分性判定 → 带引用作答 / 拒答。

与直接用 rag.aquery 的区别：
1. 先只取证据（aquery_data），拿到结构化的 chunks，自己编号后再拼提示词，
   所以每条引用都能回到具体 chunk，而不是让模型自由发挥。
2. 增加"证据充分性判定"这一跳：证据不够就直接拒答，不让模型在空白处编。
3. 全程计 token / 计成本，评测可以直接用真实账单。

用法：
    python query.py -q "full 配置的 EX 是多少？"
    python query.py -q "..." --mode naive --no-rerank --no-guard
    python query.py --file questions.txt
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

from lightrag import LightRAG  # noqa: E402
from lightrag.base import QueryParam  # noqa: E402
from lightrag.kg.shared_storage import initialize_pipeline_status  # noqa: E402
from lightrag.utils import EmbeddingFunc  # noqa: E402

from embedders import build_embedder  # noqa: E402
from llm_client import USAGE, llm_func  # noqa: E402
from rerankers import get_model as get_reranker  # noqa: E402
from rerankers import local_rerank  # noqa: E402

REFUSAL_TEXT = "证据不足：文档中未提及相关内容。"
REFUSAL_HINTS = ("证据不足", "未提及", "未提到", "没有提到", "无法回答", "无法从材料", "材料中没有")

SYSTEM_PROMPT = (
    "你是严谨的文档问答助手。以下规则没有例外：\n"
    "1. 只能使用【证据】中的内容作答，不得引入外部知识或自行推断。\n"
    "2. 每个事实句末尾必须标注来源编号，格式为 [1]、[2]，可多个。\n"
    "3. 只有当【证据】完全没有涉及问题所问的对象时，才输出这一句："
    + REFUSAL_TEXT
    + "\n"
    "   证据里有部分相关信息时，就把能确认的部分答出来并标注来源，不要因为不完整而整段拒答。\n"
    "4. 回答用中文，先给结论再给依据，不要复述问题。"
)

# 为什么是"先摘录、再判定"而不是直接输出一个标签：
# 实测发现直接判标签时，证据 1 万多字、答案埋在表格里，模型倾向于整体印象判 INSUFFICIENT，
# 同一题重复跑还会给出不同结论。强制先逐字抄一句原文，等于让它先定位再表态，
# 判定既稳定又可人工复核（摘录原文能直接看出它凭什么判）。
JUDGE_PROMPT = (
    "你在做 RAG 的证据审核。必须按下面两步作答，不要做别的解释。\n\n"
    "第一步 摘录：从【证据】里逐字抄一句能直接回答问题所问的原文（最长两句）。"
    "表格整行、列表项、括号里的数字都可以抄，抄的时候连同上文的对象名一起抄，"
    "确保单看这一句就能对上问题。证据里确实没有，就只写 NONE。\n"
    "第二步 判定：摘录到了写 SUFFICIENT，摘录不到写 INSUFFICIENT。\n\n"
    "只输出这两行：\n"
    "摘录：<原文或 NONE>\n"
    "判定：<SUFFICIENT 或 INSUFFICIENT>\n\n"
    "【问题】{question}\n\n【证据】\n{evidence}"
)


def parse_verdict(raw: str) -> tuple[bool, str]:
    """从裁判输出里取 (证据是否足够, 摘录原文)。

    判定标签缺失时按"不足"处理（宁可拒答，不要编）；
    判定与摘录自相矛盾时（说 SUFFICIENT 却摘不出原文）也按"不足"处理。
    """
    text = raw or ""
    m = re.search(r"判定[：:\s]*\s*(INSUFFICIENT|SUFFICIENT)", text, re.I)
    if m:
        verdict = m.group(1).upper()
    elif re.search(r"\bINSUFFICIENT\b", text.upper()):
        verdict = "INSUFFICIENT"
    elif re.search(r"\bSUFFICIENT\b", text.upper()):
        verdict = "SUFFICIENT"
    else:
        verdict = "INSUFFICIENT"

    m2 = re.search(r"摘录[：:]\s*([\s\S]*)", text)
    quote = m2.group(1).strip() if m2 else ""
    quote = re.split(r"判定[：:]", quote)[0].strip()
    if verdict == "SUFFICIENT" and (not quote or quote.upper().startswith("NONE")):
        return False, quote
    return verdict == "SUFFICIENT", quote


@dataclass
class Answer:
    question: str
    answer: str
    refused: bool
    citations: list[int]
    chunks: list[dict]
    latency_ms: int
    mode: str
    rerank: bool
    guard: bool
    calls: int = 0
    tokens: int = 0
    cost: float = 0.0
    extra: dict = field(default_factory=dict)


def _evidence_block(chunks: list[dict]) -> str:
    lines = []
    for i, c in enumerate(chunks, 1):
        src = c.get("file_path") or "文档"
        body = (c.get("content") or "").strip()
        lines.append(f"[{i}] 来源：{src}\n{body}")
    return "\n\n".join(lines)


def _parse_citations(text: str, n_chunks: int) -> list[int]:
    seen: list[int] = []
    for m in re.finditer(r"\[(\d{1,2})\]", text):
        k = int(m.group(1))
        if 1 <= k <= n_chunks and k not in seen:
            seen.append(k)
    return seen


def is_refusal(text: str) -> bool:
    if not text:
        return True
    if text.strip().startswith(REFUSAL_TEXT[:5]):
        return True
    head = text[:60]
    return any(h in head for h in REFUSAL_HINTS)


class DocuMind:
    def __init__(self, tag: str = "noglean", embed: str = "local", use_rerank: bool = True):
        self.tag = tag
        self.embed_backend = embed
        self.use_rerank = use_rerank
        self.rag: LightRAG | None = None

    async def open(self) -> "DocuMind":
        storage = ROOT / "rag_storage" / self.tag
        if not storage.exists():
            raise SystemExit(f"[FAIL] 索引不存在：{storage}，先跑 run_index.py --tag {self.tag}")
        fn, dim, name = build_embedder(self.embed_backend)
        self.embed_name = name
        self.rag = LightRAG(
            working_dir=str(storage),
            llm_model_func=llm_func,
            llm_model_name="deepseek-chat",
            llm_model_max_async=4,
            embedding_func=EmbeddingFunc(embedding_dim=dim, max_token_size=8192, func=fn),
            rerank_model_func=local_rerank if self.use_rerank else None,
            default_rerank_timeout=180,  # 首次调用含 ONNX 模型加载，60s 默认值不够
            # 每个实体/关系最多挂几个原文块。默认 5，在 113 块的语料上
            # 会把召回堆到 40~50 块，重排就要 7 秒以上，而真正有用的通常只有前 6 块。
            # 降到 2 之后召回压到 15 块上下，重排 1.5 秒，准确率没有可见下降。
            related_chunk_number=int(os.environ.get("RELATED_CHUNK_NUMBER", "2")),
        )
        await self.rag.initialize_storages()
        await initialize_pipeline_status()
        if self.use_rerank:
            # 预热：把模型加载与首轮 ONNX 编译挪到评测开始之前，
            # 否则第一次查询会撞上 rerank 超时被降级回原始顺序。
            await asyncio.to_thread(get_reranker)
            await local_rerank("预热", ["预热文档"])
        return self

    async def close(self) -> None:
        if self.rag is not None:
            await self.rag.finalize_storages()

    async def ask(
        self,
        question: str,
        mode: str = "mix",
        rerank: bool = True,
        guard: bool = True,
        top_k: int = 10,
        chunk_top_k: int = 6,
    ) -> Answer:
        assert self.rag is not None, "先 await open()"
        t0 = time.perf_counter()
        c0 = USAGE.calls
        p0 = USAGE.prompt_tokens
        q0 = USAGE.completion_tokens
        cost0 = USAGE.cost

        param = QueryParam(
            mode=mode,
            top_k=top_k,
            chunk_top_k=chunk_top_k,
            enable_rerank=bool(rerank and self.use_rerank),
            include_references=True,
        )
        data = await self.rag.aquery_data(question, param=param)
        payload = data.get("data") or {}
        chunks = payload.get("chunks") or []

        def finish(text: str, refused: bool) -> Answer:
            ms = int((time.perf_counter() - t0) * 1000)
            return Answer(
                question=question,
                answer=text,
                refused=refused,
                citations=_parse_citations(text, len(chunks)),
                chunks=chunks,
                latency_ms=ms,
                mode=mode,
                rerank=param.enable_rerank,
                guard=guard,
                calls=USAGE.calls - c0,
                tokens=(USAGE.prompt_tokens - p0) + (USAGE.completion_tokens - q0),
                cost=USAGE.cost - cost0,
                extra={
                    "prompt_tokens": USAGE.prompt_tokens - p0,
                    "completion_tokens": USAGE.completion_tokens - q0,
                    "n_chunks": len(chunks),
                },
            )

        if not chunks:
            return finish(REFUSAL_TEXT, True)

        evidence = _evidence_block(chunks)

        if guard:
            raw = await llm_func(
                JUDGE_PROMPT.format(question=question, evidence=evidence),
                max_tokens=400,
                temperature=0,
            )
            enough, quote = parse_verdict(raw)
            if not enough:
                ans = finish(REFUSAL_TEXT, True)
                ans.extra["judge_quote"] = quote
                return ans
        else:
            quote = ""

        # 裁判判"足够"时手上已经有一句能对上问题的原文，直接交给作答模型当锚点：
        # 否则它要在上万字证据里自己再找一遍，实测会偶发判自己"证据不足"而整段拒答。
        hint = ""
        if quote and not quote.upper().startswith("NONE"):
            hint = "\n\n【与问题直接相关的原文】\n" + quote

        text = await llm_func(
            question,
            system_prompt=SYSTEM_PROMPT + hint + "\n\n【证据】\n" + evidence,
            temperature=0,
        )
        text = (text or "").strip()
        return finish(text, is_refusal(text))


async def _main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("-q", "--question")
    p.add_argument("--file", help="每行一个问题")
    p.add_argument("--tag", default="noglean")
    p.add_argument("--mode", default="mix", choices=["naive", "local", "global", "hybrid", "mix"])
    p.add_argument("--embed", default="local", choices=["bailian", "local"])
    p.add_argument("--no-rerank", action="store_true")
    p.add_argument("--no-guard", action="store_true")
    p.add_argument("--show-chunks", action="store_true")
    args = p.parse_args()

    questions = []
    if args.question:
        questions.append(args.question)
    if args.file:
        questions += [l.strip() for l in Path(args.file).read_text(encoding="utf-8").splitlines() if l.strip()]
    if not questions:
        p.error("给 -q 或 --file")

    dm = await DocuMind(args.tag, args.embed, use_rerank=not args.no_rerank).open()
    try:
        for q in questions:
            a = await dm.ask(q, mode=args.mode, rerank=not args.no_rerank, guard=not args.no_guard)
            print("=" * 70)
            print("问：", q)
            print(f"答：{a.answer}")
            print(f"[{a.latency_ms} ms | {a.calls} 次调用 | {a.tokens} token | 引用 {a.citations or '无'}]")
            if args.show_chunks:
                for i, c in enumerate(a.chunks, 1):
                    print(f"  [{i}] {c.get('content', '')[:80].replace(chr(10), ' ')}")
    finally:
        await dm.close()


if __name__ == "__main__":
    asyncio.run(_main())
