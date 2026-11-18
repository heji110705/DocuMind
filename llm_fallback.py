"""备用模型（qwen）provider —— 默认根本不加载。

为什么单独一个文件：failover 默认关闭（`LLM_FALLBACK_ENABLED=0`），
这个模块就不会被 import。备胎平时不触发，如果连 import 都省不掉，
它的 key、端点、extra_body 每次启动都要解析一遍，任何一处写错都会让
主路径跟着起不来——那就不是备胎，是负重。

`llm_client.fallback_client()` 在函数体里 import 本模块，只有显式开启
才走得到。有测试钉住这个行为（`tests/test_llm_failover.py::test_qwen_not_imported`）。
"""

from __future__ import annotations

import os

from openai import AsyncOpenAI

_client: AsyncOpenAI | None = None


def model() -> str:
    return os.environ.get("LLM_FALLBACK_MODEL", "")


def client() -> AsyncOpenAI:
    """惰性建一次，之后复用。参数不对时让它当场抛错，不做事后兜底。"""
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=os.environ["LLM_FALLBACK_API_KEY"],
            base_url=os.environ["LLM_FALLBACK_BASE_URL"],
            timeout=180,
            max_retries=0,  # 重试由 llm_client 统一控制，才能统计次数
        )
    return _client
