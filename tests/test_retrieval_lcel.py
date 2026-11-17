"""LCEL 编排的单元测试。

分两层：
1. 纯函数（去重/裁剪/保序）——不依赖 langchain 与 lightrag，任何环境都能跑；
2. LCEL 链本身——用假 rag + 假重排跑一次，验证"三条并行 → 合并 → 重排"的接线。
   这条需要 langchain_core，CI 的最小依赖环境里没有，所以 importorskip。
"""
from __future__ import annotations

import pytest

from retrieval_lcel import build_chain, content_hash, merge_branches, order_by_rerank


def _chunk(text: str, cid: str = "", fp: str = "a.md") -> dict:
    return {"chunk_id": cid or text[:6], "content": text, "file_path": fp}


# ---------------------------------------------------------------- 纯函数
def test_content_hash_ignores_id_and_whitespace() -> None:
    """同一段正文、不同 chunk_id → 同一个键（去重靠正文不靠 id）。"""
    a = _chunk("精装与平装的价差为 18.65 元", cid="chunk-aaa")
    b = _chunk("  精装与平装的价差为 18.65 元\n", cid="chunk-bbb")
    assert content_hash(a) == content_hash(b)
    assert content_hash(a) != content_hash(_chunk("另一段话"))


def test_merge_dedupes_across_branches_and_keeps_order() -> None:
    naive = [_chunk("A"), _chunk("B")]
    local = [_chunk("B"), _chunk("C")]      # B 与 naive 重复
    glob = [_chunk("D")]

    merged = merge_branches([naive, local, glob], limit=10)
    assert [c["content"] for c in merged] == ["A", "B", "C", "D"]


def test_merge_respects_limit() -> None:
    merged = merge_branches([[_chunk("A"), _chunk("B"), _chunk("C")]], limit=2)
    assert [c["content"] for c in merged] == ["A", "B"]


def test_merge_skips_malformed_items() -> None:
    merged = merge_branches([["不是 dict", _chunk("A"), {"content": ""}]], limit=5)
    assert [c["content"] for c in merged] == ["A"]


def test_order_by_rerank_reorders_and_truncates() -> None:
    chunks = [_chunk("A"), _chunk("B"), _chunk("C")]
    ranked = [{"index": 2, "relevance_score": 9.0}, {"index": 0, "relevance_score": 5.0}]
    out = order_by_rerank(chunks, ranked, top_n=2)
    assert [c["content"] for c in out] == ["C", "A"]


def test_order_by_rerank_ignores_bad_index_and_backfills() -> None:
    """越界/非整数 index 不该把请求带崩，也不能把证据截没。"""
    chunks = [_chunk("A"), _chunk("B")]
    ranked = [{"index": 99}, {"index": "x"}, {"index": 1, "relevance_score": 1.0}]
    out = order_by_rerank(chunks, ranked, top_n=2)
    assert [c["content"] for c in out] == ["B", "A"]


# ---------------------------------------------------------------- LCEL 链
class _FakeRag:
    """只实现 aquery_data，记录每个 mode 被调了几次。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def aquery_data(self, question: str, param=None):
        mode = getattr(param, "mode", "")
        self.calls.append(mode)
        return {
            "data": {
                "chunks": [
                    {"chunk_id": f"{mode}-1", "content": f"{mode} 的命中 A", "file_path": "x.md"},
                    {"chunk_id": f"{mode}-2", "content": "三条分支都有的一句话", "file_path": "y.md"},
                ]
            }
        }


async def _fake_rerank(query, docs, top_n=None, **_):
    return [{"index": i, "relevance_score": float(len(docs) - i)} for i in range(len(docs))]


def test_chain_runs_three_branches_and_merges() -> None:
    pytest.importorskip("langchain_core", reason="CI 最小依赖里没有 langchain-core")
    import asyncio

    rag = _FakeRag()
    chain = build_chain(
        rag=rag, top_k=10, chunk_top_k=6, enable_rerank=False, rerank_func=_fake_rerank
    )
    out = asyncio.run(chain.ainvoke({"question": "精装比平装贵多少"}))

    assert sorted(rag.calls) == ["global", "local", "naive"], "三个 mode 都必须被调用"
    contents = [c["content"] for c in out["chunks"]]
    assert contents.count("三条分支都有的一句话") == 1, "重复正文必须在合并时去掉"
    assert len(contents) == 4 and out["used_rerank"] is False


def test_chain_uses_rerank_when_enabled() -> None:
    pytest.importorskip("langchain_core", reason="CI 最小依赖里没有 langchain-core")
    import asyncio

    rag = _FakeRag()
    chain = build_chain(
        rag=rag, top_k=10, chunk_top_k=2, enable_rerank=True, rerank_func=_fake_rerank
    )
    out = asyncio.run(chain.ainvoke({"question": "q"}))

    assert out["used_rerank"] is True
    assert len(out["chunks"]) == 2, "重排结果必须按 chunk_top_k 截断"
