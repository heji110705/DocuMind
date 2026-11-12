"""计量：单请求账本与并发隔离。

单次成本曾经返回进程累计值（第二问显示的是"第一问+第二问"）；改成"调用前后相减"
之后并发窗口重叠又会串账。现在每个请求一本账，这里用三个重叠的协程钉住它。
"""

import asyncio
import time

from app.billing import bill
from llm_client import Usage, _record, track_usage


class TestBill:
    def test_reads_own_usage_only(self):
        u = Usage()
        u.add(3_000, 400, 0.0, calls=2)
        b = bill(u, t0=time.perf_counter())
        assert b["calls"] == 2
        assert b["tokens_in"] == 3_000
        assert b["tokens_out"] == 400
        assert 0 < b["cost_cny"] < 0.05

    def test_elapsed_is_measured_from_t0(self):
        u = Usage()
        b = bill(u, t0=time.perf_counter() - 1.5)
        assert 1400 <= b["elapsed_ms"] <= 2000

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

    def test_nested_scope_restores_previous(self):
        """内层账本结束后，外层继续记自己的——避免 finally 里的事漏记到别人头上。"""
        with track_usage() as outer:
            _record(10, 1, 0.0)
            with track_usage() as inner:
                _record(20, 2, 0.0)
            _record(30, 3, 0.0)
        assert outer.prompt_tokens == 40
        assert inner.prompt_tokens == 20

    def test_cost_formula_uses_env_prices(self, monkeypatch):
        monkeypatch.setenv("PRICE_IN_PER_M", "1.0")
        monkeypatch.setenv("PRICE_OUT_PER_M", "2.0")
        u = Usage()
        u.add(1_000_000, 1_000_000, 0.0)
        assert abs(u.cost - 3.0) < 1e-9
