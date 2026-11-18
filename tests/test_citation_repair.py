"""引用越界兜底的单元测试。

背景：模型会写不存在的编号（实测 qwen-plus 只有 6 块却写 [16]），
越界编号被丢弃后 citations 为空，而响应里的 chunks 只回被引用的块，
于是访客看到"答案对、来源空白"。
"""

from __future__ import annotations

from prompts import parse_citations, repair_citations


class TestRepair:
    def test_out_of_range_is_mapped_to_last_chunk(self):
        """越界回填：全部编号都越界时，映射到最后一块并标记 repair=True。"""
        text = "CTR 的分母是曝光数 [16]。"
        cites, oob, repaired = repair_citations(text, parse_citations(text, 6), 6)
        assert cites == [6], "n > len(chunks) 时取最后一块"
        assert oob == 1
        assert repaired is True

    def test_valid_citation_untouched(self):
        """正常引用不受影响：不允许因为兜底把对的编号改掉。"""
        text = "订单数必须去重 [1]。"
        base = parse_citations(text, 6)
        cites, oob, repaired = repair_citations(text, base, 6)
        assert cites == [1]
        assert oob == 0
        assert repaired is False

    def test_no_bracket_does_not_invent_citation(self):
        """无引用不误填：答案里没有 [n] 时不许凭空造一个引用。"""
        text = "证据不足：文档中未提及相关内容。"
        cites, oob, repaired = repair_citations(text, [], 6)
        assert cites == []
        assert oob == 0
        assert repaired is False

    def test_partial_valid_citations_are_not_padded(self):
        """一半合法一半越界时不动：回填会把噪声引到对的引用旁边。"""
        text = "A 见 [1]，B 见 [99]。"
        cites, oob, repaired = repair_citations(text, parse_citations(text, 6), 6)
        assert cites == [1]
        assert oob == 1
        assert repaired is False

    def test_zero_chunks_is_safe(self):
        """没有任何块时不能崩，也不能造出 [1]。"""
        cites, oob, repaired = repair_citations("见 [3]", [], 0)
        assert cites == [] and repaired is False

    def test_multiple_out_of_range_collapse_to_one(self):
        """多个越界编号都落到最后一块，去重后只留一个。"""
        text = "见 [9] 与 [16]。"
        cites, oob, repaired = repair_citations(text, [], 6)
        assert cites == [6]
        assert oob == 2
        assert repaired is True
