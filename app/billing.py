"""把一次请求的账本折算成响应里的四个数字。

单独一个文件没有技术上的必要性，纯粹是为了让"成本/token/耗时"这套算法
可以在不装 lightrag 的环境里被测试——它是整个项目里最容易被写错、
又最容易被观众当场发现的一处（"你这个 ¥0.0456 跟上一题怎么一样"）。
"""

from __future__ import annotations

import time

from llm_client import Usage


def bill(used: Usage, t0: float) -> dict:
    """返回本请求自己的开销，不相减、不读全局。

    t0 是 time.perf_counter() 的起点；elapsed_ms 由它算，保证"界面上看到的秒数"
    和"日志里打出来的毫秒数"是同一个来源。
    """
    return {
        "cost_cny": round(used.cost, 6),
        "tokens_in": used.prompt_tokens,
        "tokens_out": used.completion_tokens,
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        "calls": used.calls,
    }
