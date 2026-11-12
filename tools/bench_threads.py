"""同一模型只改线程数：cross-encoder 在 CPU 上的延迟对照。

为什么值得单独做这件事：README 第 14 节把"重排慢"归因于功耗墙，结论是"没有便宜的解法"。
但那个结论是在**默认线程数**下得到的。ONNX Runtime 默认按物理核数开 intra-op 线程，
在 i9-12900H 这种大小核混排的 CPU 上，线程数一多就会：
  1. 大核被小核的同步拖住（线程要等最慢的那个）；
  2. 更大的算子并行度换来更高的功耗，触发降频。
这类"线程数从 20 降到 4 反而更快"的现象在小模型上非常常见，而且**不影响结果精度**——
同一个模型、同一份输入、同样的 batch，只是算得更紧凑。

用法：
    python tools/bench_threads.py BAAI/bge-reranker-v2-m3-int8 1 2 4 8
    python tools/bench_threads.py BAAI/bge-reranker-base 4 8 --docs 16
"""

from __future__ import annotations

import argparse
import os
import statistics
import time

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

FILLER = (
    "订单事实表的粒度是一单一书，order_id 唯一，line_no 从 1 开始；"
    "stat_date 由 pay_time 派生，跨天支付要按支付日期归属；"
    "gmv_net 扣除 refunded 状态，但处理中的退款仍计入；"
    "dim_book 共 42,180 行，rating_avg 采用 0-10 分制；"
    "评分类指标必须过滤 is_valid，否则均值虚高 3.2 分。"
)


def docs(n: int, chars: int = 512) -> list[str]:
    body = (FILLER * 8)[:chars]
    return [f"[{i:02d}] {body}" for i in range(n)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("model")
    ap.add_argument("threads", nargs="+", type=int)
    ap.add_argument("--docs", type=int, default=32)
    ap.add_argument("--chars", type=int, default=512)
    ap.add_argument("--batch", type=int, default=int(os.environ.get("RERANK_BATCH_SIZE", "8")))
    ap.add_argument("--reps", type=int, default=2)
    a = ap.parse_args()

    from fastembed.rerank.cross_encoder import TextCrossEncoder

    q, d = "评分类指标为什么必须过滤 is_valid？", docs(a.docs, a.chars)
    print(f"模型 {a.model}｜{a.docs} 条 × {a.chars} 字｜batch {a.batch}｜reps {a.reps}")
    print(f"{'threads':>8} {'min':>8} {'p50':>8} {'max':>8}")
    for th in a.threads:
        t0 = time.perf_counter()
        m = TextCrossEncoder(model_name=a.model, threads=th)
        load = time.perf_counter() - t0
        list(m.rerank(q, d[:2], batch_size=a.batch))  # 预热：避开首次 shape 编译
        ts = []
        for _ in range(a.reps):
            t1 = time.perf_counter()
            list(m.rerank(q, d, batch_size=a.batch))
            ts.append((time.perf_counter() - t1) * 1000)
        print(f"{th:>8} {min(ts):>7.0f}ms {statistics.median(ts):>7.0f}ms {max(ts):>7.0f}ms"
              f"   （加载 {load:.0f}s）", flush=True)


if __name__ == "__main__":
    main()
