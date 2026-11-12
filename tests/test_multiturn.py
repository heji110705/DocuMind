"""多轮对话的纯逻辑测试：改写结果清洗与历史渲染。

这两块决定了"第二句问『那它的默认值呢』时能不能答对"。
真跑一次多轮要花两次模型调用，用纯函数把边界钉住更划算。
"""

from prompts import build_history_block, clean_rewrite


class TestCleanRewrite:
    def test_plain_question_passes(self):
        assert clean_rewrite("dim_book 的 rating_avg 是几分制？", "原始问题") == \
            "dim_book 的 rating_avg 是几分制？"

    def test_strips_quotes(self):
        assert clean_rewrite("“那它的默认值是多少？”", "原始") == "那它的默认值是多少？"

    def test_strips_label_prefix(self):
        assert clean_rewrite("改写后的问题：那它的默认值是多少？", "原始") == "那它的默认值是多少？"

    def test_takes_first_line_only(self):
        raw = "那它的默认值是多少？\n（这里解释一下为什么这么改）"
        assert clean_rewrite(raw, "原始") == "那它的默认值是多少？"

    def test_empty_falls_back(self):
        assert clean_rewrite("", "原始问题") == "原始问题"

    def test_none_falls_back(self):
        assert clean_rewrite("NONE", "原始问题") == "NONE"

    def test_long_paragraph_falls_back(self):
        """改写模型偶尔会把整段解释吐出来，这种必须退回原句。"""
        long = "它指的是 dim_book 表里的 rating_avg 字段，" + "这个字段用来表示站内平均评分，" * 8
        assert clean_rewrite(long, "原始问题") == "原始问题"

    def test_too_short_falls_back(self):
        assert clean_rewrite("嗯", "原始问题") == "原始问题"


class TestHistoryBlock:
    def test_renders_pairs(self):
        out = build_history_block([{"q": "问了什么", "a": "答了什么"}])
        assert "问：问了什么" in out and "答：答了什么" in out

    def test_answer_optional(self):
        assert build_history_block([{"q": "只有问题"}]) == "问：只有问题"

    def test_empty(self):
        assert build_history_block([]) == ""
