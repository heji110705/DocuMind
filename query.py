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
import logging
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
from index_stats import count_chunks, index_stats  # noqa: E402
# 提示词全部在 prompts.py。这里必须把"用到的每一个名字"都 import 进来——
# 少导一个不会在 import 时报错，只会在真正走到那条路径时抛 NameError。
# 上一版就漏了 JUDGE_PROMPT 和 evidence_block，表现为**两个模式都答不出**
# （快速模式也判证据），而单测全绿、/healthz 正常、检索阶段日志一切正常。
# 这条边界上的教训写成了 tests/test_answer_path.py：把 judge/condense 真的调一次。
from prompts import (CONDENSE_PROMPT, JUDGE_PROMPT, build_answer_prompt,  # noqa: E402,F401
                     build_history_block, clean_rewrite, evidence_block,
                     is_refusal, parse_verdict)
from chunk_dedup import dedupe, threshold_from_env as dedup_threshold  # noqa: E402
from context_budget import apply_budget, budget_from_env, cap_chunk_top_k  # noqa: E402
from prompts import parse_citations as _parse_citations  # noqa: E402
from prompts import repair_citations as _repair_citations  # noqa: E402
from llm_client import llm_func, track_usage  # noqa: E402
# 常量集中在 query_consts，这里的 import 是"对外继续暴露"的兼容层：
# 老代码里的 `from query import DEFAULT_TOP_K` 仍然有效，但真实定义只有一处。
from query_consts import (DEFAULT_CHUNK_TOP_K, DEFAULT_TOP_K,  # noqa: E402,F401
                          MAX_QUESTION, MODES, REFUSAL_HINTS, REFUSAL_TEXT)
from rerankers import get_model as get_reranker  # noqa: E402
from rerankers import local_rerank  # noqa: E402

logger = logging.getLogger("documind.query")

# ------------------------------------------------------------------ 数据结构
@dataclass
class Answer:
    """一次问答的完整结果，也是 HTTP 层与评测脚本共用的契约。

    拆 prompts.py 的时候这个类被整段弄丢过一次：`ask()` 里还在 `return Answer(...)`，
    但类本身已经不在了。表现是**每一道题都在 return 那一行抛 NameError**，
    而检索、成本记账、日志全部正常——从日志上看像是"跑完了但没结果"。

    它值得单独写一段注释的原因：这是一个"被两个模块同时依赖的数据结构"，
    删掉它不会有 import 错误（因为它只在本文件内部使用），
    只有真正跑一次问答才会暴露。`tests/test_answer_path.py` 现在覆盖了这条路径。
    """

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


@dataclass
class Ingest:
    """一次增量入库的结果。字段刻意做得比"成功/失败"细，
    因为界面上要显示"这次进了几块、花了多少钱"，而不只是一个对勾。"""

    file_path: str
    chars: int
    chunks_before: int
    chunks_after: int
    elapsed_ms: int
    cost: float
    calls: int
    duplicate: bool = False

    @property
    def chunks_added(self) -> int:
        return max(0, self.chunks_after - self.chunks_before)

    def as_dict(self) -> dict:
        return {
            "file_path": self.file_path,
            "chars": self.chars,
            "chunks_added": self.chunks_added,
            "chunks_total": self.chunks_after,
            "elapsed_ms": self.elapsed_ms,
            "cost_cny": round(self.cost, 6),
            "calls": self.calls,
            "duplicate": self.duplicate,
        }


class DocuMind:
    def __init__(self, tag: str = "noglean", embed: str = "local", use_rerank: bool = True):
        self.tag = tag
        self.embed_backend = embed
        self.use_rerank = use_rerank
        self.rag: LightRAG | None = None
        # LCEL 链按 (top_k, chunk_top_k, rerank) 缓存：参数变了要重建，
        # 否则同一个 DocuMind 实例上先来一个 top_k=10、再来一个 top_k=20 的请求，
        # 后者会静默沿用前者的候选池。
        self._lcel_chain = None
        self._lcel_key: tuple | None = None
        # 检索锁：由服务端注入（见 app/main.py）。评测和 CLI 不设，
        # 因为它们本来就是串行/受控并发，加锁只会把跑批拖长。
        self.lock: asyncio.Lock | None = None

    async def open(self, create_if_missing: bool = False) -> "DocuMind":
        """加载索引。

        create_if_missing=True 是给多租户和上传功能用的：新租户第一次进来时
        索引目录还不存在，此时建一个**空索引**（LightRAG 允许空库启动），
        然后由他上传的文档把内容填进去。默认 False——本地演示要是索引没建好，
        报错比悄悄起一个空服务好，否则界面上每一问都是"证据不足"。
        """
        storage = ROOT / "rag_storage" / self.tag
        if not storage.exists():
            if not create_if_missing:
                raise SystemExit(f"[FAIL] 索引不存在：{storage}，先跑 run_index.py --tag {self.tag}")
            storage.mkdir(parents=True, exist_ok=True)
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
            # LLM 响应缓存的开关。默认开（省时间省钱），设 LLM_CACHE=0 关掉。
            #
            # 为什么需要一个开关，而不是"反正有缓存更好"：LightRAG 会把关键词抽取
            # 的结果落盘到 kv_store_llm_response_cache.json，同一道题第二次跑就不再
            # 调模型。于是**评测跑出来的 unit cost 取决于缓存状态**，而不是系统本身。
            # 实测同一配置：缓存热时 124 题全部只 1 次调用 / 3,353 token，
            # 冷启动时 2 次调用 / 4,383 token——差 31%。
            # 不把两种口径分开测，成本表就是一堆不可比的数字。
            enable_llm_cache=os.environ.get("LLM_CACHE", "1") != "0",
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

    async def retrieve(
        self,
        question: str,
        mode: str = "mix",
        rerank: bool = True,
        top_k: int = DEFAULT_TOP_K,
        chunk_top_k: int = DEFAULT_CHUNK_TOP_K,
    ) -> tuple[list[dict], bool]:
        """只取证据，不调作答模型。返回 (chunks, 实际是否启用了重排)。

        进来先把 chunk_top_k 压到预算上限：这是"少召回"，比事后裁剪更省
        （连重排都要处理的候选一起少了）。开关关着时这一行是恒等变换。

        CLI、流式接口、评测都走这一个方法，保证三个入口召回的东西完全一致。
        重排是 CPU 独占资源，服务端注入 self.lock 之后这一段会串行；
        LLM 调用不在锁里，所以多个请求仍然可以同时判定和作答。
        """
        chunk_top_k = cap_chunk_top_k(chunk_top_k, budget_from_env())
        assert self.rag is not None, "先 await open()"
        if self.lock is None:
            return await self._retrieve(question, mode, rerank, top_k, chunk_top_k)
        async with self.lock:
            return await self._retrieve(question, mode, rerank, top_k, chunk_top_k)

    async def _retrieve(
        self, question: str, mode: str, rerank: bool, top_k: int, chunk_top_k: int
    ) -> tuple[list[dict], bool]:
        """按 RETRIEVAL_IMPL 分发。默认 lcel，设成 lightrag 即回到改动前的行为。"""
        impl = (os.environ.get("RETRIEVAL_IMPL") or "lcel").strip().lower()
        if mode == "mix" and impl == "lcel":
            try:
                return await self._retrieve_lcel(question, rerank, top_k, chunk_top_k)
            except Exception as e:                     # noqa: BLE001
                # 编排层出错不该让整个问答不可用：记一行、退回原实现。
                # 注意这会让 A/B 的数字悄悄退化成 lightrag，所以日志足够显眼。
                logger.warning("[lcel] 编排失败，退回 LightRAG mix：%s: %s", type(e).__name__, e)
        return await self._retrieve_lightrag(question, mode, rerank, top_k, chunk_top_k)

    async def _retrieve_lcel(
        self, question: str, rerank: bool, top_k: int, chunk_top_k: int
    ) -> tuple[list[dict], bool]:
        """LCEL 版 mix：三条检索并发 → 合并去重 → 重排 → 截断。"""
        assert self.rag is not None, "先 await open()"
        from retrieval_lcel import build_chain

        key = (top_k, chunk_top_k, bool(rerank and self.use_rerank))
        if self._lcel_chain is None or self._lcel_key != key:
            self._lcel_chain = build_chain(
                rag=self.rag,
                top_k=top_k,
                chunk_top_k=chunk_top_k,
                enable_rerank=key[2],
                rerank_func=local_rerank,
            )
            self._lcel_key = key
        out = await self._lcel_chain.ainvoke({"question": question})
        return out.get("chunks") or [], bool(out.get("used_rerank"))

    async def _retrieve_lightrag(
        self, question: str, mode: str, rerank: bool, top_k: int, chunk_top_k: int
    ) -> tuple[list[dict], bool]:
        param = QueryParam(
            mode=mode,
            top_k=top_k,
            chunk_top_k=chunk_top_k,
            enable_rerank=bool(rerank and self.use_rerank),
            include_references=True,
        )
        data = await self.rag.aquery_data(question, param=param)
        payload = (data or {}).get("data") or {}
        return payload.get("chunks") or [], param.enable_rerank

    async def judge(self, question: str, chunks: list[dict]) -> tuple[bool, str]:
        """证据充分性判定，返回 (是否足够, 摘录的原文或 NONE)。"""
        raw = await llm_func(
            JUDGE_PROMPT.format(question=question, evidence=evidence_block(chunks)),
            max_tokens=400,
            temperature=0,
        )
        return parse_verdict(raw)

    async def condense(self, question: str, history: list[dict] | None) -> str:
        """把带指代的问题改写成独立问题。没有历史就直接返回原句（零成本）。

        这条路径同样受 track_usage 记账——改写的钱也是钱，
        界面上"本次成本"必须包含它，否则多轮一开成本就少算一次调用。
        """
        if not history:
            return question
        raw = await llm_func(
            CONDENSE_PROMPT.format(history=build_history_block(history), question=question),
            max_tokens=200,
            temperature=0,
        )
        return clean_rewrite(raw, question)

    async def ingest(self, text: str, file_path: str) -> Ingest:
        """把一篇文档增量插进当前索引，返回新增块数与真实开销。

        为什么不是"重建索引"：122 块的语料重建一次要 3 分钟、¥1 左右，
        而插一篇 2 千字的文档只要十几秒、几分钱。演示时"上传→立刻提问"
        能不能成立，全靠这一步是不是增量的。

        计数为什么要 flush 之后再读：LightRAG 的 JsonKVStorage 平时只改内存里的
        共享字典，磁盘上的 json 由 index_done_callback 落盘。直接在 ainsert 之后
        读文件，读到的是**上一次**的快照，于是"新增 0 块"——看起来像没插进去。
        """
        assert self.rag is not None, "先 await open()"
        text = (text or "").strip()
        if len(text) < 20:
            raise ValueError("文档内容太少（<20 字），解析可能失败")

        before = count_chunks(self.tag)
        t0 = time.perf_counter()
        with track_usage() as used:
            if self.lock is not None:
                # 插入会改共享存储，和检索串行，避免查到写了一半的索引。
                async with self.lock:
                    await self.rag.ainsert(text, file_paths=file_path)
                    await self.rag.text_chunks.index_done_callback()
            else:
                await self.rag.ainsert(text, file_paths=file_path)
                await self.rag.text_chunks.index_done_callback()
        dt = int((time.perf_counter() - t0) * 1000)
        after = count_chunks(self.tag)
        return Ingest(
            file_path=file_path,
            chars=len(text),
            chunks_before=before,
            chunks_after=after,
            elapsed_ms=dt,
            cost=used.cost,
            calls=used.calls,
            # 内容哈希相同会被 LightRAG 去重，此时块数不变，这是正常行为不是失败。
            duplicate=after <= before,
        )

    def stats(self) -> dict:
        return index_stats(self.tag)

    async def delete(self, doc_id: str) -> dict:
        """按 doc_id 删除一篇文档（含它的 chunk 与只属于它的实体）。

        为什么要做删除：上传是单向门的话，演示时传错一份文件就只能重建索引。
        LightRAG 的 adelete_by_doc_id 会连带清掉只有这篇文档引用到的实体，
        但不是事务性的——删完要重新数一遍规模，别拿删之前的数字当真。
        """
        assert self.rag is not None, "先 await open()"
        before = count_chunks(self.tag)
        if self.lock is not None:
            async with self.lock:
                await self.rag.adelete_by_doc_id(doc_id)
                await self.rag.text_chunks.index_done_callback()
        else:
            await self.rag.adelete_by_doc_id(doc_id)
            await self.rag.text_chunks.index_done_callback()
        after = count_chunks(self.tag)
        return {"doc_id": doc_id, "chunks_removed": max(0, before - after),
                "chunks_total": after}


    async def ask(
        self,
        question: str,
        mode: str = "mix",
        rerank: bool = True,
        guard: bool = True,
        top_k: int = DEFAULT_TOP_K,
        chunk_top_k: int = DEFAULT_CHUNK_TOP_K,
        history: list[dict] | None = None,
    ) -> Answer:
        t0 = time.perf_counter()

        # 整个请求（含检索期的关键词抽取、多轮改写、判定）都在自己的账本里记账。
        # 用全局计数器相减在并发跑批时会串账，实测单题能算出 9 次调用、28k prompt token。
        with track_usage() as used:
            # 先做指代消解：这一步必须在检索之前，否则"那它的默认值呢"这种问题
            # 向量化之后没有主语，召回的是"默认值"字面最像的块。
            standalone = await self.condense(question, history)

            chunks, used_rerank = await self.retrieve(
                standalone, mode=mode, rerank=rerank, top_k=top_k, chunk_top_k=chunk_top_k
            )
            # 顺序：先去重、再裁预算。
            # 两者都必须在这里（judge、提示词、引用编号、响应 chunks 共用这一份列表）；
            # 若只在拼提示词时处理，答案里的 [n] 会指向处理之后错位的编号。
            chunks, dedup_stats = dedupe(chunks, dedup_threshold())
            chunks, budget_stats = apply_budget(chunks, budget_from_env())

            def finish(text: str, refused: bool) -> Answer:
                ms = int((time.perf_counter() - t0) * 1000)
                # 先按正常口径解析；若一个合法引用都没有但答案里确有 [n]，
                # 按"就近映射到已有块"兜底，并如实标记 citation_repair。
                cites, oob, repaired = _repair_citations(
                    text, _parse_citations(text, len(chunks)), len(chunks)
                )
                return Answer(
                    question=question,
                    answer=text,
                    refused=refused,
                    citations=cites,
                    chunks=chunks,
                    latency_ms=ms,
                    mode=mode,
                    rerank=used_rerank,
                    guard=guard,
                    calls=used.calls,
                    tokens=used.total_tokens,
                    cost=used.cost,
                    extra={
                        "prompt_tokens": used.prompt_tokens,
                        "completion_tokens": used.completion_tokens,
                        "n_chunks": len(chunks),
                        "standalone": standalone,
                        "history_used": len(history or []),
                        "citation_oob": oob,
                        "citation_repair": repaired,
                        "model_used": used.model_used,
                        "budget": budget_stats,
                        "dedup": dedup_stats,
                    },
                )

            if not chunks:
                return finish(REFUSAL_TEXT, True)

            quote = ""
            if guard:
                enough, quote = await self.judge(standalone, chunks)
                if not enough:
                    ans = finish(REFUSAL_TEXT, True)
                    ans.extra["judge_quote"] = quote
                    return ans

            prompt, system = build_answer_prompt(standalone, chunks, quote)
            text = await llm_func(prompt, system_prompt=system, temperature=0)
            text = (text or "").strip()
            return finish(text, is_refusal(text))


async def _main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("-q", "--question")
    p.add_argument("--file", help="每行一个问题")
    p.add_argument("--tag", default="prod", help="索引目录名（rag_storage/<tag>）")
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
