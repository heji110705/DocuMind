"""诊断：LCEL 路径的重排到底有没有生效。

背景：昨晚的 A/B 用 `--configs mix`，而 run_eval 里 "mix" 的定义是
`rerank=False, guard=False`——也就是说那一轮的**两个实现都没开重排**，
80.9% 是"无重排"的数，不能拿去和线上 95.7%（rerank+guard）比。
但"配置里没开"不等于"开了就有效"，所以这个脚本要回答的是后者：
把重排打开，看 LCEL 与 LightRAG 各自喂给 rerank 的候选、拿回来的分数、
以及重排前后的顺序。

做法：把 `local_rerank` 包一层记录器，在构造 DocuMind **之前**替换掉
模块全局名——两个路径的接线方式不同（LightRAG 在 __init__ 时收走函数对象，
LCEL 在 build_chain 时收走），所以必须在 open() 之前换，事后换只影响 LCEL。

不调作答模型；mix 模式内部会做关键词抽取（走 LLM），2 题 × 2 实现的量很小。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# 脚本可能被 docker cp 到 /tmp 再执行：这时 __file__ 的父目录不是项目根，
# 必须把 cwd（-w /app）也加进来。
for _cand in (str(Path(__file__).resolve().parent.parent), os.getcwd()):
    if _cand not in sys.path:
        sys.path.insert(0, _cand)

import query as qmod  # noqa: E402
import rerankers  # noqa: E402

RECORDS: list[dict] = []
_ORIG = rerankers.local_rerank
CONTENT_2_PATH: dict[str, str] = {}


def _load_path_map() -> None:
    fp = Path("rag_storage/prod/vdb_chunks.json")
    raw = json.loads(fp.read_text(encoding="utf-8"))
    rows = raw.get("data") if isinstance(raw, dict) else raw
    for r in rows:
        body = (r.get("content") or "").strip()
        if body:
            CONTENT_2_PATH[body] = r.get("file_path") or "?"


def _label(doc: str) -> str:
    """块 → "文件名 | 前 40 字"。命中不到就给个显式标记，不要假装是同一个块。"""
    body = (doc or "").strip()
    path = CONTENT_2_PATH.get(body)
    tail = "".join(body.split())[:40]
    if path:
        return f"{Path(path).name} | {tail}"
    return f"(未匹配到块) | {tail}"


async def _recorder(query: str, documents: list[str], top_n=None, extra_body=None):
    ranked = await _ORIG(query, documents, top_n=top_n, extra_body=extra_body)
    RECORDS.append(
        {
            "query": query,
            "n_docs": len(documents),
            "docs": list(documents),
            "ranked": ranked,
        }
    )
    return ranked


async def main() -> None:
    questions = json.loads(Path("eval_set_v2.json").read_text(encoding="utf-8"))["items"]
    pick = [q for q in questions if q["id"] in ("Q016", "Q025")]
    impl = (os.environ.get("RETRIEVAL_IMPL") or "lcel").strip().lower()

    _load_path_map()
    # 必须在构造之前替换：LightRAG 在 __init__ 里就把函数对象收走了
    qmod.local_rerank = _recorder
    rerankers.local_rerank = _recorder

    dm = await qmod.DocuMind("prod", "local", use_rerank=True).open()
    print(f"### RETRIEVAL_IMPL={impl} | use_rerank={dm.use_rerank}")

    for item in pick:
        RECORDS.clear()
        chunks, used = await dm.retrieve(
            item["q"], mode="mix", rerank=True, top_k=20, chunk_top_k=6
        )
        print(f"\n== {item['id']}: {item['q']}")
        print(f"   used_rerank={used} | 返回块数={len(chunks)} | rerank 调用次数={len(RECORDS)}")
        for k, rec in enumerate(RECORDS):
            docs = rec["docs"]
            ranked = rec["ranked"]
            scores = [r["relevance_score"] for r in ranked]
            order_before = list(range(min(5, len(docs))))
            order_after = [r["index"] for r in ranked[:5]]
            same = order_before == order_after
            print(
                f"   调用#{k}: 候选={len(docs)} 分数范围=[{min(scores):.4f}, {max(scores):.4f}] "
                f"分数全同={len(set(scores)) == 1} 顺序未变={same}"
            )
            print("     —— 重排前 top5 ——")
            for i in order_before:
                print(f"       {i}  {_label(docs[i])}")
            print("     —— 重排后 top5 ——")
            for r in ranked[:5]:
                print(f"       {r['index']}  score={r['relevance_score']:.4f}  {_label(docs[r['index']])}")


if __name__ == "__main__":
    asyncio.run(main())
