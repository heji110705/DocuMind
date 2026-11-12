"""作答提示词的拼接测试。

流式与非流式现在共用 build_answer_prompt，这个函数的输出直接决定了
"引用编号能不能对上 chunk 序号"和"裁判摘录有没有被当成锚点传下去"。
"""

from query import build_answer_prompt


CHUNKS = [
    {"file_path": "10-字段字典.md", "content": "order_id 是订单号，一单多书共享。"},
    {"file_path": "20-指标口径.md", "content": "订单数必须按订单号去重，否则高估 28%。"},
]


def test_evidence_numbering_starts_at_one():
    _, system = build_answer_prompt("订单数怎么算", CHUNKS)
    assert "[1] 来源：10-字段字典.md" in system
    assert "[2] 来源：20-指标口径.md" in system


def test_quote_is_passed_as_anchor():
    _, system = build_answer_prompt("订单数怎么算", CHUNKS, "订单数必须按订单号去重，否则高估 28%。")
    assert "【与问题直接相关的原文】" in system
    assert "高估 28%" in system.split("【与问题直接相关的原文】")[1]


def test_none_quote_adds_no_anchor():
    for q in ("", "NONE", "none"):
        _, system = build_answer_prompt("订单数怎么算", CHUNKS, q)
        assert "【与问题直接相关的原文】" not in system


def test_user_prompt_is_the_question():
    user, _ = build_answer_prompt("订单数怎么算", CHUNKS)
    assert user == "订单数怎么算"
