"""评测里的纯判分函数（不依赖模型与索引）。

单独拆出来的理由和 prompts.py 一样：判定规则一改，"分数"就整体变化，
必须能被单测钉住；而 run_eval.py 一 import 就把 lightrag 拉进来了。
"""

from __future__ import annotations

import os


def hit(text: str, groups: list[list[str]]) -> bool:
    """组间 AND、组内 OR。

    写成"任一关键词出现即算对"会让分数虚高：一道题要求同时说出
    "28%" 和 "订单"，只命中一个也判对，模型学会答半边就能拿满分。
    """
    if not groups:
        return True
    return all(any(alt in text for alt in g) for g in groups)


def cost_of(prompt_tokens: int, completion_tokens: int) -> float:
    pi = float(os.environ.get("PRICE_IN_PER_M", "1.07"))
    po = float(os.environ.get("PRICE_OUT_PER_M", "4.29"))
    return prompt_tokens / 1e6 * pi + completion_tokens / 1e6 * po
