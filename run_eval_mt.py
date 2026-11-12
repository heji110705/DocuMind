"""多轮对话的对照评测：带上下文改写 vs 不带，差多少。

为什么要单独一个小评测，而不是塞进 316 题的主表：
主表测的是"单题命中"，多轮测的是"这一轮问的到底是不是上一轮那个对象"，
判定方式不同（第二句的关键词只能从第一句的答案里来），合在一张表里会互相污染。

设计（10 组两轮对话）：
  第一轮：「<指标名>」是怎么算的？        —— 建立"我们在聊哪个指标"
  第二轮：那它有哪些口径坑？              —— 只有"它"指代明确才答得对

两种配置：
  no_history    直接把第二轮丢进检索（= 单轮系统的真实表现）
  with_condense 先把"它"改写成指标名再检索（= 本项目的多轮实现）

判分：第二轮答案里必须出现该指标口径坑中的数字，且不能拒答。
这个关键词只能从第一轮的对象推出来——所以它测的确实是"指代有没有接上"，
而不是"检索准不准"。

用法：
    python run_eval_mt.py
    python run_eval_mt.py --tag mt_prod
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).parent

from evalutil import hit  # noqa: E402
from query import DocuMind  # noqa: E402

# (指标名, 第二轮问题的问法, 该题必须出现的关键词)
CASES = [
    ("订单行数", "那它有哪些口径坑？", ["1.28"]),
    ("DAU", "这个指标的口径要注意什么？", ["31%"]),
    ("渠道 ROI 排名", "那它有哪些口径坑？", ["100"]),
    ("7 日留存", "这个指标的口径要注意什么？", ["D+7"]),
    ("30 日 LTV", "那它有哪些口径坑？", ["30 天窗口"]),
    ("会话数", "这个指标的口径要注意什么？", ["30 分钟"]),
    ("平均评分", "那它有哪些口径坑？", ["is_valid"]),
    ("CVR", "这个指标的口径要注意什么？", ["7 天"]),
    ("销量", "那它有哪些口径坑？", ["赠品"]),
    ("渠道 ROI", "这个指标的口径要注意什么？", ["净额"]),
]


def corpus_text() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted((ROOT / "seed").rglob("*.md")))


def check_cases() -> list[str]:
    """关键词必须真的能在语料里逐字找到，否则这道题永远判错。"""
    text = re.sub(r"[\s`*_#|]+", "", corpus_text())
    bad = []
    for name, t2, keys in CASES:
        for k in keys:
            if re.sub(r"[\s`*_#|]+", "", k) not in text:
                bad.append(f"{name}: 关键词 {k!r} 不在语料里")
    return bad


async def run(dm: DocuMind, tag: str) -> dict:
    rows = []
    for i, (name, t2, keys) in enumerate(CASES, 1):
        t1 = f"「{name}」是怎么算的？"
        a1 = await dm.ask(t1, mode="mix", rerank=False, guard=True)
        history = [{"q": t1, "a": a1.answer[:400]}]

        base = await dm.ask(t2, mode="mix", rerank=False, guard=True)
        ctx = await dm.ask(t2, mode="mix", rerank=False, guard=True, history=history)

        def ok(a) -> bool:
            return (not a.refused) and hit(a.answer, [keys])

        rows.append({
            "id": f"M{i:02d}",
            "metric": name,
            "turn1": t1,
            "turn2": t2,
            "keys": keys,
            "answer1": a1.answer[:300],
            "no_history": {"answer": base.answer[:400], "refused": base.refused,
                           "ok": ok(base), "ms": base.latency_ms},
            "with_condense": {"answer": ctx.answer[:400], "refused": ctx.refused,
                              "ok": ok(ctx), "ms": ctx.latency_ms,
                              "standalone": ctx.extra.get("standalone")},
        })
        print(f"  [{i}/{len(CASES)}] {name}: 单轮 {'✓' if ok(base) else '✗'} "
              f"多轮 {'✓' if ok(ctx) else '✗'} → 改写为「{ctx.extra.get('standalone')}」",
              flush=True)

    def rate(k: str) -> float:
        return round(sum(1 for r in rows if r[k]["ok"]) / len(rows) * 100, 1)

    out = {
        "n": len(rows),
        "no_history_acc": rate("no_history"),
        "with_condense_acc": rate("with_condense"),
        "changed_by_rewrite": sum(
            1 for r in rows if r["with_condense"]["standalone"] not in (None, r["turn2"])),
        "rows": rows,
    }
    (ROOT / "data" / f"eval_{tag}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    md = (
        f"| 配置 | 第二轮正确率 |\n|---|---|\n"
        f"| 不带上下文（单轮） | {out['no_history_acc']}% |\n"
        f"| 带指代消解（本项目） | {out['with_condense_acc']}% |\n\n"
        f"{out['n']} 组两轮对话；其中 {out['changed_by_rewrite']} 组被改写改写过。\n"
    )
    (ROOT / "data" / f"eval_{tag}.md").write_text(md, encoding="utf-8")
    return out


async def amain(args) -> None:
    bad = check_cases()
    if bad:
        for b in bad:
            print("[FAIL]", b)
        sys.exit(1)

    dm = await DocuMind(args.index_tag, "local", use_rerank=False).open()
    t0 = time.perf_counter()
    try:
        out = await run(dm, args.tag)
    finally:
        await dm.close()

    print()
    print(f"不带上下文：{out['no_history_acc']}%    带指代消解：{out['with_condense_acc']}%"
          f"    （{out['n']} 组，{time.perf_counter() - t0:.0f}s）")
    print(f"明细：data/eval_{args.tag}.json")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="multiturn")
    ap.add_argument("--index-tag", default="prod")
    asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    main()
