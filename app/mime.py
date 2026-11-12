"""把静态资源的 MIME 类型钉死。

为什么值得一个模块：Windows 上 `.js` 的类型来自注册表，很多机器被别的软件
改成了 `text/plain`。浏览器对 `<script type="module">` 的 MIME 检查是硬性的，
类型不对就直接拒绝执行，页面上表现为"HTML 正常、所有交互都没反应"，
控制台里只有一行容易被忽略的警告。这个坑在交付前真的踩过一次。
"""

from __future__ import annotations

import mimetypes


def register() -> None:
    mimetypes.add_type("text/javascript", ".js")
    mimetypes.add_type("text/javascript", ".mjs")
    mimetypes.add_type("text/css", ".css")


def guess(name: str) -> str:
    """返回猜出来的 MIME（先注册再猜，保证不受宿主环境影响）。"""
    register()
    return mimetypes.guess_type(name)[0] or "application/octet-stream"
