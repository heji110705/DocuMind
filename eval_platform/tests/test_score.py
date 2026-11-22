"""RAGAS 接线的单测：不联网、不装 ragas，也能把「口径」钉住。

被测的是 `ragas_score` 里**不依赖重依赖**的那一层：选样本、拒答覆盖、
代理召回、写回 Item.ragas、汇总口径。真正调 ragas 的 `_evaluate` 用 mock 替掉——
这样 CI 上不用装几百兆的 ragas 也能验证到口径。
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from eval_platform import ragas_score  # noqa: E402
from eval_platform.schema import Item, Run  # noqa: E402


def _fake_scores(rows, **_kw):
    """按 id 返回固定分，模拟 ragas 的逐题输出（忽略 model/base/key 等参数）。"""
    return {r["id"]: {"faithfulness": 0.9, "answer_relevancy_llm": 0.7,
                      "context_precision": 0.5} for r in rows}


class TestUsable(unittest.TestCase):
    def test_excludes_errors(self):
        items = [Item(id="Q1", type="fact", error="HTTP 500"),
                 Item(id="Q2", type="fact", contexts=["ctx"])]
        self.assertEqual([i.id for i in ragas_score.usable_items(items)], ["Q2"])

    def test_refuse_without_contexts_still_counted(self):
        """拒答题常常没有 citations；把它丢掉，"拒答对不对"就不进分母了。"""
        items = [Item(id="Q9", type="refuse", answer="证据不足", contexts=[])]
        self.assertEqual([i.id for i in ragas_score.usable_items(items)], ["Q9"])

    def test_limit_applies_after_filtering(self):
        items = [Item(id="Q1", type="fact", error="x"),
                 Item(id="Q2", type="fact", contexts=["c"]),
                 Item(id="Q3", type="fact", contexts=["c"])]
        self.assertEqual([i.id for i in ragas_score.usable_items(items, limit=1)], ["Q2"])


class TestScoreItems(unittest.TestCase):
    def setUp(self):
        self._patch = mock.patch.object(ragas_score, "_evaluate", side_effect=_fake_scores)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_writes_per_item_scores(self):
        it = Item(id="Q1", type="fact", question="q", answer="a", contexts=["c"])
        ragas_score.score_items([it], progress=False)
        self.assertEqual(it.ragas["faithfulness"], 0.9)
        self.assertEqual(it.ragas["answer_relevancy_llm"], 0.7)
        self.assertEqual(it.ragas["context_precision"], 0.5)

    def test_refuse_without_leak_gets_full_faithfulness(self):
        it = Item(id="Q9", type="refuse", question="q", answer="证据不足",
                  contexts=[], kw_leak=False)
        meta = ragas_score.score_items([it], progress=False)
        self.assertEqual(it.ragas["faithfulness"], 1.0)
        self.assertEqual(meta["n_refused_scored"], 1)
        self.assertEqual(meta["n_scored"], 1)      # 拒答题在分母里

    def test_refuse_with_leak_gets_zero(self):
        it = Item(id="Q9", type="refuse", question="q", answer="A 比 B 高",
                  contexts=[], kw_leak=True)
        ragas_score.score_items([it], progress=False)
        self.assertEqual(it.ragas["faithfulness"], 0.0)

    def test_keyword_recall_proxy(self):
        hit_all = Item(id="Q1", type="fact", question="q", answer="a", contexts=["一单一书", "订单"],
                       must=[["一单一书"], ["订单"]])
        miss_one = Item(id="Q2", type="fact", question="q", answer="a", contexts=["订单"],
                        must=[["一单一书"], ["订单"]])
        ragas_score.score_items([hit_all, miss_one], progress=False)
        self.assertEqual(hit_all.ragas["context_recall_keyword_proxy"], 1.0)
        self.assertEqual(miss_one.ragas["context_recall_keyword_proxy"], 0.0)

    def test_no_must_means_no_proxy(self):
        it = Item(id="Q1", type="fact", question="q", answer="a", contexts=["c"])
        ragas_score.score_items([it], progress=False)
        self.assertIsNone(it.ragas["context_recall_keyword_proxy"])

    def test_nan_becomes_none(self):
        with mock.patch.object(ragas_score, "_evaluate", return_value={
            "Q1": {"faithfulness": float("nan"), "answer_relevancy_llm": 0.7,
                   "context_precision": 0.5}}):
            it = Item(id="Q1", type="fact", question="q", answer="a", contexts=["c"])
            ragas_score.score_items([it], progress=False)
        self.assertIsNone(it.ragas["faithfulness"])
        # 写出去必须还是合法 JSON
        import json
        json.dumps(it.ragas, allow_nan=False)

    def test_no_samples_raises(self):
        with self.assertRaises(ragas_score.NoSamples):
            ragas_score.score_items([Item(id="Q1", type="fact", error="x")], progress=False)


class TestAggregate(unittest.TestCase):
    def test_run_metrics_include_refuse_denominator(self):
        """均值必须把「拒答记 1.0」算进去 —— 这就是那条口径的可见结果。"""
        fact = Item(id="Q1", type="fact", question="q", answer="a", contexts=["c"])
        refuse = Item(id="Q9", type="refuse", question="q", answer="证据不足", contexts=[])
        run = Run(suite="documind", items=[fact, refuse])
        with mock.patch.object(ragas_score, "_evaluate", side_effect=_fake_scores):
            agg = ragas_score.score_run(run, progress=False)
        # (0.9 + 1.0) / 2
        self.assertAlmostEqual(agg["faithfulness"], 0.95, places=4)
        self.assertEqual(agg["n_scored"], 2)
        self.assertEqual(run.metrics()["ragas"]["faithfulness"], 0.95)
        self.assertEqual(run.metrics()["ragas"]["n_scored"], 2)

    def test_note_is_kept(self):
        run = Run(suite="documind", items=[
            Item(id="Q1", type="fact", question="q", answer="a", contexts=["c"])])
        with mock.patch.object(ragas_score, "_evaluate", side_effect=_fake_scores):
            agg = ragas_score.score_run(run, progress=False)
        self.assertIn("代理口径", agg["context_recall_note"])


class TestJudgeFailure(unittest.TestCase):
    """ragas 在 key 失效时只给 NaN、不抛异常 —— 这一层必须把它变成显式失败。"""

    def test_all_nan_raises(self):
        nan = float("nan")
        with mock.patch.object(ragas_score, "_evaluate", return_value={
            "Q1": {"faithfulness": nan, "answer_relevancy_llm": nan, "context_precision": nan}}):
            with self.assertRaises(ragas_score.RagasUnavailable) as ctx:
                ragas_score.score_items(
                    [Item(id="Q1", type="fact", question="q", answer="a", contexts=["c"])],
                    progress=False)
        self.assertIn("NaN", str(ctx.exception))

    def test_empty_scores_raise(self):
        with mock.patch.object(ragas_score, "_evaluate", return_value={}):
            with self.assertRaises(ragas_score.RagasUnavailable):
                ragas_score.score_items(
                    [Item(id="Q1", type="fact", question="q", answer="a", contexts=["c"])],
                    progress=False)

    def test_partial_nan_warns_and_records(self):
        nan = float("nan")
        with mock.patch.object(ragas_score, "_evaluate", return_value={
            "Q1": {"faithfulness": 0.9, "answer_relevancy_llm": 0.7, "context_precision": 0.5},
            "Q2": {"faithfulness": nan, "answer_relevancy_llm": nan, "context_precision": nan}}):
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                meta = ragas_score.score_items(
                    [Item(id="Q1", type="fact", question="q", answer="a", contexts=["c"]),
                     Item(id="Q2", type="fact", question="q", answer="a", contexts=["c"])],
                    progress=False)
        self.assertIn("没打上分", buf.getvalue())
        self.assertEqual(meta["n_nan"]["faithfulness"], 1)


class TestPreflight(unittest.TestCase):
    """裁判端点先探一次：无效 key 必须在花钱之前就失败。"""

    def _client(self, *, chat_error=None):
        calls = {"chat": 0, "embed": 0}

        class _Completions:
            async def create(self, **kw):
                calls["chat"] += 1
                if chat_error:
                    raise chat_error
                return {"ok": True}

        class _Embeddings:
            async def create(self, **kw):
                calls["embed"] += 1
                return {"ok": True}

        class _Chat:
            completions = _Completions()

        class _Client:
            chat = _Chat()
            embeddings = _Embeddings()

        return _Client(), calls

    def test_ok_passes(self):
        client, _ = self._client()
        ragas_score._preflight(client, "m", "sk-1234", "http://x")

    def test_chat_failure_raises_with_hint(self):
        client, _ = self._client(chat_error=RuntimeError("401 Invalid API-key"))
        with self.assertRaises(ragas_score.JudgeEndpoint) as ctx:
            ragas_score._preflight(client, "m", "sk-1234", "http://x")
        msg = str(ctx.exception)
        self.assertIn("裁判端点", msg)
        self.assertIn("401", msg)
        self.assertIn("--ragas-key", msg)
        self.assertIsInstance(ctx.exception, ragas_score.RagasUnavailable)  # 仍是子类

    def test_does_not_touch_embeddings(self):
        """answer_relevancy 已改成裁判直判：预检不该再打向量端点。

        DeepSeek 没有 embeddings（实测 404），这条断言是把"不依赖向量"钉住，
        否则将来谁把 RAGAS 原版 AnswerRelevancy 加回来，score 会在预检就挂。
        """
        client, calls = self._client()
        ragas_score._preflight(client, "m", "sk-1234", "http://x")
        self.assertEqual(calls, {"chat": 1, "embed": 0})


class TestGroupReport(unittest.TestCase):
    """三口径的分母是硬约定：同一批数据换个分母，faith 能从 0.45 跳到 0.90。"""

    def _items(self):
        answered = Item(id="Q1", type="fact", question="q", answer="a", contexts=["c"],
                        ragas={"faithfulness": 0.9, "context_precision": 0.8,
                               "context_recall_keyword_proxy": 1.0,
                               "answer_relevancy_llm": 0.7})
        refused_fact = Item(id="Q2", type="fact", question="q", answer="证据不足",
                            contexts=[], refused=True, ragas=None)
        zero_fact = Item(id="Q3", type="fact", question="q", answer="a", contexts=["c"],
                         ragas={"faithfulness": 0.0, "context_precision": 0.5,
                                "context_recall_keyword_proxy": 0.0,
                                "answer_relevancy_llm": 0.6})
        refuse = Item(id="Q9", type="refuse", question="q", answer="证据不足",
                      ragas={"faithfulness": 1.0})
        return [answered, refused_fact, zero_fact, refuse]

    def test_three_sections(self):
        g = ragas_score.group_report(self._items())
        a, b, b2, c = (g["A_answerable_all"], g["B_answerable_answered"],
                       g["B2_answered_excl_zero"], g["C_refuse"])
        # A：3 道可答题，没答出来的 Q2 记 0 → (0.9 + 0 + 0.0) / 3
        self.assertEqual(a["n"], 3)
        self.assertEqual(a["n_zero_filled"], 1)
        self.assertAlmostEqual(a["faithfulness"], 0.3, places=4)
        # prec 只算有分的 2 道 → (0.8 + 0.5) / 2
        self.assertAlmostEqual(a["context_precision"], 0.65, places=4)
        self.assertEqual(a["n_prec"], 2)
        # B：剔除误拒 → 2 道（Q1、Q3）
        self.assertEqual(b["n"], 2)
        self.assertAlmostEqual(b["faithfulness"], 0.45, places=4)
        # B2：再剔除 faith=0 → 1 道
        self.assertEqual(b2["n"], 1)
        self.assertAlmostEqual(b2["faithfulness"], 0.9, places=4)
        # C：拒答题单独
        self.assertEqual(c["n"], 1)
        self.assertEqual(c["faithfulness"], 1.0)

    def test_refuse_not_in_answerable_sections(self):
        g = ragas_score.group_report(self._items())
        self.assertEqual(g["A_answerable_all"]["faithfulness"], 0.3)  # 不含拒答那 1.0

    def test_format_has_three_headers(self):
        text = ragas_score.format_groups(ragas_score.group_report(self._items()))
        self.assertIn("口径一：可答题全量", text)
        self.assertIn("口径二：剔除误拒后", text)
        self.assertIn("口径三：拒答题单独", text)

    def test_empty_run_is_safe(self):
        g = ragas_score.group_report([])
        self.assertIsNone(g["A_answerable_all"]["faithfulness"])
        self.assertEqual(g["C_refuse"]["n"], 0)


class TestMissingRagas(unittest.TestCase):
    def test_raises_unavailable_with_install_hint(self):
        """没装 ragas 时要给一句能照抄的安装命令，而不是一个裸 ImportError。"""
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *a, **kw):
            if name.startswith("ragas"):
                raise ImportError("No module named 'ragas'")
            return real_import(name, *a, **kw)

        with mock.patch.object(builtins, "__import__", fake_import):
            with self.assertRaises(ragas_score.RagasUnavailable) as ctx:
                ragas_score._evaluate([{"id": "Q1", "question": "q", "answer": "a",
                                        "contexts": ["c"]}],
                                      model="m", base=None, key="k", progress=False)
        self.assertIn("pip install", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
