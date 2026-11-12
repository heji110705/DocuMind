"""换重排模型前后，跑同一批输入的延迟对照。

用法：
    python tools/bench_model.py BAAI/bge-reranker-v2-m3-int8
    python tools/bench_model.py BAAI/bge-reranker-base

为什么单独写一个脚本：README 第 14 节已经证明"同一输入重复跑"在这台机器上
会出现 350ms → 4710ms 的抖动（功耗墙），所以对照必须在**同一次进程、相邻时刻**
做两遍，并且报告 min / 中位数而不是单次值。跨进程比"某一次"的数字没有意义。
"""

from __future__ import annotations

import os
import statistics
import sys
import time

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

# 语料里 chunk 的中位数是 973 字，重排只取前 512 字（见 rerankers.MAX_CHARS）
FILLER = (
    "订单事实表的粒度是一单一书，order_id 唯一，line_no 从 1 开始；"
    "stat_date 由 pay_time 派生，跨天支付要按支付日期归属；"
    "gmv_net 扣除 refunded 状态，但处理中的退款仍计入；"
    "dim_book 共 42,180 行，rating_avg 采用 0-10 分制；"
    "评分类指标必须过滤 is_valid，否则均值虚高 3.2 分。"
)


def make_docs(n: int, chars: int = 512) -> list[str]:
    body = (FILLER * 8)[:chars]
    return [f"[{i:02d}] {body}" for i in range(n)]


def timeit(model, query: str, docs: list[str], batch: int, repeats: int) -> list[float]:
    out = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        list(model.rerank(query, docs, batch_size=batch))
        out.append((time.perf_counter() - t0) * 1000)
    return out


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "BAAI/bge-reranker-base"
    batch = int(os.environ.get("RERANK_BATCH_SIZE", "8"))
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    t0 = time.perf_counter()
    model = TextCrossEncoder(model_name=name)
    load_s = time.perf_counter() - t0
    print(f"模型      : {name}")
    print(f"加载耗时  : {load_s:.1f}s（含首次下载/编译）", flush=True)

    q = "评分类指标为什么必须过滤 is_valid？"
    for n, repeats in ((8, 5), (32, 3)):
        docs = make_docs(n)
        ts = timeit(model, q, docs, batch, repeats)
        print(
            f"{n:>2} 条 × {len(docs[0])} 字："
            f"min {min(ts):.0f}ms  p50 {statistics.median(ts):.0f}ms  max {max(ts):.0f}ms  "
            f"（{repeats} 次）",
            flush=True,
        )


if __name__ == "__main__":
    main()
