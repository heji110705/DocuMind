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
import json
import logging
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI

logger = logging.getLogger("documind.llm")

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

# DeepSeek 支持这些采样参数；response_format 故意不透传——
# LightRAG 自己的 JSON 抽取已经能容错 ```json 围栏，加了反而偶发空返回。
_ALLOWED = {"max_tokens", "temperature", "top_p", "stop", "timeout", "seed", "extra_body"}


def _vendor_extras(which: str = "primary") -> dict:
    """厂商私有参数，从 LLM_EXTRA_BODY 读一个 JSON 对象。

    为什么需要它：不同厂商把关键开关藏在私有字段里，而 OpenAI 兼容层只认
    extra_body。最典型的例子是千问系列的思考模式——默认开着，一句
    "只回两个字"也会先想掉几十上百个 token（实测 19~273 个），
    关掉只需要 {"enable_thinking": false}，输出立刻降到 1 个 token。

    不做成硬编码的 ENABLE_THINKING 开关，是因为换一家厂商又要改一次代码。
    这里是"透传"，语义由调用方决定，代码不必知道厂商是谁。

    解析失败一律返回空 dict 而不抛异常：一个写错的配置项不应该让整个服务起不来。
    """
    key = "LLM_EXTRA_BODY" if which == "primary" else "LLM_FALLBACK_EXTRA_BODY"
    raw = os.environ.get(key, "").strip()
    if not raw:
        return {}
    try:
        val = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return val if isinstance(val, dict) else {}


def _price_for(model: str) -> tuple[float, float]:
    """按模型名取 (输入价, 输出价)，单位：元 / 百万 token。

    只认主模型一种价。`PRICE_QWEN_*` 三项已经删掉，不是留空：
    留一个空配置项，迟早有人往里填个"差不多"的数字，然后 cost_cny
    悄悄失真，而界面上那个 ¥ 看起来一切正常——这比不显示价格更糟。
    qwen 的单价没有在控制台核实到，就不给它留位置（见 DEPLOY.md 第 15 节）。
    """
    m = (model or "").lower()
    legacy_in = float(os.environ.get("PRICE_IN_PER_M", "1.07") or 1.07)
    legacy_out = float(os.environ.get("PRICE_OUT_PER_M", "4.29") or 4.29)
    if m.startswith("deepseek"):
        return (
            float(os.environ.get("PRICE_DEEPSEEK_INPUT", "") or legacy_in),
            float(os.environ.get("PRICE_DEEPSEEK_OUTPUT", "") or legacy_out),
        )
    return legacy_in, legacy_out


class LLMError(Exception):
    """LLM 不可用的统一出口。

    服务层只看 ``status`` / ``code``，不去解析厂商报文的字符串——
    把"是不是 401"的判断留在客户端这一层，接口层才不会长出一堆正则。
    """

    status = 500
    code = "E2050"


class LLMAuthError(LLMError):
    """401 / 403：key 写错、被封、或上游用 403 表达"额度耗尽"。

    重试对它没有任何意义——同一把 key 再敲十次还是 401，只会把用户从
    "0.3 秒看到报错"拖成"等十几秒才看到报错"。所以直接抛，服务层回 500。
    """

    status = 500
    code = "E2051"


class LLMUnavailable(LLMError):
    """429 / 5xx / 超时，重试之后仍然不可用。服务层回 503 + Retry-After。"""

    status = 503
    code = "E2053"
    retry_after = 30


_AUTH_STATUS = {401, 403}


def _status_of(exc: Exception) -> int | None:
    status = getattr(exc, "status_code", None)
    return status if isinstance(status, int) else None


def _is_auth(exc: Exception) -> bool:
    if _status_of(exc) in _AUTH_STATUS:
        return True
    name = type(exc).__name__
    return "Authentication" in name or "PermissionDenied" in name


def _is_retryable(exc: Exception) -> bool:
    """只有 429 / 5xx / 超时 / 连接错误值得重试。

    400（参数写错）重试多少次都是 400，白白多等 4 秒；把它排除掉，
    日志里也少两条噪音。
    """
    status = _status_of(exc)
    if status is None:
        name = type(exc).__name__
        return any(k in name for k in ("Timeout", "Connection", "APIConnection", "ConnectError"))
    return status == 429 or 500 <= status <= 599


def _backoff(attempt: int) -> float:
    """第 1、2 次重试分别等 1 秒、3 秒。

    服务端默认只重试 2 次（`LLM_RETRIES=3` 次尝试），用户最多多等 4 秒；
    评测跑批会把 `LLM_RETRIES` 提到 5，后面的间隔继续翻倍（6s / 12s），
    让一次 25 分钟的长跑不至于被几十秒的网络抖动毁掉。
    """
    return 1.0 if attempt == 0 else 3.0 * 2 ** (attempt - 1)


async def _sleep(seconds: float) -> None:
    """单独抽出来只为一件事：测试要断言"重试前真的等了 1s / 3s"，
    但测试自己不能真等 4 秒。"""
    await asyncio.sleep(seconds)


def fallback_enabled() -> bool:
    """备用模型是否启用：必须显式写 `LLM_FALLBACK_ENABLED=1`，且三项配置齐全。

    **默认关闭**。三条理由（完整版见 DEPLOY.md「为什么默认不 failover」）：
    qwen 会引用越界，而"引用可回溯"是产品核心承诺；免费额度不可预期；
    两个模型混跑会污染 cost_cny 的口径。
    """
    flag = os.environ.get("LLM_FALLBACK_ENABLED", "0").strip().lower()
    if flag not in {"1", "true", "yes", "on"}:
        return False
    return bool(
        os.environ.get("LLM_FALLBACK_MODEL")
        and os.environ.get("LLM_FALLBACK_API_KEY")
        and os.environ.get("LLM_FALLBACK_BASE_URL")
    )


def fallback_client() -> AsyncOpenAI:
    """惰性加载 qwen provider：只有启用 failover 才 import。

    import 放在函数体里是刻意的——默认配置下 `llm_fallback` 连
    `sys.modules` 都不会进，备胎里写错任何一个变量都伤不到主路径。
    """
    from llm_fallback import client as _make

    return _make()


def _model_of(which: str) -> str:
    if which == "fallback":
        return os.environ.get("LLM_FALLBACK_MODEL", "")
    return os.environ.get("LLM_MODEL", "deepseek-chat")


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
        # 按模型分桶：主模型失败切到备用之后，两个模型的输入/输出价格不同，
        # 混在一起算成本就是错的。键是实际用过的模型名。
        self.by_model: dict[str, list[int]] = {}
        self.model_used = ""
        self.fallbacks = 0

    def add(self, prompt_tokens: int = 0, completion_tokens: int = 0,
            seconds: float = 0.0, calls: int = 0, model: str = "") -> None:
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.seconds += seconds
        self.calls += calls
        if model:
            bucket = self.by_model.setdefault(model, [0, 0])
            bucket[0] += prompt_tokens
            bucket[1] += completion_tokens
            self.model_used = model

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cost(self) -> float:
        """按 model_used 分别取价再求和。

        为什么不能一个价算所有：主模型 deepseek-chat 与备用 qwen-plus 的单价差一个量级，
        发生切换的那次请求如果按主模型价折算，成本会被高估（或低估）几倍。
        分桶之后，即使一次请求里既有主模型调用又有备用模型调用，也能算准。

        单价来源见 docs/DEPLOY.md 第 15 节；没配置对应变量时回落到
        PRICE_IN_PER_M / PRICE_OUT_PER_M（兼容旧配置）。
        """
        if not self.by_model:
            pin, pout = _price_for("")
            return self.prompt_tokens / 1e6 * pin + self.completion_tokens / 1e6 * pout
        total = 0.0
        for model, (tin, tout) in self.by_model.items():
            pin, pout = _price_for(model)
            total += tin / 1e6 * pin + tout / 1e6 * pout
        return total

    def snapshot(self) -> dict:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "seconds": round(self.seconds, 2),
            "cost": round(self.cost, 6),
            "model_used": self.model_used,
            "fallbacks": self.fallbacks,
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


def _record(prompt_tokens: int, completion_tokens: int, seconds: float,
            calls: int = 1, model: str = "") -> None:
    USAGE.add(prompt_tokens, completion_tokens, seconds, calls, model=model)
    local = _LOCAL.get()
    if local is not None:
        local.add(prompt_tokens, completion_tokens, seconds, calls, model=model)


def _mark_fallback() -> None:
    """记一次"切到备用"。只加计数，不改变后续行为——切完不再回试主模型。"""
    USAGE.fallbacks += 1
    local = _LOCAL.get()
    if local is not None:
        local.fallbacks += 1


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


async def _once(which: str, messages: list[dict], opts: dict) -> str:
    """向指定模型发一次请求并记账。``which`` 是 primary / fallback。

    extra_body 按来源分开：主模型可能是 DeepSeek（不认识 enable_thinking），
    备用可能是通义（需要 enable_thinking=false）。用同一份 extras 会一边报错。
    """
    cli = client() if which == "primary" else fallback_client()
    model = _model_of(which)
    call_opts = dict(opts)
    extras = _vendor_extras(which)
    if extras:
        eb = dict(call_opts.get("extra_body") or {})
        for k, v in extras.items():
            eb.setdefault(k, v)
        call_opts["extra_body"] = eb
    else:
        call_opts.pop("extra_body", None)

    t0 = time.perf_counter()
    r = await cli.chat.completions.create(model=model, messages=messages, **call_opts)
    dt = time.perf_counter() - t0
    if r.usage:
        _record(r.usage.prompt_tokens, r.usage.completion_tokens, dt, model=model)
    else:
        _record(0, 0, dt, model=model)
    return (r.choices[0].message.content or "").strip()


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
    # 环境变量里的厂商私有参数，统一塞进 extra_body。
    # 不能直接 setdefault 到 opts 上——openai SDK 只接受它认识的顶层参数名，
    # 其余字段一律走 extra_body 透传，否则会 TypeError: unexpected keyword argument
    # （实测踩过：enable_thinking 被当成顶层参数直接报错，三关全挂）。
    # 重试次数与退避。默认 3 次（服务端用，用户最多等 6 秒）；
    # 评测跑批设 LLM_RETRIES=5（最多等 60 秒），因为一次 25 分钟的长跑
    # 不该被一次几十秒的网络抖动毁掉——实测踩过：代理端口切换导致整轮 124 题
    # 全部 APIConnectionError，白跑。
    attempts = max(1, int(os.environ.get("LLM_RETRIES", "3")))
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return await _once("primary", messages, opts)
        except Exception as e:  # 限流 / 网络抖动 / 5xx / key 失效
            last = e
            if _is_auth(e):
                # 401 / 403 重试没有意义：同一把 key 再敲十次还是 401。
                # 直接进失败分支，由服务层回 500，日志里写清是认证问题。
                logger.error(
                    "[llm] 主模型 %s 认证失败（%s: %s），不重试",
                    _model_of("primary"), type(e).__name__, str(e)[:120],
                )
                break
            if not _is_retryable(e) or attempt == attempts - 1:
                break
            USAGE.retries += 1
            await _sleep(_backoff(attempt))

    # 主模型彻底失败：**只有显式开启** failover 才切，切完不再回试主模型
    # （避免抖动时来回横跳）。没开就直接抛，让服务层给出 500 / 503。
    if last is not None and fallback_enabled():
        logger.warning(
            "[llm] 主模型 %s 失败（%s: %s），切备用 %s",
            _model_of("primary"), type(last).__name__, str(last)[:120],
            _model_of("fallback"),
        )
        _mark_fallback()
        try:
            return await _once("fallback", messages, opts)
        except Exception as e2:  # noqa: BLE001
            raise LLMUnavailable(f"主备均不可用：{type(e2).__name__}: {e2}") from e2

    if last is not None and _is_auth(last):
        raise LLMAuthError(f"{type(last).__name__}: {str(last)[:200]}") from last
    if last is not None and not _is_retryable(last):
        # 400 这类是我们自己把请求写错了，说"上游不可用"会误导排查方向。
        raise LLMError(f"上游拒绝请求：{type(last).__name__}: {str(last)[:200]}") from last
    raise LLMUnavailable(f"{type(last).__name__}: {str(last)[:200]}") from last


async def _open_stream(which: str, messages: list[dict], opts: dict):
    """建立流式请求，返回 (stream, model)。参数处理与 _once 一致。"""
    cli = client() if which == "primary" else fallback_client()
    model = _model_of(which)
    call_opts = dict(opts)
    extras = _vendor_extras(which)
    if extras:
        eb = dict(call_opts.get("extra_body") or {})
        for k, v in extras.items():
            eb.setdefault(k, v)
        call_opts["extra_body"] = eb
    else:
        call_opts.pop("extra_body", None)
    stream = await cli.chat.completions.create(
        model=model, messages=messages, stream=True,
        stream_options={"include_usage": True}, **call_opts,
    )
    return stream, model


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
    # 建流这一步也可能失败（key 失效 / 5xx / 超时）。必须在**拿到流之前**切，
    # 一旦开始 yield 就无法安全地换模型重来——用户会看到答案被截断。
    # 重试策略与 llm_func 完全一致：401/403 不重试，429/5xx/超时 1s、3s 各重试一次。
    t0 = time.perf_counter()
    attempts = max(1, int(os.environ.get("LLM_RETRIES", "3")))
    stream = None
    used_model = ""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            stream, used_model = await _open_stream("primary", messages, opts)
            break
        except Exception as e:  # noqa: BLE001
            last = e
            if _is_auth(e):
                logger.error(
                    "[llm-stream] 主模型 %s 认证失败（%s: %s），不重试",
                    _model_of("primary"), type(e).__name__, str(e)[:120],
                )
                break
            if not _is_retryable(e) or attempt == attempts - 1:
                break
            USAGE.retries += 1
            await _sleep(_backoff(attempt))

    if stream is None:
        if last is not None and fallback_enabled():
            logger.warning("[llm-stream] 主模型建流失败（%s: %s），切备用 %s",
                           type(last).__name__, str(last)[:120], _model_of("fallback"))
            _mark_fallback()
            try:
                stream, used_model = await _open_stream("fallback", messages, opts)
            except Exception as e2:  # noqa: BLE001
                raise LLMUnavailable(f"主备均不可用：{type(e2).__name__}: {e2}") from e2
        elif last is not None and _is_auth(last):
            raise LLMAuthError(f"{type(last).__name__}: {str(last)[:200]}") from last
        elif last is not None and not _is_retryable(last):
            raise LLMError(f"上游拒绝请求：{type(last).__name__}: {str(last)[:200]}") from last
        else:
            raise LLMUnavailable(
                f"{type(last).__name__}: {str(last)[:200]}" if last else "建流失败"
            ) from last
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
        _record(usage.prompt_tokens, usage.completion_tokens, dt, model=used_model)
        info = {"prompt_tokens": usage.prompt_tokens, "completion_tokens": usage.completion_tokens,
                "estimated": False, "model_used": used_model}
    else:
        text = "".join(buf)
        pin = len(prompt) + len(system_prompt or "")
        info = {"prompt_tokens": int(pin * 0.6), "completion_tokens": int(len(text) * 0.6),
                "estimated": True, "model_used": used_model}
        _record(info["prompt_tokens"], info["completion_tokens"], dt, model=used_model)
    yield {"__usage__": info}
