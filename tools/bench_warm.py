"""同一份输入重复 12 次，看延迟是"首次慢后续快"（arena 预热）
还是"随机抖动"（外部内存/CPU 争用）。

顺带打印进程 RSS，确认不是自己把自己顶到换页。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def rss_mb() -> float:
    import ctypes
    from ctypes import wintypes

    class PMC(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    pmc = PMC()
    pmc.cb = ctypes.sizeof(PMC)
    ctypes.windll.psapi.GetProcessMemoryInfo(
        ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb
    )
    return pmc.WorkingSetSize / 1024 / 1024


def main() -> None:
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    q = "订单事实表的粒度是什么，一单多书会重复计数吗"
    body = (
        "订单事实表的粒度为一单一书一行，同一订单含多本书时会重复计数。"
        "正确做法是按订单号去重后再统计订单数。"
    )
    docs = [f"块 {i}：{body}"[:512] for i in range(12)]

    print("加载模型…")
    m = TextCrossEncoder(model_name="BAAI/bge-reranker-v2-m3-int8")
    list(m.rerank(q, docs[:2], batch_size=8))     # 触发完整加载
    print(f"加载后 RSS={rss_mb():.0f} MB\n")

    print("rep  ms      RSS_MB")
    for i in range(12):
        t0 = time.perf_counter()
        list(m.rerank(q, docs, batch_size=8))
        dt = (time.perf_counter() - t0) * 1000
        print(f"{i:>3} {dt:>7.0f} {rss_mb():>7.0f}")


if __name__ == "__main__":
    main()
