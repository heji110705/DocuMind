"""租户 → DocuMind 实例的池子。

隔离靠的是**物理目录**：每个租户一个 rag_storage/<tag>__<tenant>，
各自一套向量库、图谱、文档状态。所以"A 上传的文档 B 能不能查到"这个问题
不需要靠过滤条件保证，而是根本不在同一个库里——这是唯一一种演示时能自证的隔离。

池子的两件事：
1. 懒加载 + LRU：每个实例要加载 embedding 模型与图谱，几十 MB 到几百 MB，
   不可能每个租户常驻。默认最多同时开 3 个，超了关掉最久没用的那个。
2. 引用计数：正在被请求使用的实例不关。关一个正在跑的 LightRAG 会让请求
   在 finalize 之后的某次写入上炸掉，而那种报错极难和"租户"联系起来。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from collections import OrderedDict

from app.auth import Tenant
from query import DocuMind


class TenantPool:
    def __init__(
        self,
        embed: str = "local",
        use_rerank: bool = True,
        capacity: int = 3,
        create_if_missing: bool = False,
    ):
        self.embed = embed
        self.use_rerank = use_rerank
        self.capacity = max(1, capacity)
        # 新建的空租户索引要允许自动创建；默认租户不创建——本地索引没建好时
        # 报错比悄悄起一个空库好，否则界面上每一问都是"证据不足"。
        self.create_if_missing = create_if_missing
        self._items: "OrderedDict[str, DocuMind]" = OrderedDict()
        self._active: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def get(self, tenant: Tenant) -> DocuMind:
        async with self._lock:
            dm = self._items.get(tenant.tid)
            if dm is not None:
                self._items.move_to_end(tenant.tid)
                return dm
            await self._evict_locked()
            allow_create = self.create_if_missing and tenant.tid != "default"
            dm = DocuMind(tenant.index_tag, self.embed, use_rerank=self.use_rerank)
            try:
                dm = await dm.open(create_if_missing=allow_create)
            except SystemExit as e:  # DocuMind.open 用 SystemExit 表达"索引不存在"
                raise RuntimeError(f"租户 {tenant.tid} 的索引不可用：{e}") from e
            # 检索锁由服务端注入：只有 retrieve / ingest 那一段串行（ONNX 重排是 CPU 独占），
            # 判定和作答不排队。
            dm.lock = asyncio.Lock()
            self._items[tenant.tid] = dm
            self._active.setdefault(tenant.tid, 0)
            return dm

    @asynccontextmanager
    async def lease(self, tenant: Tenant):
        dm = await self.get(tenant)
        self._active[tenant.tid] = self._active.get(tenant.tid, 0) + 1
        try:
            yield dm
        finally:
            self._active[tenant.tid] = max(0, self._active.get(tenant.tid, 1) - 1)

    async def _evict_locked(self) -> None:
        while len(self._items) >= self.capacity:
            victim = next((tid for tid in self._items if self._active.get(tid, 0) == 0), None)
            if victim is None:
                # 全都有人在用：允许临时超编，也不强杀请求。
                return
            dm = self._items.pop(victim)
            self._active.pop(victim, None)
            try:
                await dm.close()
            except Exception:
                pass

    async def close_all(self) -> None:
        for dm in list(self._items.values()):
            try:
                await dm.close()
            except Exception:
                pass
        self._items.clear()
        self._active.clear()

    def stats(self) -> dict:
        return {
            "open": list(self._items),
            "capacity": self.capacity,
            "active": dict(self._active),
        }
