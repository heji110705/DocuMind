"""上传内容的限量读取。

**为什么单独一个模块**：这段逻辑本来写在 ``app/main.py`` 里，而 main 会连带
导入 fastapi + lightrag（几个 GB）。结果是"上传超限"这么纯的逻辑，只能在全量
依赖下被测到——CI 的 unit job 只装 requirements-fast.txt，一 import 就红
（2026-11-16 实际踩到：提交 c956b39 把 CI 弄红了）。

所以这里只依赖标准库：谁需要限量读，就 import 它，不牵连整个服务。
它也不认识 HTTP 状态码——``UploadTooLarge`` 只描述事实，翻译成 413 是
web 层的事（``app/main.py`` 里映射成 E2203）。
"""
from __future__ import annotations

from typing import Any

__all__ = ["UploadTooLarge", "read_upload"]

CHUNK_SIZE = 1 << 20


class UploadTooLarge(Exception):
    """读到的字节数超过上限。带上限值，方便上层拼出可读的错误信息。"""

    def __init__(self, max_bytes: int):
        super().__init__(f"超过 {max_bytes} 字节上限")
        self.max_bytes = max_bytes


async def read_upload(file: Any, max_bytes: int, chunk_size: int = CHUNK_SIZE) -> bytes:
    """按块读取，超限立刻停。

    为什么不是 ``await file.read()``：那会把整个请求体先读进内存，再在
    ``save_bytes`` 里检查上限——一个 500MB 的上传会先把内存吃光才报错。
    在 1.7G 的机器上，这不是理论风险。

    两道闸：先看调用方已经算好的 ``file.size``（可能为 None），再边读边数。
    声明长度可能缺失（chunked 传输），所以后者不能省。
    """
    declared = getattr(file, "size", None)
    if isinstance(declared, int) and declared > max_bytes:
        raise UploadTooLarge(max_bytes)

    buf = bytearray()
    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise UploadTooLarge(max_bytes)
    return bytes(buf)
