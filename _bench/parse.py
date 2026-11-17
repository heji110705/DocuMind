"""解析 psql 输出，算 recall@10 与延迟分位。

输入是 run_exact.sql / run_hnsw.sql / run_ivfflat.sql 的 stdout（含 ``===QID`` 标记）。
输出一个可直接贴进 README 的表。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

TIME_RE = re.compile(r"Execution Time:\s*([\d.]+)\s*ms")
ROW_RE = re.compile(r"^(Q\d+),([^,]+),([-\d.]+)$")


def parse(text: str) -> dict[str, dict]:
    """→ {qid: {"ms": float, "top": [chunk_id, ...]}}"""
    out: dict[str, dict] = {}
    for block in text.split("===")[1:]:
        head, _, body = block.partition("\n")
        qid = head.strip()
        if not qid:
            continue
        m = TIME_RE.search(body)
        rows = [r for r in (ROW_RE.match(line.strip()) for line in body.splitlines()) if r]
        out[qid] = {
            "ms": float(m.group(1)) if m else None,
            # 去重保序：同一条 SQL 里 vector 出现两次不影响，但 id 可能重复出现
            "top": list(dict.fromkeys(r.group(2) for r in rows)),
        }
    return out


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    vals = sorted(values)
    k = max(0, min(len(vals) - 1, int(round((p / 100) * (len(vals) - 1)))))
    return vals[k]


def main() -> int:
    root = Path(__file__).resolve().parent
    exact = parse((root / "out_exact.txt").read_text(encoding="utf-8"))
    rows = []
    for name in ("hnsw", "ivfflat", "nano"):
        fp = root / f"out_{name}.txt"
        if not fp.exists():
            continue
        got = parse(fp.read_text(encoding="utf-8"))
        recalls, ms = [], []
        for qid, base in exact.items():
            hit = got.get(qid)
            if not hit or hit["ms"] is None:
                continue
            truth = set(base["top"])
            recalls.append(len(truth & set(hit["top"])) / max(1, len(truth)))
            ms.append(hit["ms"])
        if not recalls:
            continue
        rows.append(
            {
                "impl": name,
                "n": len(recalls),
                "recall@10": sum(recalls) / len(recalls),
                "p50_ms": pct(ms, 50),
                "p99_ms": pct(ms, 99),
                "max_ms": max(ms),
            }
        )

    base_ms = [v["ms"] for v in exact.values() if v["ms"] is not None]
    print(f"基准（精确全表扫描，n={len(base_ms)}）：p50 {pct(base_ms,50):.3f} ms · "
          f"p99 {pct(base_ms,99):.3f} ms")
    print()
    print("| 索引 | n | recall@10 | p50 (ms) | p99 (ms) | max (ms) |")
    print("|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['impl']} | {r['n']} | {r['recall@10']:.3f} | "
              f"{r['p50_ms']:.3f} | {r['p99_ms']:.3f} | {r['max_ms']:.3f} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
