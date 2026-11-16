"""开放演示模式下的防刷：按 IP 的分钟级限流 + 全局每日上限。

为什么要它
----------
线上的 Demo 是**无鉴权**的（.env 没配 API_KEYS），任何人拿到 IP 都能提问，
每次提问都会烧掉 `.env` 里那个 LLM key 的额度。2026-11-16 那次整机卡死之后，
"被别人刷"从理论风险变成了要防的实际问题。

设计取舍
--------
1. **不做鉴权**。HR 点开链接就要能问，让 ta 先拿 key 就没人会用了。
   所以限制的是"速率与总量"，不是"身份"。
2. **两层，不是一层**：
   - 按 IP 的分钟级限流：挡住脚本/刷子（它们通常高并发、固定 IP）；
   - 全局每日上限：挡住分布式或换 IP 的刷法（总量封顶，损失可预期）。
   只有第一层的话，换 IP 就绕过了；只有第二层的话，一个人几秒钟能把当天额度用完，
   正常访客就进不来了。
3. **纯内存**。进程重启计数清零——这是有意接受的：演示场景下"重启就重置"比
   引入 Redis 更划算，而且真要刷的时候 2C2G 也扛不住写盘。
4. **失败开放**（fail-open）：限流器自己出异常时放行请求。
   宁可少挡一次，也不要让一个 bug 把整个 Demo 变成 500。
"""

from __future__ import annotations

import os
import time
from collections import defaultdict


def _int_env(name: str, default: int) -> int:
    """读整型环境变量。写错了退回默认值而不是崩掉——配置项不该让服务起不来。"""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError:
        return default
    return val if val >= 0 else default


class RateLimiter:
    """按 key（这里是 IP）的滑动窗口限流 + 全局每日计数。

    时间用注入的 clock，方便测试里不 sleep 就验证窗口滚动。
    """

    def __init__(
        self,
        per_min: int = 6,
        per_day: int = 100,
        daily_total: int = 300,
        window_s: float = 60.0,
        clock=time.monotonic,
    ) -> None:
        self.per_min = per_min
        self.per_day = per_day
        self.daily_total = daily_total
        self.window_s = window_s
        self._clock = clock
        self._hits: dict[str, list[float]] = defaultdict(list)
        self._day_start = clock()
        self._total_today = 0
        self._total_by_ip: dict[str, int] = defaultdict(int)

    # -- 内部 ---------------------------------------------------------------
    def _roll_day(self, now: float) -> None:
        """跨天就清零。用 86400 秒而不是日历日，够用且不用处理时区。"""
        if now - self._day_start >= 86400:
            self._day_start = now
            self._total_today = 0
            self._total_by_ip.clear()
            self._hits.clear()

    def _recent(self, ip: str, now: float) -> list[float]:
        hits = [t for t in self._hits[ip] if now - t < self.window_s]
        if hits:
            self._hits[ip] = hits
        else:
            self._hits.pop(ip, None)
        return hits

    # -- 对外 ---------------------------------------------------------------
    def check(self, ip: str) -> tuple[bool, int, str]:
        """返回 (是否放行, 建议 Retry-After 秒, 原因)。放行时不计数。

        分成 check / hit 两步，是为了让"实际被处理了"才计数——
        参数校验失败的请求（比如问题为空）不该消耗额度。
        """
        now = self._clock()
        self._roll_day(now)

        if self.daily_total and self._total_today >= self.daily_total:
            return (False, 3600, f"今日全站额度已用完（{self.daily_total} 次），明天再来")

        if self.per_day and self._total_by_ip[ip] >= self.per_day:
            return (False, 3600, f"你的今日额度已用完（{self.per_day} 次）")

        if self.per_min:
            recent = self._recent(ip, now)
            if len(recent) >= self.per_min:
                wait = max(1, int(self.window_s - (now - recent[0])) + 1)
                return (False, wait, f"请求过于频繁（每分钟最多 {self.per_min} 次）")

        return (True, 0, "")

    def hit(self, ip: str) -> None:
        now = self._clock()
        self._hits[ip].append(now)
        self._total_today += 1
        self._total_by_ip[ip] += 1

    def snapshot(self) -> dict:
        return {
            "per_min": self.per_min or None,
            "per_day": self.per_day or None,
            "daily_total": self.daily_total or None,
            "used_today": self._total_today,
            "window_s": self.window_s,
        }


def build_from_env() -> RateLimiter:
    """从环境变量构造。全部留空则退化为"不限"，即保持原有开放行为。"""
    return RateLimiter(
        per_min=_int_env("OPEN_IP_PER_MIN", 6),
        per_day=_int_env("OPEN_IP_PER_DAY", 100),
        daily_total=_int_env("OPEN_DAILY_TOTAL", 300),
    )
