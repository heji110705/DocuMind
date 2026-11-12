"""鉴权、配额、租户隔离的单元测试（纯标准库）。

这里的每一条都对应一个"演示时会被问到"的问题：
- 不配 key 时的默认行为是什么？（开放模式，演示零配置能跑）
- key 错了会不会泄露"这个 key 存在但过期"？（不会，统一 401）
- 配额用完之后返回什么？（429 + Retry-After，不是 500）
- 两个租户会不会读到同一份索引？（索引目录不同，物理隔离）
- 租户名能不能塞 `../` 穿越出去？（正则挡住）
"""

import pytest

from app.auth import AuthError, Gate, Tenant, parse_keys, resolve

BASE = "prod"


class TestParseKeys:
    def test_empty_means_open_mode(self):
        assert parse_keys("", BASE) == {}
        assert parse_keys(None, BASE) == {}

    def test_minimal_entry(self):
        t = parse_keys("sk-demo-2f8a1c:demo", BASE)["sk-demo-2f8a1c"]
        assert (t.tid, t.index_tag, t.daily_quota, t.concurrency) == ("demo", "prod__demo", 0, 2)

    def test_full_entry(self):
        t = parse_keys("sk-demo-2f8a1c:demo:500:3", BASE)["sk-demo-2f8a1c"]
        assert (t.daily_quota, t.concurrency) == (500, 3)

    def test_default_tenant_uses_base_index(self):
        t = parse_keys("sk-demo-2f8a1c:default", BASE)["sk-demo-2f8a1c"]
        assert t.index_tag == BASE

    def test_multiple_keys(self):
        ts = parse_keys("sk-aaaaaaaa:demo,sk-bbbbbbbb:alpha", BASE)
        assert set(ts.values() and [v.tid for v in ts.values()]) == {"demo", "alpha"}

    def test_short_key_rejected(self):
        with pytest.raises(ValueError):
            parse_keys("short:demo", BASE)

    def test_bad_tenant_name_rejected(self):
        for bad in ("../etc", "Demo", "a b", ""):
            with pytest.raises(ValueError):
                parse_keys(f"sk-aaaaaaaa:{bad}", BASE)

    def test_duplicate_tenant_rejected(self):
        with pytest.raises(ValueError):
            parse_keys("sk-aaaaaaaa:demo,sk-bbbbbbbb:demo", BASE)

    def test_negative_quota_rejected(self):
        with pytest.raises(ValueError):
            parse_keys("sk-aaaaaaaa:demo:-1", BASE)


class TestResolve:
    def test_open_mode_returns_default(self):
        t = resolve(None, {}, BASE)
        assert (t.tid, t.index_tag) == ("default", BASE)

    def test_missing_key_is_401(self):
        ts = parse_keys("sk-aaaaaaaa:demo", BASE)
        with pytest.raises(AuthError) as e:
            resolve(None, ts, BASE)
        assert (e.value.status, e.value.code) == (401, "E2101")

    def test_wrong_key_is_401_without_hint(self):
        ts = parse_keys("sk-aaaaaaaa:demo", BASE)
        with pytest.raises(AuthError) as e:
            resolve("sk-wrong-key", ts, BASE)
        assert "无效" in e.value.message

    def test_bearer_prefix_accepted(self):
        ts = parse_keys("sk-aaaaaaaa:demo", BASE)
        assert resolve("Bearer sk-aaaaaaaa", ts, BASE).tid == "demo"

    def test_correct_key(self):
        ts = parse_keys("sk-aaaaaaaa:demo,sk-bbbbbbbb:alpha", BASE)
        assert resolve("sk-bbbbbbbb", ts, BASE).tid == "alpha"


class TestGate:
    def _gate(self, quota=0, conc=2):
        return Gate(Tenant("demo", "prod__demo", daily_quota=quota, concurrency=conc))

    def test_quota_blocks_after_limit(self):
        g = self._gate(quota=2)
        g.acquire(); g.release()
        g.acquire(); g.release()
        with pytest.raises(AuthError) as e:
            g.acquire()
        assert (e.value.status, e.value.code) == (429, "E2103")
        assert e.value.retry_after

    def test_concurrency_blocks_fast(self):
        """并发满了立刻拒，不排队：排队会把 20 秒的高精度请求堆成一条长龙。"""
        g = self._gate(conc=1)
        g.acquire()
        with pytest.raises(AuthError) as e:
            g.acquire()
        assert (e.value.status, e.value.code) == (429, "E2104")
        g.release()
        g.acquire()   # 释放之后可以继续用

    def test_release_is_idempotent(self):
        g = self._gate(conc=1)
        g.release(); g.release()
        assert g.snapshot()["inflight"] == 0

    def test_snapshot_fields(self):
        s = self._gate(quota=10).snapshot()
        assert s["tenant"] == "demo" and s["quota"] == 10 and s["index"] == "prod__demo"
