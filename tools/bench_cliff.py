"""384 字符 → 199ms/条，512 字符 → 2230ms/条。这个 11 倍跳跃是不是真的？

如果是，那最优解不是 160（砍掉 70% 内容、质量崩）也不是 512（慢 11 倍），
而是贴着跳跃点左边的 384（只少 25% 内容）。

做法：同一批真实 chunk（900~1100 字），只用"每条一次"的打分方式，
在 320~544 之间细扫，每档 5 次重复取最小值——最小值代表纯计算时间，
不会被机器抖动污染。
"""

from __future__ import annotations

import os
import statistics
import sys
import time
from pathlib import Path

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

QUERY = "订单事实表的粒度是什么，一单多书会重复计数吗"


def load_chunks(n: int) -> list[str]:
    out: list[str] = []
    for f in sorted((ROOT / "seed" / "v2").glob("*.md")):
        buf = ""
        for para in f.read_text(encoding="utf-8").split("\n\n"):
            p = para.strip()
            if not p:
                continue
            buf = (buf + "\n\n" + p).strip() if buf else p
            if len(buf) >= 900:
                out.append(buf)
                buf = ""
            if len(out) >= n:
                return out
    return out


def main() -> None:
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    m = TextCrossEncoder(model_name="BAAI/bge-reranker-v2-m3-int8")
    pool = load_chunks(20)
    inner = m.model if hasattr(m, "model") else m
    inner._ensure_tokenizer()

    print(f"{'chars':>6} {'tokens':>7} {'min ms':>8} {'p50 ms':>8} {'ms/条':>7}  32条约")
    for chars in (320, 352, 384, 400, 416, 448, 480, 512):
        docs = [c[:chars] for c in pool]
        tok = statistics.mean(len(inner.tokenizer.encode(QUERY, d).ids) for d in docs)
        ts = []
        for _ in range(5):
            t0 = time.perf_counter()
            list(m.rerank(QUERY, docs, batch_size=8))
            ts.append((time.perf_counter() - t0) * 1000)
        mn, p50 = min(ts), statistics.median(ts)
        print(f"{chars:>6} {tok:>7.0f} {mn:>8.0f} {p50:>8.0f} {mn/len(docs):>7.0f}  "
              f"{mn/len(docs)*32/1000:>6.1f}s")


if __name__ == "__main__":
    main()
