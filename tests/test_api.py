"""HTTP 边界与计费的单元测试。

这里钉的两件事都是"看起来没问题、用起来对不上"的类型：

1. 参数校验：mode 写错、top_k 传字符串，以前会抛异常冒成 HTTP 500，
   或者被 LightRAG 悄悄吞掉用默认值——两种都让人以为是服务挂了。
2. 计量：单次成本曾经返回进程累计值；后来改成前后相减，并发请求又会串账。
   现在每个请求一本账，这里用三个并发任务验证互不干扰。
"""

import asyncio

import pytest

from app.main import BadRequest, _bill, _read_params
from llm_client import Usage, _record, track_usage


class TestReadParams:
    def test_defaults_match_eval(self):
        """检索默认值必须和评测跑的那套一致，否则界面复现不了 README 的准确率。

        重排默认关：一条 curl 等两分钟会被当成服务挂了。要精度就显式传 true。
        """
        p = _read_params({"question": "订单数为什么要去重"})
        assert p == {
            "question": "订单数为什么要去重",
            "mode": "mix",
            "top_k": 10,
            "chunk_top_k": 6,
            "rerank": False,
        }

    def test_strips_question(self):
        assert _read_params({"question": "  空格包围  "})["question"] == "空格包围"

    def test_empty_question(self):
        with pytest.raises(BadRequest) as e:
            _read_params({"question": "   "})
        assert e.value.code == "E1001"

    def test_missing_question(self):
        with pytest.raises(BadRequest) as e:
            _read_params({})
        assert e.value.code == "E1001"

    def test_too_long(self):
        with pytest.raises(BadRequest) as e:
            _read_params({"question": "问" * 501})
        assert e.value.code == "E1002"

    def test_length_boundary_ok(self):
        assert len(_read_params({"question": "问" * 500})["question"]) == 500

    def test_bad_mode_is_400_not_500(self):
        with pytest.raises(BadRequest) as e:
            _read_params({"question": "测试", "mode": "bogus"})
        assert e.value.code == "E1003"

    def test_mode_case_insensitive(self):
        assert _read_params({"question": "测试", "mode": "MIX"})["mode"] == "mix"

    def test_top_k_not_a_number(self):
        with pytest.raises(BadRequest) as e:
            _read_params({"question": "测试", "top_k": "abc"})
        assert e.value.code == "E1004"

    def test_top_k_out_of_range(self):
        with pytest.raises(BadRequest) as e:
            _read_params({"question": "测试", "top_k": 999})
        assert e.value.code == "E1004"

    def test_top_k_accepts_numeric_string(self):
        assert _read_params({"question": "测试", "top_k": "12"})["top_k"] == 12

    def test_body_not_object(self):
        with pytest.raises(BadRequest) as e:
            _read_params(["不是字典"])
        assert e.value.code == "E1005"

    def test_question_wrong_type_is_400(self):
        """传数字进来不能冒成 500：.strip() 会抛 AttributeError。"""
        for bad in (123, ["问题"], {"q": 1}, True):
            with pytest.raises(BadRequest) as e:
                _read_params({"question": bad})
            assert e.value.code == "E1005"


class TestBill:
    def test_reads_own_usage_only(self):
        """账单读的是本次请求自己的账本，不是进程累计值。"""
        u = Usage()
        u.add(3_000, 400, 0.0, calls=2)
        b = _bill(u, t0=0.0)
        assert b["calls"] == 2
        assert b["tokens_in"] == 3_000
        assert b["tokens_out"] == 400
        assert 0 < b["cost_cny"] < 0.05

    def test_concurrent_requests_do_not_mix(self):
        """三个并发任务的账本互不干扰。

        这是并发跑批时真正会出问题的地方：用"全局计数器前后相减"的写法，
        重叠窗口会把别人的增量算进自己账上，单题成本就变成一串不可能的数字。
        """

        async def worker(tokens: int) -> int:
            with track_usage() as u:
                for _ in range(3):
                    await asyncio.sleep(0.01)   # 故意让三个任务的窗口重叠
                    _record(tokens, 10, 0.0)
                return u.prompt_tokens

        async def main():
            return await asyncio.gather(worker(100), worker(500), worker(900))

        assert asyncio.run(main()) == [300, 1500, 2700]
