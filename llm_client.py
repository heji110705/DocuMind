"""DeepSeek 调用 + token/费用计量。

为什么不用 lightrag.llm.openai.openai_complete_if_cache：
它只返回字符串，拿不到 usage，成本只能靠估算。这里直接用 AsyncOpenAI，
把真实 prompt_tokens / completion_tokens 累加进来，成本就是账单数字。

对 LightRAG 是透明替换：签名保持 (prompt, system_prompt, history_messages, **kwargs)，
不认识的 kwargs（hashing_kv / keyword_extraction）直接丢弃。
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

# DeepSeek 支持这些采样参数；response_format 故意不透传——
# LightRAG 自己的 JSON 抽取已经能容错 ```json 围栏，加了反而偶发空返回。
_ALLOWED = {"max_tokens", "temperature", "top_p", "stop", "timeout", "seed"}


class Usage:
    """累计调用次数、token、耗时，并折算成人民币成本。"""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.seconds = 0.0
        self.retries = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cost(self) -> float:
        """单价从 .env 读，默认按 deepseek-chat 低峰价折算（$0.15 / $0.60 每百万，汇率 7.15）。"""
        pin = float(os.environ.get("PRICE_IN_PER_M", "1.07"))
        pout = float(os.environ.get("PRICE_OUT_PER_M", "4.29"))
        return self.prompt_tokens / 1e6 * pin + self.completion_tokens / 1e6 * pout

    def snapshot(self) -> dict:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "seconds": round(self.seconds, 2),
            "cost": round(self.cost, 6),
        }

    def lines(self) -> list[str]:
        return [
            f"LLM 调用 {self.calls} 次（重试 {self.retries}），耗时 {self.seconds:.1f}s",
            f"token 输入 {self.prompt_tokens:,} / 输出 {self.completion_tokens:,}",
            f"实付约 ¥{self.cost:.4f}",
        ]


USAGE = Usage()


def reset_usage() -> None:
    USAGE.reset()


_client: AsyncOpenAI | None = None


def client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=os.environ["LLM_API_KEY"],
            base_url=os.environ.get("LLM_BASE_URL", "https://api.deepseek.com"),
            timeout=180,
            max_retries=0,  # 重试自己控制，才能统计到重试次数
        )
    return _client


async def llm_func(
    prompt: str,
    system_prompt: str | None = None,
    history_messages: list | None = None,
    **kwargs,
) -> str:
    messages: list[dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    for m in history_messages or []:
        messages.append(m if isinstance(m, dict) else {"role": "user", "content": str(m)})
    messages.append({"role": "user", "content": prompt})

    opts = {k: v for k, v in kwargs.items() if k in _ALLOWED and v is not None}
    opts.setdefault("max_tokens", 4096)

    last: Exception | None = None
    for attempt in range(3):
        t0 = time.perf_counter()
        try:
            r = await client().chat.completions.create(
                model=os.environ.get("LLM_MODEL", "deepseek-chat"),
                messages=messages,
                **opts,
            )
            USAGE.seconds += time.perf_counter() - t0
            USAGE.calls += 1
            if r.usage:
                USAGE.prompt_tokens += r.usage.prompt_tokens
                USAGE.completion_tokens += r.usage.completion_tokens
            return (r.choices[0].message.content or "").strip()
        except Exception as e:  # 限流 / 网络抖动 / 5xx
            last = e
            USAGE.retries += 1
            if attempt == 2:
                raise
            await asyncio.sleep(2 * (attempt + 1))
    raise last  # pragma: no cover


async def llm_stream(
    prompt: str,
    system_prompt: str | None = None,
    history_messages: list | None = None,
    **kwargs,
):
    """流式版本，逐段 yield 文本增量，最后 yield 一个 {"__usage__": ...} 供调用方结账。

    为什么不用 llm_func 再切片：那样看起来是流式，实际是等整段返回后一次性吐出，
    首 token 时间跟非流式一样。真流式必须走 stream=True。

    usage 拿不到时（部分兼容实现不返回 stream_options）退化为按字符估算，
    并在返回的 usage 里标 estimated=True——宁可标清"这是估的"，也不要混进真实账单。
    """
    messages: list[dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    for m in history_messages or []:
        messages.append(m if isinstance(m, dict) else {"role": "user", "content": str(m)})
    messages.append({"role": "user", "content": prompt})

    opts = {k: v for k, v in kwargs.items() if k in _ALLOWED and v is not None}
    opts.setdefault("max_tokens", 4096)

    t0 = time.perf_counter()
    stream = await client().chat.completions.create(
        model=os.environ.get("LLM_MODEL", "deepseek-chat"),
        messages=messages,
        stream=True,
        stream_options={"include_usage": True},
        **opts,
    )
    buf: list[str] = []
    usage = None
    async for chunk in stream:
        if getattr(chunk, "usage", None):
            usage = chunk.usage
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        piece = getattr(delta, "content", None)
        if piece:
            buf.append(piece)
            yield piece

    USAGE.seconds += time.perf_counter() - t0
    USAGE.calls += 1
    if usage is not None:
        USAGE.prompt_tokens += usage.prompt_tokens
        USAGE.completion_tokens += usage.completion_tokens
        info = {"prompt_tokens": usage.prompt_tokens, "completion_tokens": usage.completion_tokens,
                "estimated": False}
    else:
        text = "".join(buf)
        pin = len(prompt) + len(system_prompt or "")
        info = {"prompt_tokens": int(pin * 0.6), "completion_tokens": int(len(text) * 0.6),
                "estimated": True}
        USAGE.prompt_tokens += info["prompt_tokens"]
        USAGE.completion_tokens += info["completion_tokens"]
    yield {"__usage__": info}
