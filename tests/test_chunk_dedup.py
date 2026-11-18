"""检索期去重的单元测试（纯逻辑）。"""

from __future__ import annotations

import pytest

from chunk_dedup import dedupe, similarity, threshold_from_env


def _c(text: str, path: str = "a.md") -> dict:
    return {"content": text, "file_path": path}


class TestSimilarity:
    def test_identical(self):
        assert similarity("订单数必须去重", "订单数必须去重") == 1.0

    def test_unrelated(self):
        assert similarity("订单数必须去重", "今天的天气不错") < 0.1

    def test_sliding_window_neighbours_are_not_duplicates(self):
        """同一张表被切两段：字符相似度高但高不过 0.85，不该被判成重复。

        这是本语料的真实形态（insightpilot-docs-plus.md 那张结果表切成 32 块），
        阈值定低了会把这些行当重复删掉，等于丢数据。
        """
        a = "| Q30 | T3 | ok | 投影列数不符（列数不同：4 vs 5） |"
        b = "| Q32 | T4 | ok | 行数/分组粒度不符（行数不同：3 vs 2） |"
        assert similarity(a, b) < 0.85


class TestDedupe:
    def test_disabled_is_noop(self):
        chunks = [_c("同样的内容"), _c("同样的内容")]
        out, stats = dedupe(chunks, None)
        assert out is chunks and stats["enabled"] is False

    def test_drops_exact_duplicate_within_doc(self):
        chunks = [_c("订单数必须去重，否则复购会重复计数", "a.md"),
                  _c("订单数必须去重，否则复购会重复计数", "a.md")]
        out, stats = dedupe(chunks, 0.85)
        assert len(out) == 1 and stats["n_out"] == 1
        assert stats["dropped"] and stats["dropped"][0]["sim"] == 1.0

    def test_keeps_cross_document_lookalikes(self):
        """跨文档的相似表述不合并：同一概念的不同措辞往往各有信息。"""
        body = "订单数必须按 order_id 去重"
        chunks = [_c(body, "a.md"), _c(body, "b.md")]
        out, _ = dedupe(chunks, 0.85)
        assert len(out) == 2

    def test_keeps_distinct_chunks(self):
        chunks = [_c("CTR 的分母是曝光数", "a.md"), _c("ARPPU 的分母是付费用户数", "a.md")]
        out, _ = dedupe(chunks, 0.85)
        assert len(out) == 2

    def test_preserves_order_and_original_objects(self):
        chunks = [_c("AAAA 不同内容", "a.md"), _c("AAAA 不同内容", "a.md"), _c("BBBB 另一段", "a.md")]
        out, _ = dedupe(chunks, 0.85)
        assert [c["content"] for c in out] == ["AAAA 不同内容", "BBBB 另一段"]
        assert out[0] is chunks[0], "不该复制对象，下游还要用原引用"


class TestEnv:
    def test_off_by_default(self, monkeypatch):
        monkeypatch.delenv("DEDUP", raising=False)
        assert threshold_from_env() is None

    def test_threshold_override(self, monkeypatch):
        monkeypatch.setenv("DEDUP", "1")
        monkeypatch.setenv("DEDUP_THRESHOLD", "0.9")
        assert threshold_from_env() == 0.9

    def test_bad_threshold_falls_back(self, monkeypatch):
        monkeypatch.setenv("DEDUP", "1")
        monkeypatch.setenv("DEDUP_THRESHOLD", "高")
        assert threshold_from_env() == 0.85

    def test_clamped(self, monkeypatch):
        monkeypatch.setenv("DEDUP", "1")
        monkeypatch.setenv("DEDUP_THRESHOLD", "0.1")
        assert threshold_from_env() == 0.5
