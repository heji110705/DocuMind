"""DeepSeek 调用 + token/费用计量。

为什么不用 lightrag.llm.openai.openai_complete_if_cache：
它只返回字符串，拿不到 usage，成本只能靠估算。这里直接用 AsyncOpenAI，
把真实 prompt_tokens / completion_tokens 累加进来，成本就是账单数字。

计量分两层：
- USAGE 是进程级累计，适合看"这次跑批一共花了多少"。
- track_usage() 是请求级账本，用 ContextVar 隔离。

为什么需要第二层：一开始只有 USAGE，单题成本靠"调用前后相减"。并发跑批时
多个任务的窗口互相重叠，减出来的差值会把别人的增量算进来——实测出现过
单题 9 次调用、单题 28,340 prompt token 这种不可能的数字，四组合计成本也因此偏高。
ContextVar 让每个协程（及其子任务）各记各的，才谈得上"成本是账单数字"。

对 LightRAG 是透明替换：签名保持 (prompt, system_prompt, history_messages, **kwargs)，
不认识的 kwargs（hashing_kv / keyword_extraction）直接丢弃。
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
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

    def add(self, prompt_tokens: int = 0, completion_tokens: int = 0,
            seconds: float = 0.0, calls: int = 0) -> None:
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.seconds += seconds
        self.calls += calls

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
_LOCAL: ContextVar[Usage | None] = ContextVar("local_usage", default=None)


@contextmanager
def track_usage():
    """给当前协程开一个独立账本，yield 出来后从里面读本次请求的真实开销。

    用 with 包住整段 await 是安全的：ContextVar 在同一个任务内保持不变，
    子任务（LightRAG 内部 gather 出来的）会继承同一个 Usage 对象，
    所以它们的增量也会记进这本账。
    """
    u = Usage()
    token = _LOCAL.set(u)
    try:
        yield u
    finally:
        _LOCAL.reset(token)


def _record(prompt_tokens: int, completion_tokens: int, seconds: float, calls: int = 1) -> None:
    USAGE.add(prompt_tokens, completion_tokens, seconds, calls)
    local = _LOCAL.get()
    if local is not None:
        local.add(prompt_tokens, completion_tokens, seconds, calls)


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

    # 重试次数与退避。默认 3 次（服务端用，用户最多等 6 秒）；
    # 评测跑批设 LLM_RETRIES=5（最多等 60 秒），因为一次 25 分钟的长跑
    # 不该被一次几十秒的网络抖动毁掉——实测踩过：代理端口切换导致整轮 124 题
    # 全部 APIConnectionError，白跑。
    attempts = max(1, int(os.environ.get("LLM_RETRIES", "3")))
    last: Exception | None = None
    for attempt in range(attempts):
        t0 = time.perf_counter()
        try:
            r = await client().chat.completions.create(
                model=os.environ.get("LLM_MODEL", "deepseek-chat"),
                messages=messages,
                **opts,
            )
            dt = time.perf_counter() - t0
            if r.usage:
                _record(r.usage.prompt_tokens, r.usage.completion_tokens, dt)
            else:
                _record(0, 0, dt)
            return (r.choices[0].message.content or "").strip()
        except Exception as e:  # 限流 / 网络抖动 / 5xx
            last = e
            USAGE.retries += 1
            if attempt == attempts - 1:
                raise
            await asyncio.sleep(2 ** (attempt + 1))
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

    dt = time.perf_counter() - t0
    if usage is not None:
        _record(usage.prompt_tokens, usage.completion_tokens, dt)
        info = {"prompt_tokens": usage.prompt_tokens, "completion_tokens": usage.completion_tokens,
                "estimated": False}
    else:
        text = "".join(buf)
        pin = len(prompt) + len(system_prompt or "")
        info = {"prompt_tokens": int(pin * 0.6), "completion_tokens": int(len(text) * 0.6),
                "estimated": True}
        _record(info["prompt_tokens"], info["completion_tokens"], dt)
    yield {"__usage__": info}
