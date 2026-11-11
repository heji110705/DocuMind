"""在 batch_size=8 下扫 (截断长度 × 候选数)，找出"质量够、又能进 5 秒"的点。

前一轮的结论：
  - 所谓"192 字符断崖"是 batch_size=20 的病态路径（同一组 5 次重复稳定 41s），
    batch_size=8 时 512 字符只要 3.9s / 20 条，不存在断崖。
  - threads 显式设 4 或 8 反而更慢（150ms → 231/300ms 每条），默认(全核)最好。

所以只剩一个自由度：给多少条、每条看多少字。
"""

from __future__ import annotations

import os
import statistics
import sys
import time
from pathlib import Path

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

QUERY = "订单事实表的粒度是什么，一单多书会重复计数吗"


def load_chunks(n: int) -> list[str]:
    """取真实 chunk：按 chunk_size=600 token 切，接近线上长度。"""
    files = sorted((ROOT / "seed" / "v2").glob("*.md"))
    chunks: list[str] = []
    for f in files:
        buf = ""
        for para in f.read_text(encoding="utf-8").split("\n\n"):
            p = para.strip()
            if not p:
                continue
            buf = (buf + "\n\n" + p).strip() if buf else p
            if len(buf) >= 900:          # 约等于 600 token 的中文 chunk
                chunks.append(buf)
                buf = ""
            if len(chunks) >= n:
                return chunks
    return chunks


def main() -> None:
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    model = TextCrossEncoder(model_name="BAAI/bge-reranker-v2-m3-int8")
    pool = load_chunks(20)
    inner = model.model if hasattr(model, "model") else model
    inner._ensure_tokenizer()
    lens = [len(c) for c in pool]
    print(f"候选池 {len(pool)} 条，长度 {min(lens)}~{max(lens)} 字符，均值 {sum(lens)//len(lens)}\n")

    print(f"{'docs':>5} {'chars':>6} {'tokens/条':>9} {'min ms':>8} {'p50 ms':>8} {'ms/条':>7}")
    for n_docs in (8, 12, 16, 20):
        docs_all = pool[:n_docs]
        for chars in (384, 512, 768, 1024):
            docs = [d[:chars] for d in docs_all]
            tok = statistics.mean(len(inner.tokenizer.encode(QUERY, d).ids) for d in docs)
            ts = []
            for _ in range(5):
                t0 = time.perf_counter()
                list(model.rerank(QUERY, docs, batch_size=8))
                ts.append((time.perf_counter() - t0) * 1000)
            print(f"{n_docs:>5} {chars:>6} {tok:>9.0f} {min(ts):>8.0f} "
                  f"{statistics.median(ts):>8.0f} {min(ts)/n_docs:>7.0f}")
        print()


if __name__ == "__main__":
    main()
