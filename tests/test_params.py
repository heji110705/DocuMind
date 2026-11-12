"""HTTP 边界校验的单元测试（不依赖 lightrag）。

钉的是"看起来没问题、用起来对不上"的类型：mode 写错、top_k 传字符串、
enable_rerank 传字符串 "false"、历史形状不对——以前前三种要么冒成 500，
要么被静默当成 True，最后一种是直接崩在 .get() 上。
"""

import pytest

from app.params import MAX_HISTORY_TURNS, BadRequest, read_history, read_params


class TestReadParams:
    def test_defaults_match_eval(self):
        """检索默认值必须和评测跑的那套一致，否则界面复现不了 README 的准确率。

        重排默认关：一条 curl 等两分钟会被当成服务挂了。要精度就显式传 true。
        """
        p = read_params({"question": "订单数为什么要去重"})
        assert p == {
            "question": "订单数为什么要去重",
            "mode": "mix",
            "top_k": 10,
            "chunk_top_k": 6,
            "rerank": False,
            "history": [],
        }

    def test_strips_question(self):
        assert read_params({"question": "  空格包围  "})["question"] == "空格包围"

    def test_empty_question(self):
        with pytest.raises(BadRequest) as e:
            read_params({"question": "   "})
        assert e.value.code == "E2001"

    def test_missing_question(self):
        with pytest.raises(BadRequest) as e:
            read_params({})
        assert e.value.code == "E2001"

    def test_too_long(self):
        with pytest.raises(BadRequest) as e:
            read_params({"question": "问" * 501})
        assert e.value.code == "E2002"

    def test_length_boundary_ok(self):
        assert len(read_params({"question": "问" * 500})["question"]) == 500

    def test_bad_mode_is_400_not_500(self):
        with pytest.raises(BadRequest) as e:
            read_params({"question": "测试", "mode": "bogus"})
        assert e.value.code == "E2003"

    def test_mode_case_insensitive(self):
        assert read_params({"question": "测试", "mode": "MIX"})["mode"] == "mix"

    def test_top_k_not_a_number(self):
        with pytest.raises(BadRequest) as e:
            read_params({"question": "测试", "top_k": "abc"})
        assert e.value.code == "E2004"

    def test_top_k_out_of_range(self):
        with pytest.raises(BadRequest) as e:
            read_params({"question": "测试", "top_k": 999})
        assert e.value.code == "E2004"

    def test_top_k_accepts_numeric_string(self):
        assert read_params({"question": "测试", "top_k": "12"})["top_k"] == 12

    def test_body_not_object(self):
        with pytest.raises(BadRequest) as e:
            read_params(["不是字典"])
        assert e.value.code == "E2005"

    def test_question_wrong_type_is_400(self):
        """传数字进来不能冒成 500：.strip() 会抛 AttributeError。"""
        for bad in (123, ["问题"], {"q": 1}, True):
            with pytest.raises(BadRequest) as e:
                read_params({"question": bad})
            assert e.value.code == "E2005"


class TestRerankFlag:
    """`bool("false") is True` 这个坑：字符串 "false" 会把重排**打开**。

    打开重排意味着一次请求从 1 秒变 2 分钟，而现象只是"卡住了"，
    几乎不可能让人联想到是前端传了个字符串。
    """

    def test_string_false_is_false(self):
        assert read_params({"question": "测试", "enable_rerank": "false"})["rerank"] is False

    def test_string_true_is_true(self):
        for v in ("true", "TRUE", "1", "yes", "on", 1, True):
            assert read_params({"question": "测试", "enable_rerank": v})["rerank"] is True

    def test_false_values(self):
        for v in ("false", "0", "no", "off", 0, False):
            assert read_params({"question": "测试", "enable_rerank": v})["rerank"] is False

    def test_garbage_is_400(self):
        with pytest.raises(BadRequest) as e:
            read_params({"question": "测试", "enable_rerank": "也许"})
        assert e.value.code == "E2006"


class TestHistory:
    def test_absent_is_empty(self):
        assert read_history(None) == []
        assert read_history([]) == []

    def test_accepts_short_form(self):
        assert read_history([{"q": "上一问", "a": "上一答"}]) == [{"q": "上一问", "a": "上一答"}]

    def test_accepts_long_form(self):
        assert read_history([{"question": "上一问", "answer": "上一答"}]) == [
            {"q": "上一问", "a": "上一答"}
        ]

    def test_keeps_only_last_turns(self):
        hist = [{"q": f"问题{i}", "a": "答"} for i in range(10)]
        out = read_history(hist)
        assert len(out) == MAX_HISTORY_TURNS
        assert out[-1]["q"] == "问题9"

    def test_truncates_answer(self):
        out = read_history([{"q": "问", "a": "答" * 1000}])
        assert len(out[0]["a"]) == 400

    def test_drops_items_without_question(self):
        out = read_history([{"a": "只有答案"}, {"q": "", "a": "空问题"}, {"q": "好的"}])
        assert [h["q"] for h in out] == ["好的"]

    def test_bad_shape_is_ignored_not_fatal(self):
        """历史只是提示，形状不对不该让一次提问失败。"""
        assert read_history("不是列表") == []
        assert read_history([1, 2, "x"]) == []

    def test_history_survives_read_params(self):
        p = read_params({"question": "那它的默认值呢", "history": [{"q": "top_k 是什么", "a": "10"}]})
        assert p["history"] == [{"q": "top_k 是什么", "a": "10"}]
