"""判定逻辑的单元测试。

这些函数决定了"拒答还是作答"、"引用编号对不对"，是最容易改坏、
又最难靠人眼发现的三处。所以单独钉住。
"""

import pytest

from prompts import is_refusal, parse_citations as _parse_citations, parse_verdict
from query_consts import REFUSAL_TEXT


class TestParseVerdict:
    def test_sufficient_with_quote(self):
        ok, quote = parse_verdict("摘录：订单数必须去重，否则高估 28%。\n判定：SUFFICIENT")
        assert ok is True
        assert "28%" in quote

    def test_insufficient(self):
        ok, _ = parse_verdict("摘录：NONE\n判定：INSUFFICIENT")
        assert ok is False

    def test_sufficient_but_no_quote_is_treated_as_insufficient(self):
        """自相矛盾时按不足处理——宁可拒答，不要编。"""
        ok, _ = parse_verdict("摘录：NONE\n判定：SUFFICIENT")
        assert ok is False

    def test_missing_label_defaults_to_insufficient(self):
        ok, _ = parse_verdict("我觉得这段材料大概能回答吧")
        assert ok is False

    def test_label_without_colon(self):
        ok, _ = parse_verdict("摘录：CTR 分母是曝光数\n判定 SUFFICIENT")
        assert ok is True

    def test_multiline_quote_is_truncated_at_verdict(self):
        ok, quote = parse_verdict("摘录：第一句。\n第二句。\n判定：SUFFICIENT")
        assert ok is True
        assert "判定" not in quote


class TestIsRefusal:
    def test_exact_refusal_text(self):
        assert is_refusal(REFUSAL_TEXT) is True

    def test_empty(self):
        assert is_refusal("") is True

    def test_normal_answer(self):
        assert is_refusal("订单数需要去重，实测高估 28%。") is False

    def test_hint_only_counts_at_head(self):
        """拒答词出现在开头才算拒答；出现在长答案中段是正常引用，不能误判。"""
        long_answer = "按文档口径，" + "这是一段很长的解释。" * 20 + "材料中没有提到的部分我不展开。"
        assert is_refusal(long_answer) is False


class TestCitations:
    def test_basic(self):
        assert _parse_citations("答案是 42 [1]，依据见 [3]。", 5) == [1, 3]

    def test_dedup_and_keep_order(self):
        assert _parse_citations("[2] 先说 [1] 再说 [2]", 5) == [2, 1]

    def test_out_of_range_dropped(self):
        """模型偶尔会引用不存在的编号，必须丢掉而不是抛错。"""
        assert _parse_citations("见 [9]", 3) == []

    def test_no_citation(self):
        assert _parse_citations("没有引用", 3) == []


class TestEvalHit:
    """评测判定：组间 AND、组内 OR。写错会让分数虚高。"""

    def test_and_of_ors(self):
        from evalutil import hit

        groups = [["28"], ["订单"]]
        assert hit("订单数会高估 28%", groups) is True
        assert hit("订单数会高估", groups) is False
        assert hit("高估 28%", groups) is False

    def test_or_inside_group(self):
        from evalutil import hit

        assert hit("用了 device_id", [["device_id", "设备号"]]) is True
        assert hit("用了设备号", [["device_id", "设备号"]]) is True
