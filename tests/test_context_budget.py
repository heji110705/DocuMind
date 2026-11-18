"""上下文预算的单元测试（纯逻辑，不依赖 fastapi/lightrag）。

这些用例守的是三件事：**默认必须关闭**（否则 README §2.1 的数字全部作废）、
**不能把证据裁光**（否则拒答率上涨会被误读成"预算有效但伤质量"）、
**不能改入参**（调用方还要用原始对象拼响应）。
"""

from __future__ import annotations

import pytest

from context_budget import Budget, apply_budget, budget_from_env, cap_chunk_top_k


def _chunk(text: str, path: str = "a.md") -> dict:
    return {"content": text, "file_path": path}


class TestEnv:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("CTX_BUDGET", raising=False)
        assert budget_from_env() is None

    @pytest.mark.parametrize("value", ["0", "", "true", "yes", "off"])
    def test_only_exactly_one_enables(self, monkeypatch, value):
        monkeypatch.setenv("CTX_BUDGET", value)
        assert budget_from_env() is None

    def test_defaults_when_enabled(self, monkeypatch):
        monkeypatch.setenv("CTX_BUDGET", "1")
        for k in ("CTX_MAX_CHUNKS", "CTX_MAX_TOTAL_CHARS", "CTX_CHUNK_MAX_CHARS"):
            monkeypatch.delenv(k, raising=False)
        b = budget_from_env()
        assert b == Budget(
            max_chunks=4, max_total_chars=3000, chunk_max_chars=800, max_chunk_top_k=6
        )

    def test_bad_number_falls_back(self, monkeypatch):
        monkeypatch.setenv("CTX_BUDGET", "1")
        monkeypatch.setenv("CTX_MAX_CHUNKS", "四")
        assert budget_from_env().max_chunks == 4


class TestApply:
    def test_none_budget_is_noop(self):
        chunks = [_chunk("x" * 5000)]
        out, stats = apply_budget(chunks, None)
        assert out is chunks
        assert stats["enabled"] is False and stats["n_out"] == 1

    def test_caps_chunk_count(self):
        chunks = [_chunk("内容" * 10) for _ in range(6)]
        out, stats = apply_budget(chunks, Budget(max_chunks=2, max_total_chars=None, chunk_max_chars=None))
        assert len(out) == 2
        assert stats["dropped"] == 4

    def test_clips_long_chunk_at_sentence_end(self):
        body = "第一句话。" + "填充" * 400 + "。结尾。"
        out, stats = apply_budget(
            [_chunk(body)], Budget(max_chunks=5, max_total_chars=None, chunk_max_chars=50)
        )
        assert len(out[0]["content"]) <= 50
        assert out[0]["content"].endswith("。"), "截断应落在句末，不能切在词中间"
        assert stats["clipped"] == 1

    def test_total_chars_stops_adding(self):
        chunks = [_chunk("a" * 300) for _ in range(6)]
        out, _ = apply_budget(
            chunks, Budget(max_chunks=None, max_total_chars=700, chunk_max_chars=None)
        )
        assert len(out) == 2  # 300 + 300 = 600 ≤ 700，第 3 块再放就是 900，装不下
        assert sum(len(c["content"]) for c in out) == 600

    def test_keeps_one_chunk_even_if_over_budget(self):
        """首块就超预算时也必须留一块，否则 judge 会判"证据不足"直接拒答。"""
        out, _ = apply_budget(
            [_chunk("a" * 5000), _chunk("b" * 10)],
            Budget(max_chunks=5, max_total_chars=100, chunk_max_chars=None),
        )
        assert len(out) == 1

    def test_input_not_mutated(self):
        original = _chunk("很长" * 500)
        before = original["content"]
        apply_budget([original], Budget(max_chunks=1, max_total_chars=None, chunk_max_chars=20))
        assert original["content"] == before, "裁剪不能改入参，响应里还要用原始内容"

    def test_empty_input(self):
        out, stats = apply_budget([], Budget())
        assert out == [] and stats["n_in"] == 0


class TestTopKCap:
    def test_no_budget_is_identity(self):
        assert cap_chunk_top_k(20, None) == 20

    def test_caps_above_limit(self):
        assert cap_chunk_top_k(20, Budget(max_chunk_top_k=6)) == 6

    def test_keeps_smaller_request(self):
        """请求只要 3 块时不该被抬到上限——上限是封顶，不是下限。"""
        assert cap_chunk_top_k(3, Budget(max_chunk_top_k=6)) == 3

    def test_never_below_one(self):
        assert cap_chunk_top_k(0, Budget(max_chunk_top_k=6)) == 1
