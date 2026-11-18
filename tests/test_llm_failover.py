"""failover 默认关闭 + 失败分类的回归测试。

四条用例对应四个"线上会让人以为服务挂了"的坑：

1. 401 重试是白等：key 错了，再敲十次还是 401，只是把报错从 0.3 秒拖到十几秒；
2. 429 重试两次仍失败要回 503 + Retry-After，间隔必须是 1s / 3s（不是指数 2/4）；
3. SSE 已经发出 200 了，改不了状态码，只能先发 error 事件再 close——
   少这一条，前端会一直转圈等 token；
4. 默认配置下 qwen 模块连 import 都不该发生：备胎不许拖累主路径。

为什么这些要用"假 _once"而不是真打上游：真打无法制造稳定的 401 / 429，
也没法断言"到底重试了几次"。把 `_once` / `_sleep` 换掉之后，重试策略本身
变成纯逻辑，才能在 CI 里跑。
"""

from __future__ import annotations

import asyncio
import sys

import pytest

import llm_client


class Boom(Exception):
    """带 status_code 的假异常，形状与 openai 的 APIStatusError 一致。"""

    def __init__(self, status: int):
        super().__init__(f"HTTP {status} boom")
        self.status_code = status


def _no_fallback(monkeypatch):
    monkeypatch.delenv("LLM_FALLBACK_ENABLED", raising=False)
    monkeypatch.delenv("LLM_FALLBACK_MODEL", raising=False)
    monkeypatch.delenv("LLM_FALLBACK_API_KEY", raising=False)
    monkeypatch.delenv("LLM_FALLBACK_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_RETRIES", raising=False)  # 用默认的 3 次尝试


# --------------------------------------------------------------- 1. 401 不重试
def test_401_no_retry(monkeypatch):
    _no_fallback(monkeypatch)
    calls: list[int] = []

    async def boom(*a, **k):
        calls.append(1)
        raise Boom(401)

    monkeypatch.setattr(llm_client, "_once", boom)

    with pytest.raises(llm_client.LLMAuthError):
        asyncio.run(llm_client.llm_func("hi"))

    assert len(calls) == 1, "401 只该打一次上游"


def test_403_also_no_retry(monkeypatch):
    _no_fallback(monkeypatch)
    calls: list[int] = []

    async def boom(*a, **k):
        calls.append(1)
        raise Boom(403)  # 百炼"免费额度耗尽"就是用 403 表达的

    monkeypatch.setattr(llm_client, "_once", boom)

    with pytest.raises(llm_client.LLMAuthError):
        asyncio.run(llm_client.llm_func("hi"))

    assert len(calls) == 1


# ------------------------------------------------- 2. 429 重试两次后 503
def test_429_retries_twice_then_unavailable(monkeypatch):
    _no_fallback(monkeypatch)
    calls: list[int] = []
    delays: list[float] = []

    async def boom(*a, **k):
        calls.append(1)
        raise Boom(429)

    async def fake_sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr(llm_client, "_once", boom)
    monkeypatch.setattr(llm_client, "_sleep", fake_sleep)

    with pytest.raises(llm_client.LLMUnavailable) as ei:
        asyncio.run(llm_client.llm_func("hi"))

    assert len(calls) == 3, "1 次 + 2 次重试"
    assert delays == [1.0, 3.0], "间隔必须是 1s / 3s"
    assert ei.value.status == 503
    assert ei.value.retry_after == 30


def test_500_retries_twice_then_unavailable(monkeypatch):
    _no_fallback(monkeypatch)
    calls: list[int] = []

    async def boom(*a, **k):
        calls.append(1)
        raise Boom(502)

    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(llm_client, "_once", boom)
    monkeypatch.setattr(llm_client, "_sleep", fake_sleep)

    with pytest.raises(llm_client.LLMUnavailable):
        asyncio.run(llm_client.llm_func("hi"))

    assert len(calls) == 3


def test_400_not_retried(monkeypatch):
    """参数写错（400）重试多少次都是 400：不重试，也不能报成"上游不可用"。

    503 会让人以为是上游抖动、等 30 秒再来；400 是我们自己把请求写错了，
    要报 500，排查方向才对。
    """
    _no_fallback(monkeypatch)
    calls: list[int] = []

    async def boom(*a, **k):
        calls.append(1)
        raise Boom(400)

    monkeypatch.setattr(llm_client, "_once", boom)

    with pytest.raises(llm_client.LLMError) as ei:
        asyncio.run(llm_client.llm_func("hi"))

    assert len(calls) == 1
    assert not isinstance(ei.value, llm_client.LLMUnavailable)
    assert ei.value.status == 500


# --------------------------------------------- 3. SSE：503 先发 error 再 close
@pytest.fixture()
def sse_app(monkeypatch):
    """把 app 的流水线换成假的：不碰索引、不碰模型，只走到"建流失败"这一步。"""
    pytest.importorskip("lightrag", reason="app.main 会连带 import query → lightrag")
    pytest.importorskip("fastapi.testclient")

    import app.main as M

    class _DM:
        async def condense(self, q, history):
            return q

        async def retrieve(self, *a, **k):
            return ([{"content": "证据块", "file_path": "a.md"}], False)

        async def judge(self, q, chunks):
            return True, "证据块"

    class _Lease:
        async def __aenter__(self):
            return _DM()

        async def __aexit__(self, *exc):
            return False

    class _Pool:
        def lease(self, tenant):
            return _Lease()

    monkeypatch.setitem(M.STATE, "pool", _Pool())
    monkeypatch.setitem(M.STATE, "gates", {})
    monkeypatch.setitem(M.STATE, "base_tag", "test")
    monkeypatch.setitem(M.STATE, "keys", {})
    return M


def test_sse_emits_error_then_closes(sse_app, monkeypatch):
    M = sse_app

    async def boom_stream(*a, **k):
        raise M.LLMUnavailable("upstream 503")
        yield  # pragma: no cover  —— 让它成为异步生成器

    from fastapi.testclient import TestClient

    # app.main 在模块级 `from llm_client import llm_stream`，所以换的是模块里的名字；
    # 用 setitem 让 monkeypatch 测试结束后自动还原，避免污染其它用例。
    monkeypatch.setitem(M.__dict__, "llm_stream", boom_stream)

    # 不进 with：不触发 lifespan，就不会去建索引/加载模型（这个用例只测错误分支）
    client = TestClient(M.app)
    r = client.post("/v1/ask/stream", json={"question": "XX 的默认值是多少"})
    body = r.text

    assert "event: error" in body, body[-400:]
    assert '"status": 503' in body
    assert '"retry_after": 30' in body
    assert "event: done" in body, "发完 error 必须补一个 done 再关流"
    # 顺序：error 在前，done 在后
    assert body.index("event: error") < body.index("event: done")


# --------------------------------- 4. 默认关闭时 qwen provider 不被 import
def test_qwen_not_imported_when_disabled(monkeypatch):
    monkeypatch.setenv("LLM_FALLBACK_MODEL", "qwen-plus")
    monkeypatch.setenv("LLM_FALLBACK_API_KEY", "sk-fake")
    monkeypatch.setenv("LLM_FALLBACK_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("LLM_FALLBACK_ENABLED", "0")
    monkeypatch.delenv("LLM_RETRIES", raising=False)
    sys.modules.pop("llm_fallback", None)

    assert llm_client.fallback_enabled() is False, "默认必须是关"

    async def boom(*a, **k):
        raise Boom(429)

    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(llm_client, "_once", boom)
    monkeypatch.setattr(llm_client, "_sleep", fake_sleep)

    with pytest.raises(llm_client.LLMUnavailable):
        asyncio.run(llm_client.llm_func("hi"))

    assert "llm_fallback" not in sys.modules, "关着 failover 却 import 了 qwen provider"


def test_qwen_imported_only_after_enable(monkeypatch):
    """反向确认：开关不是死的——显式打开后确实会去加载 provider。"""
    monkeypatch.setenv("LLM_FALLBACK_MODEL", "qwen-plus")
    monkeypatch.setenv("LLM_FALLBACK_API_KEY", "sk-fake")
    monkeypatch.setenv("LLM_FALLBACK_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("LLM_FALLBACK_ENABLED", "1")
    sys.modules.pop("llm_fallback", None)

    assert llm_client.fallback_enabled() is True
    llm_client.fallback_client()  # 构造 AsyncOpenAI 不发网络请求
    assert "llm_fallback" in sys.modules
