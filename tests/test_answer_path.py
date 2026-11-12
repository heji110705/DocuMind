"""作答链路的"真的会跑"测试：把 judge / condense / build_answer_prompt 各调一次。

这个文件是为了一个非常具体的失败写的：
    NameError: name 'JUDGE_PROMPT' is not defined

它出现在把提示词从 `query.py` 拆到 `prompts.py` 之后。拆分本身没错，
错在 `query.py` 只 import 了"看起来会用到"的几个名字，漏了 `JUDGE_PROMPT`
和 `evidence_block`（后者还改了名）。

**为什么 113 个测试一个都没拦住**：
    - import 阶段不报错——Python 只在你真正访问那个名字时才抛 NameError；
    - `/healthz`、`/readyz` 正常——它们不碰判定；
    - 检索阶段日志一切正常，SSE 也照常推 stage 事件；
    - 只有走到"判证据"这一步才炸，而这一步恰好是两个模式（快速/高精度）的共同路径。

也就是说：一个 import 漏项能让整套系统"看起来在线、实际不能答"，
而所有"健康检查"类测试都看不见它。唯一的办法是**把那条路径真的走一遍**。

`judge()` 和 `condense()` 只在方法体内引用模块级常量，不依赖 `self` 的任何状态，
所以可以直接 `DocuMind.judge(None, ...)` 调用——不需要建索引、不需要网络、
不需要 LightRAG，纯替换掉 `llm_func` 就能跑。这是刻意的：如果这两条路径需要
一整台机器才能验证，那它们就永远不会被验证。
"""

from __future__ import annotations

import asyncio

import pytest

# 这个文件要 import query，而 query.py 在模块级 import lightrag。
# CI 的 unit job 只装 requirements-fast.txt（不含 lightrag），不跳过就会整批
# collection error——那会让"最小依赖也能跑测试"这件事变成空话。
# 用 importorskip 而不是 try/except ImportError：后者会把真正的 import 错误
# （比如 query.py 里写错了模块名）也一起吞掉。
pytest.importorskip("lightrag", reason="query.py 依赖 lightrag，属完整依赖层（见 CI 的 full job）")

import query as Q  # noqa: E402


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def fake_llm(monkeypatch):
    """把 llm_func 换成可编程的假函数，记录收到的提示词。"""
    calls: list[str] = []
    reply = {"text": "NONE"}

    async def fake(prompt: str, **kw) -> str:
        calls.append(prompt)
        return reply["text"]

    monkeypatch.setattr(Q, "llm_func", fake)
    return calls, reply


def test_judge_runs(fake_llm):
    """判定路径必须能真的执行完，而不是在拼提示词时抛 NameError。"""
    calls, reply = fake_llm
    reply["text"] = "摘录：直接 COUNT(*) 会高估约 1.28 倍\n判定：SUFFICIENT"
    chunks = [{"content": "订单数必须 COUNT(DISTINCT order_id)，直接 COUNT(*) 会高估约 1.28 倍"}]
    enough, quote = run(Q.DocuMind.judge(None, "订单数为什么必须去重？", chunks))
    assert calls, "没有调用模型，说明提示词根本没拼出来"
    assert enough is True
    assert "1.28" in quote
    # 提示词里必须同时带上问题和证据，缺一个判定就没有依据
    assert "订单数为什么必须去重" in calls[0]
    assert "COUNT(DISTINCT" in calls[0]


def test_judge_says_not_enough(fake_llm):
    _, reply = fake_llm
    reply["text"] = "NONE"
    enough, quote = run(Q.DocuMind.judge(None, "服务器机房在哪个城市？", []))
    assert enough is False
    assert quote in ("", "NONE")


def test_condense_without_history_is_free(fake_llm):
    """没有历史时不该花一次模型调用——多轮功能不能拖慢单轮。"""
    calls, _ = fake_llm
    q = "它有哪些口径坑？"
    out = run(Q.DocuMind.condense(None, q, []))
    assert out == q
    assert calls == []


def test_condense_with_history_calls_model(fake_llm):
    calls, reply = fake_llm
    reply["text"] = "「毛利率」有哪些口径坑？"
    out = run(Q.DocuMind.condense(None, "它有哪些口径坑？", [
        {"q": "毛利率怎么算？", "a": "用毛利除以净收入。"},
    ]))
    assert calls, "有历史时必须走一次改写"
    assert "毛利率" in out


def test_answer_prompt_includes_quote_anchor(fake_llm):
    """判定器摘出的那句原文必须进作答提示词，否则模型会二次自我否定。"""
    system, user = Q.build_answer_prompt(
        "订单数为什么必须去重？",
        [{"content": "直接 COUNT(*) 会高估约 1.28 倍", "file_path": "18-常见问题.md"}],
        quote="实测比真实订单数高 28%",
    )
    assert "28%" in user
    assert "18-常见问题.md" in user
    assert system


def test_answer_dataclass_exists():
    """`Answer` 这个数据结构必须存在。

    它被整段删掉过一次。因为它只在本文件内部使用，删了不会有 import 错误，
    只会在 `ask()` 的 return 那一行抛 NameError——**每一道题都失败**，
    而从日志上看"检索正常、成本正常、日志正常"，像是跑完了但没结果。
    """
    a = Q.Answer(
        question="q", answer="a", refused=False, citations=[1], chunks=[],
        latency_ms=1, mode="naive", rerank=False, guard=False,
    )
    assert a.extra == {}          # 默认值必须是新 dict，不能是共享可变对象
    b = Q.Answer(
        question="q", answer="a", refused=False, citations=[1], chunks=[],
        latency_ms=1, mode="naive", rerank=False, guard=False,
    )
    a.extra["x"] = 1
    assert b.extra == {}


def test_ask_end_to_end_with_stubs(fake_llm, monkeypatch):
    """把 `ask()` 真正跑一遍：检索打桩、模型打桩，其余全是真代码。

    这一条是上一条的升级版，也是唯一能同时抓住"漏导入 JUDGE_PROMPT"
    和"丢了 Answer 类"的测试——因为它们都只在 ask() 内部被执行到才报错。

    刻意不依赖 LightRAG、不联网、不建索引：`retrieve` 直接返回固定的一块证据，
    `llm_func` 返回预设文本。这样这条测试能在 1 秒内跑完，才有可能进 CI。
    """
    calls, reply = fake_llm
    dm = Q.DocuMind(tag="__test__", embed="local", use_rerank=False)

    async def fake_retrieve(question, **kw):
        return ([{"content": "订单数必须 COUNT(DISTINCT order_id)，直接 COUNT(*) 会高估约 1.28 倍",
                  "file_path": "18-常见问题.md"}], False)

    monkeypatch.setattr(dm, "retrieve", fake_retrieve)

    # 判定 → 作答两次调用，用 side_effect 按顺序给答案
    seq = [
        "摘录：直接 COUNT(*) 会高估约 1.28 倍\n判定：SUFFICIENT",
        "订单数必须去重，因为一单多书会产生多行，直接 COUNT(*) 会高估约 1.28 倍 [1]。",
    ]

    async def scripted(prompt, **kw):
        calls.append(prompt)
        return seq[len(calls) - 1] if len(calls) <= len(seq) else seq[-1]

    monkeypatch.setattr(Q, "llm_func", scripted)

    ans = run(dm.ask("订单数为什么必须去重？", mode="mix", rerank=False, guard=True))
    assert ans.refused is False
    assert "1.28" in ans.answer
    assert ans.citations == [1]
    assert ans.guard is True and ans.rerank is False
    assert ans.extra["n_chunks"] == 1
    assert ans.extra["history_used"] == 0
    assert len(calls) == 2          # 判定一次 + 作答一次


def test_ask_refuses_when_judge_says_no(fake_llm, monkeypatch):
    """证据不足时必须拒答，且拒答文案来自单一常量。"""
    calls, reply = fake_llm
    reply["text"] = "摘录：NONE\n判定：INSUFFICIENT"
    dm = Q.DocuMind(tag="__test__", embed="local", use_rerank=False)

    async def fake_retrieve(question, **kw):
        return ([{"content": "今天广州多云。", "file_path": "x.md"}], False)

    monkeypatch.setattr(dm, "retrieve", fake_retrieve)
    ans = run(dm.ask("服务器机房在哪个城市？", mode="mix", rerank=False, guard=True))
    assert ans.refused is True
    assert ans.answer == Q.REFUSAL_TEXT
    assert ans.citations == []


def test_ask_without_chunks_refuses_without_calling_model(fake_llm, monkeypatch):
    """一块证据都没召回时，不该再花一次模型调用去判"够不够"。"""
    calls, _ = fake_llm
    dm = Q.DocuMind(tag="__test__", embed="local", use_rerank=False)

    async def empty_retrieve(question, **kw):
        return ([], False)

    monkeypatch.setattr(dm, "retrieve", empty_retrieve)
    ans = run(dm.ask("随便问点什么？", mode="naive", rerank=False, guard=True))
    assert ans.refused is True
    assert calls == []
