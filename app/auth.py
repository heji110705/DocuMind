"""API Key → 租户，以及每租户的配额与并发闸门。

为什么现在做这件事（README 里原先写的是"不做鉴权，加个假 token 反而让人以为它安全"）：
那句话针对的是"演示页上挂一个写死的 token"。这次做的是另一件事——
**让索引在物理上隔离**：每个租户一个 rag_storage 目录，A 上传的文档 B 检索不到。
没有这一步，"多租户"就只能写在文档里，演示时无法证明。

边界也说清楚：
- 这是**单进程内**的隔离，适合"一个容器扛几个小客户"或本地演示。
- 不是 OAuth、没有用户体系、没有审计日志、没有密钥轮换。
  真要上生产，key 该由网关或 IdP 签发，这里只做"识别租户 + 限流"。
- 不配置 API_KEYS 时保持本地开放模式（随便问），因为演示项目默认要能跑起来。

配置格式（.env）：
    API_KEYS=sk-demo-2f8a1c:demo:500:2,sk-alpha-91bd3e:alpha:200:1
    字段：key:租户名[:每日配额[:并发上限]]
    租户名只允许小写字母/数字/下划线/连字符，避免拼路径时穿越目录。
"""

from __future__ import annotations

import hmac
import re
from dataclasses import dataclass, field
from datetime import date

TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
MIN_KEY_LEN = 8  # 比这短的 key 一秒就能猜出来，不如不设


class AuthError(Exception):
    """鉴权/配额失败。带上 HTTP 状态码，端点里直接照着返回。"""

    def __init__(self, code: str, status: int, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message
        self.retry_after = retry_after


@dataclass(frozen=True)
class Tenant:
    tid: str
    index_tag: str
    daily_quota: int = 0      # 0 = 不限
    concurrency: int = 2      # 同时在跑的请求上限


@dataclass
class Gate:
    """一个租户的闸门：当日配额 + 在飞请求数。

    并发用"超了就拒"而不是排队：排队会让高精度模式（一次 20 秒）把后面
    所有请求一起拖死，而用户看到的只是一个转圈的页面。直接 429 + Retry-After
    至少让人知道发生了什么。
    """

    tenant: Tenant
    _day: date = field(default_factory=date.today)
    _used: int = 0
    _inflight: int = 0

    def _roll(self) -> None:
        today = date.today()
        if today != self._day:
            self._day = today
            self._used = 0

    def acquire(self) -> None:
        self._roll()
        if self.tenant.daily_quota and self._used >= self.tenant.daily_quota:
            raise AuthError(
                "E2103", 429,
                f"租户 {self.tenant.tid} 今日配额已用完（{self.tenant.daily_quota} 次）",
                retry_after=3600,
            )
        if self._inflight >= self.tenant.concurrency:
            raise AuthError(
                "E2104", 429,
                f"租户 {self.tenant.tid} 并发已满（{self.tenant.concurrency}），稍后重试",
                retry_after=2,
            )
        self._used += 1
        self._inflight += 1

    def release(self) -> None:
        self._inflight = max(0, self._inflight - 1)

    def snapshot(self) -> dict:
        self._roll()
        return {
            "tenant": self.tenant.tid,
            "index": self.tenant.index_tag,
            "quota": self.tenant.daily_quota or None,
            "used_today": self._used,
            "inflight": self._inflight,
            "concurrency": self.tenant.concurrency,
        }


def parse_keys(spec: str | None, base_tag: str) -> dict[str, Tenant]:
    """解析 API_KEYS。格式错误直接抛 ValueError——启动时就炸，别等到线上。"""
    out: dict[str, Tenant] = {}
    for chunk in (spec or "").replace("\n", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":")
        if len(parts) < 2:
            raise ValueError(f"API_KEYS 条目格式应为 key:租户名[:配额[:并发]]，收到 {chunk!r}")
        key, tid = parts[0].strip(), parts[1].strip()
        if len(key) < MIN_KEY_LEN:
            raise ValueError(f"API Key 太短（至少 {MIN_KEY_LEN} 位）：{key!r}")
        if not TENANT_RE.match(tid):
            raise ValueError(f"租户名只能是小写字母/数字/下划线/连字符：{tid!r}")
        quota = int(parts[2]) if len(parts) > 2 and parts[2].strip() else 0
        conc = int(parts[3]) if len(parts) > 3 and parts[3].strip() else 2
        if quota < 0 or conc < 1:
            raise ValueError(f"配额/并发必须为正：{chunk!r}")
        if any(t.tid == tid for t in out.values()):
            raise ValueError(f"租户名重复：{tid}")
        # 默认租户沿用主索引（本地演示那套），其他租户各自一个目录，物理隔离。
        tag = base_tag if tid == "default" else f"{base_tag}__{tid}"
        out[key] = Tenant(tid=tid, index_tag=tag, daily_quota=quota, concurrency=conc)
    return out


def resolve(header: str | None, tenants: dict[str, Tenant], base_tag: str) -> Tenant:
    """从请求头取租户。

    没配置 key → 开放模式，返回默认租户（本地演示零配置）。
    配置了 key → 必须带对，错和缺都回 401 且**不区分**，避免用错误信息探测 key。
    """
    if not tenants:
        return Tenant(tid="default", index_tag=base_tag)

    raw = (header or "").strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    if not raw:
        raise AuthError("E2101", 401, "缺少 API Key（请求头 X-API-Key）")

    for key, tenant in tenants.items():
        if hmac.compare_digest(key, raw):
            return tenant
    raise AuthError("E2101", 401, "API Key 无效")
