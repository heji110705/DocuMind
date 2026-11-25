"""访客 IP 解析：限流的唯一身份来源。

抽成独立模块的理由：这里全是纯函数（只看 request 和几个环境变量），
但它原来住在 ``app/main.py`` 里，而 ``app/main.py`` 一导入就拖着
``app.tenancy`` → ``query`` → ``lightrag`` 一整条重依赖链。结果是
CI 里那个「单元测试（不依赖 lightrag）」的 job **根本导不进这个函数**，
测试只能靠 ``importorskip`` 跳过——而跳过等于没测。

这个模块不 import 任何项目内模块，只依赖 starlette 的 Request。
"""

from __future__ import annotations

import ipaddress
import os

from starlette.requests import Request


def trust_proxy() -> bool:
    """是否相信 ``X-Forwarded-For``。默认 **不信**（``TRUST_PROXY=0``）。

    为什么默认关：线上是 uvicorn 直连 80 端口（iptables DNAT 保留源地址），
    前面没有反向代理。此时任何客户端都能自己塞一个
    ``X-Forwarded-For: 1.2.3.4``，而按 IP 的限流器会把它当成"另一个访客"——
    一个 header 就换来无限额度，``OPEN_IP_PER_DAY`` 等于不存在。

    只有真的挂了反代（nginx / Cloudflare）才开；开了必须让反代**覆盖**掉
    客户端传来的 XFF，而不是追加（``proxy_set_header X-Forwarded-For $remote_addr;``）。
    """
    return os.environ.get("TRUST_PROXY", "0").strip().lower() not in {
        "", "0", "false", "no", "off"
    }


def valid_ip(text: str) -> str | None:
    """把一段文本当 IP 解析；不合法返回 None（绝不抛异常）。"""
    try:
        return str(ipaddress.ip_address(text))
    except (ValueError, TypeError):
        return None


def client_ip(request: Request) -> str:
    """取访客 IP。三档，由 ``TRUST_PROXY`` 决定。

    * ``TRUST_PROXY=0``（默认）：**只认 socket peer**，``X-Forwarded-For`` /
      ``X-Real-IP`` 一律忽略。直连部署下这两个头都是客户端可写的，没有可信度。
    * ``TRUST_PROXY=1``：取 ``X-Forwarded-For`` 的**最后一段**。不是第一段——
      第一段是客户端自己写的，反代只会把自己看到的地址追加在末尾；从后往前
      取第一个合法 IP，才落在"反代亲自看到的那一跳"上。
    * 解析不出来（空值、畸形、带端口的伪 IP）：回退 socket peer。宁可少识别
      一次，也不能因为一个坏 header 把请求打成 500。

    边界：``X-Real-IP`` 在任何模式下都不用——它没有标准的多跳语义，同样是
    客户端可写的头，多支持一个就多一个绕过口。
    """
    peer = request.client.host if request.client else "unknown"
    if not trust_proxy():
        return peer

    forwarded = request.headers.get("x-forwarded-for", "")
    for part in reversed(forwarded.split(",")):
        candidate = valid_ip(part.strip())
        if candidate:
            return candidate
    return peer
