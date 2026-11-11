"""用 LightRAG 索引一份文档，输出 chunk/实体/关系数量与耗时。

用法：
    python run_index.py --input seed/xxx.pdf --pages 10
    python run_index.py --input seed/xxx.pdf --pages 10 --gleaning 0 --tag noglean
    python run_index.py --input seed/xxx.pdf --dry-run          # 只看切块，不调 API

设计说明：
- 只取前 N 页，避免调参阶段把钱烧在整本书上（见 docs 里的成本测算）。
- 每次运行用独立 working_dir（按 tag 区分），互不污染，方便对比 gleaning 开关。
"""

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

import numpy as np
from dotenv import load_dotenv
from lightrag import LightRAG
from lightrag.kg.shared_storage import initialize_pipeline_status
from lightrag.utils import EmbeddingFunc

from chunkers import markdown_chunker
from embedders import build_embedder, default_backend
from llm_client import USAGE, llm_func, reset_usage

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")


def read_pdf(path: Path, pages: int) -> str:
    import pymupdf as fitz

    doc = fitz.open(path)
    n = min(pages, len(doc))
    parts = []
    for i in range(n):
        t = doc[i].get_text().strip()
        if t:
            parts.append(f"# 第 {i + 1} 页\n\n{t}")
    doc.close()
    return "\n\n".join(parts)


def load_text(path: Path, pages: int) -> str:
    if path.suffix.lower() == ".pdf":
        return read_pdf(path, pages)
    text = path.read_text(encoding="utf-8", errors="ignore")
    limit = pages * 1200  # md 没有页的概念，按 1200 字≈1 页折算
    if len(text) > limit:
        text = text[:limit] + "\n\n（以下内容已按 --pages 截断）"
    return text


async def count_graph(rag: LightRAG):
    """尽量稳健地数出实体与关系数量，不同存储实现走不同分支。"""
    try:
        labels = await rag.chunk_entity_relation_graph.get_all_labels()
        entities = len(labels or [])
    except Exception:
        entities = -1
    edges = -1
    try:
        g = getattr(rag.chunk_entity_relation_graph, "_graph", None)
        if g is not None:
            entities = g.number_of_nodes()
            edges = g.number_of_edges()
    except Exception:
        pass
    return entities, edges


async def amain(args):
    text = load_text(Path(args.input), args.pages)
    chars = len(text)
    print(f"输入     : {args.input}")
    print(f"取用页数 : {args.pages}")
    print(f"字符数   : {chars:,}  估算 {chars * 0.6 / 1000:.1f}k token")
    est_chunks = max(1, round(chars * 0.6 / 1100))
    print(f"预计块数 : ~{est_chunks}（chunk 1200 / overlap 100）")
    print(f"预计调用 : ~{est_chunks * (1 + args.gleaning)} 次 LLM")
    print("-" * 62)

    if chars < 200:
        sys.exit("[FAIL] 提取到的文本太少，检查 PDF 是不是扫描件（需要 OCR）")

    if args.dry_run:
        out = ROOT / "data" / "tmp" / f"{Path(args.input).stem}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"[DRY] 已把提取文本写到 {out}，可先人工检查质量")
        return

    if not os.environ.get("LLM_API_KEY"):
        sys.exit("[FAIL] .env 里缺少 LLM_API_KEY（抽取实体要用对话模型）")

    embed_fn, embed_dim, embed_name = build_embedder(args.embed)
    print(f"embedding: {embed_name}（{embed_dim} 维）")

    reset_usage()
    storage = ROOT / "rag_storage" / args.tag
    storage.mkdir(parents=True, exist_ok=True)

    rag = LightRAG(
        working_dir=str(storage),
        llm_model_func=llm_func,
        llm_model_name=os.environ["LLM_MODEL"],
        llm_model_max_async=args.concurrency,
        embedding_func=EmbeddingFunc(
            embedding_dim=embed_dim,
            max_token_size=8192,
            func=embed_fn,
        ),
        entity_extract_max_gleaning=args.gleaning,
        chunk_token_size=1200,
        chunk_overlap_token_size=100,
        # 关掉开关时退回 LightRAG 默认定长切块，用于做切块策略对照
        chunking_func=None if args.chunker == "default" else markdown_chunker,
    )

    await rag.initialize_storages()
    await initialize_pipeline_status()

    t0 = time.perf_counter()
    try:
        await rag.ainsert(text)
    except Exception as e:
        msg = str(e).replace("\n", " ")
        print(f"[FAIL] 索引失败：{type(e).__name__}")
        print(f"       {msg[:500]}")
        print("-" * 62)
        low = msg.lower()
        if "403" in msg or "denied" in low:
            print("诊断：embedding 或 LLM 模型未开通。先跑 check_embedding.py。")
        elif "429" in msg:
            print("诊断：限流。把 --concurrency 降到 2，等一分钟重试。")
        elif "dimension" in low or "shape" in low:
            print("诊断：向量维度不一致。核对 .env 的 EMBEDDING_DIM。")
        sys.exit(1)
    elapsed = time.perf_counter() - t0

    entities, edges = await count_graph(rag)
    print(f"[OK] 索引完成，耗时 {elapsed:.1f}s")
    print(f"     实体 {entities}，关系 {edges}")
    print(f"     落盘 {storage}")
    for line in USAGE.lines():
        print(f"     {line}")
    print("-" * 62)
    print("把实际数字记到 README 的实测表里 —— 简历上写自己的账单，不写估算。")
    await rag.finalize_storages()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=False, help="PDF 或 md 路径")
    p.add_argument("--pages", type=int, default=10, help="只取前 N 页（默认 10）")
    p.add_argument("--gleaning", type=int, default=0, choices=[0, 1],
                   help="0=每块 1 次调用（省钱），1=每块 2 次（更全）")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--tag", default="default", help="索引落盘子目录名，用于对比实验")
    p.add_argument("--chunker", default="default", choices=["default", "markdown"],
                   help="default=LightRAG 定长切块（实测更优，见 README 消融）；"
                        "markdown=表格不切断/带章节路径（实验项，未跑赢默认）")
    p.add_argument("--embed", default=default_backend(), choices=["bailian", "local"],
                   help="embedding 后端：bailian=云端，local=本地 fastembed")
    p.add_argument("--dry-run", action="store_true", help="只提取文本，不调 API")
    args = p.parse_args()
    if not args.input:
        p.error("必须指定 --input")
    asyncio.run(amain(args))


if __name__ == "__main__":
    main()
