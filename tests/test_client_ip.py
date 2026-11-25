"""``_client_ip`` 的单测：这是限流的唯一身份来源，错了等于没有限流。

背景（真漏洞）：线上是 uvicorn 直连 80 端口，前面没有反代，而旧实现优先取
``X-Forwarded-For`` 的第一段。任何人加一个 ``X-Forwarded-For: 随机IP`` 就在
限流器眼里换了一个人，可以无限刷——``OPEN_IP_PER_DAY`` 形同虚设。

所以这里钉三件事：
  1. 默认（TRUST_PROXY=0）下，伪造 XFF 改不了 client_ip；
  2. 开了 TRUST_PROXY=1，取的是**最后一段**（反代亲自看到的那一跳）；
  3. 空值 / 畸形值不抛异常，回退到 socket peer。
"""
from __future__ import annotations

import pytest
from starlette.requests import Request


def make_request(headers: dict[str, str] | None = None, peer: str = "9.9.9.9") -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/ask",
        "headers": raw,
        "client": (peer, 54321),
    }
    return Request(scope)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("TRUST_PROXY", raising=False)
    yield


def _client_ip(request: Request) -> str:
    # 从 app.net 导入，不是 app.main —— 后者会拖着 lightrag 整条依赖链，
    # 而 CI 的「单元测试（不依赖 lightrag）」job 不装它，导不进来等于没测。
    from app.net import client_ip as fn
    return fn(request)


class TestTrustProxyOff:
    """默认档：直连部署，任何转发头都不可信。"""

    def test_forged_xff_is_ignored(self):
        req = make_request({"X-Forwarded-For": "1.2.3.4"})
        assert _client_ip(req) == "9.9.9.9"

    def test_forged_xff_chain_is_ignored(self):
        req = make_request({"X-Forwarded-For": "1.2.3.4, 5.6.7.8, 10.9.9.1"})
        assert _client_ip(req) == "9.9.9.9"

    def test_x_real_ip_is_ignored(self):
        req = make_request({"X-Real-IP": "1.2.3.4"})
        assert _client_ip(req) == "9.9.9.9"

    def test_explicit_zero(self, monkeypatch):
        monkeypatch.setenv("TRUST_PROXY", "0")
        req = make_request({"X-Forwarded-For": "1.2.3.4"})
        assert _client_ip(req) == "9.9.9.9"

    def test_false_like_values_are_off(self, monkeypatch):
        for value in ("false", "no", "off", " "):
            monkeypatch.setenv("TRUST_PROXY", value)
            req = make_request({"X-Forwarded-For": "1.2.3.4"})
            assert _client_ip(req) == "9.9.9.9", value


class TestTrustProxyOn:
    def test_takes_last_segment(self, monkeypatch):
        monkeypatch.setenv("TRUST_PROXY", "1")
        req = make_request({"X-Forwarded-For": "1.2.3.4, 5.6.7.8"})
        assert _client_ip(req) == "5.6.7.8"

    def test_single_value(self, monkeypatch):
        monkeypatch.setenv("TRUST_PROXY", "1")
        assert _client_ip(make_request({"X-Forwarded-For": "1.2.3.4"})) == "1.2.3.4"

    def test_skips_empty_segments(self, monkeypatch):
        monkeypatch.setenv("TRUST_PROXY", "1")
        req = make_request({"X-Forwarded-For": "1.2.3.4, , 5.6.7.8 ,"})
        assert _client_ip(req) == "5.6.7.8"

    def test_ipv6(self, monkeypatch):
        monkeypatch.setenv("TRUST_PROXY", "1")
        req = make_request({"X-Forwarded-For": "2001:db8::1, 2001:db8::2"})
        assert _client_ip(req) == "2001:db8::2"

    def test_no_header_falls_back_to_peer(self, monkeypatch):
        monkeypatch.setenv("TRUST_PROXY", "1")
        assert _client_ip(make_request()) == "9.9.9.9"


class TestMalformed:
    """畸形值不许抛异常——抛了就是 500，攻击者用一行 header 就能打死接口。"""

    @pytest.mark.parametrize("value", [
        "", " ", "not-an-ip", "1.2.3.4:8080", "999.1.1.1", "1.2.3", "<script>",
    ])
    def test_bad_value_falls_back_to_peer(self, monkeypatch, value):
        monkeypatch.setenv("TRUST_PROXY", "1")
        req = make_request({"X-Forwarded-For": value})
        assert _client_ip(req) == "9.9.9.9"

    def test_peer_unknown_still_returns_string(self, monkeypatch):
        """没有 client 的场景（ASGI 里确实存在）也要给个字符串，别返回 None。"""
        monkeypatch.setenv("TRUST_PROXY", "1")
        scope = {"type": "http", "method": "POST", "path": "/api/ask",
                 "headers": [], "client": None}
        assert _client_ip(Request(scope)) == "unknown"

    def test_mixed_bad_then_good_takes_good(self, monkeypatch):
        monkeypatch.setenv("TRUST_PROXY", "1")
        req = make_request({"X-Forwarded-For": "1.2.3.4, garbage, 5.6.7.8"})
        assert _client_ip(req) == "5.6.7.8"
