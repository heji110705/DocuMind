"""防刷闸门的单元测试。

为什么单独测这个类而不只测接口：限流的正确性全在"时间窗口怎么滚"上，
而接口测试要真等 60 秒才能验证窗口，不现实。注入 clock 就能毫秒级验证完。
"""

from __future__ import annotations

from app.limits import RateLimiter, build_from_env


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def tick(self, s: float) -> None:
        self.t += s


def test_per_minute_window_blocks_then_recovers():
    clk = FakeClock()
    lim = RateLimiter(per_min=3, per_day=0, daily_total=0, clock=clk)

    for _ in range(3):
        ok, _, _ = lim.check("1.1.1.1")
        assert ok
        lim.hit("1.1.1.1")

    ok, wait, why = lim.check("1.1.1.1")
    assert not ok and wait > 0 and "频繁" in why

    # 另一个 IP 不受影响
    assert lim.check("2.2.2.2")[0]

    # 窗口滚过去之后恢复
    clk.tick(61)
    assert lim.check("1.1.1.1")[0]


def test_per_ip_day_quota():
    clk = FakeClock()
    lim = RateLimiter(per_min=0, per_day=2, daily_total=0, clock=clk)
    for _ in range(2):
        assert lim.check("9.9.9.9")[0]
        lim.hit("9.9.9.9")
    ok, _, why = lim.check("9.9.9.9")
    assert not ok and "今日额度" in why
    # 换 IP 仍可用（这是 per_ip 层挡不住的，靠 daily_total 兜）
    assert lim.check("8.8.8.8")[0]


def test_global_daily_total_caps_everyone():
    clk = FakeClock()
    lim = RateLimiter(per_min=0, per_day=0, daily_total=3, clock=clk)
    for i in range(3):
        assert lim.check(f"10.0.0.{i}")[0]
        lim.hit(f"10.0.0.{i}")
    ok, _, why = lim.check("10.0.0.9")
    assert not ok and "全站" in why

    # 跨天后重置
    clk.tick(86401)
    assert lim.check("10.0.0.9")[0]


def test_zero_means_unlimited():
    lim = RateLimiter(per_min=0, per_day=0, daily_total=0, clock=FakeClock())
    for _ in range(500):
        assert lim.check("x")[0]
        lim.hit("x")


def test_check_does_not_count_until_hit():
    """参数校验失败的请求不该消耗额度——所以 check 和 hit 是分开的。"""
    clk = FakeClock()
    lim = RateLimiter(per_min=2, per_day=0, daily_total=0, clock=clk)
    for _ in range(10):          # 只检查、不计数
        assert lim.check("5.5.5.5")[0]
    assert lim.check("5.5.5.5")[0]
    assert lim.snapshot()["used_today"] == 0


def test_build_from_env_defaults_and_fallback(monkeypatch):
    for k in ("OPEN_IP_PER_MIN", "OPEN_IP_PER_DAY", "OPEN_DAILY_TOTAL"):
        monkeypatch.delenv(k, raising=False)
    lim = build_from_env()
    assert (lim.per_min, lim.per_day, lim.daily_total) == (6, 100, 300)

    monkeypatch.setenv("OPEN_IP_PER_MIN", "abc")   # 写错退回默认
    monkeypatch.setenv("OPEN_DAILY_TOTAL", "-5")   # 负数也退回默认
    lim = build_from_env()
    assert lim.per_min == 6 and lim.daily_total == 300
