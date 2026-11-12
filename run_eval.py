"""124 题 × 5 组配置消融评测。

5 组配置是一个 2×2 因子设计加一个基线，回答"每个开关各自值多少"：
    naive      只做向量召回（基线）
    mix        图谱 + 向量混合召回，不重排、不判证据
    mix_guard  mix + 证据判定（= 界面上"快速"模式跑的东西）
    mix_rr     mix + 本地 cross-encoder 重排
    full       mix + 重排 + 证据判定（= 界面上"高精度"模式）

为什么要单独跑 mix_guard：界面默认走的是"混合召回 + 判定"，既不是 mix 也不是 full，
而之前的表里没有这一行，导致界面上标注的准确率只能借用相邻配置的数字。
把界面真实在跑的配置测出来，标注才不是猜的。

判定：
    可答题 = must_include 里每一组至少命中一个字符串（纯字符串匹配，不用 LLM 当裁判）
    不可答题 = 必须拒答

用法：
    python run_eval.py --tag prod
    python run_eval.py --tag prod --limit 5      # 冒烟
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).parent

from evalutil import cost_of, hit  # noqa: E402
from llm_client import llm_func  # noqa: E402
from query import DocuMind, is_refusal  # noqa: E402

CONFIGS = [
    ("naive", dict(mode="naive", rerank=False, guard=False)),
    ("mix", dict(mode="mix", rerank=False, guard=False)),
    ("mix_guard", dict(mode="mix", rerank=False, guard=True)),
    ("mix_rr", dict(mode="mix", rerank=True, guard=False)),
    ("full", dict(mode="mix", rerank=True, guard=True)),
]


async def run_config(dm: DocuMind, items: list[dict], name: str, kw: dict, conc: int) -> dict:
    sem = asyncio.Semaphore(conc)
    rows: list[dict] = []

    async def one(it: dict) -> None:
        async with sem:
            t0 = time.perf_counter()
            try:
                # 单题硬超时：评测里最怕的不是答错，是某一题卡住把后面全拖死。
                # 卡住通常是上游 hang 或 rerank 死锁，等下去没有任何收益。
                a = await asyncio.wait_for(dm.ask(it["q"], **kw), timeout=TIMEOUT_S)
            except asyncio.TimeoutError:
                rows.append({
                    "id": it["id"], "type": it["type"], "q": it["q"],
                    "answer": f"[TIMEOUT >{TIMEOUT_S}s]", "refused": True,
                    "citations": [], "ok": False,
                    "ms": TIMEOUT_S * 1000, "calls": 0,
                    "prompt_tokens": 0, "completion_tokens": 0, "n_chunks": 0,
                })
                print(f"  [timeout] {it['id']} {it['q'][:30]}", flush=True)
                return
            except Exception as e:  # noqa: BLE001
                # 单题异常必须就地吞掉，否则一道题就把整组 124 题废掉。
                #
                # 这是实测踩出来的：`full` 组在冷启动下两次跑到 63/79 题时静默挂死——
                # 进程 0% CPU（不是空转）、日志里没有 traceback、只留一句
                # "Successfully finalized 12 storages"。原因是某一题抛了异常
                # （上游 400 或 rerank 内部错误），asyncio.gather 直接把它抛出去，
                # amain 的 finally 关掉索引，然后 asyncio.run 在退出时等一个
                # 不可取消的任务，永久卡住。
                # 结果就是：前 60 多题白跑，且看不出是哪一题出的问题。
                # 现在改成记一行 [ERROR] 继续跑，异常本身留在 answer 里可追。
                rows.append({
                    "id": it["id"], "type": it["type"], "q": it["q"],
                    "answer": f"[ERROR] {type(e).__name__}: {e}"[:400], "refused": True,
                    "citations": [], "ok": False,
                    "ms": int((time.perf_counter() - t0) * 1000), "calls": 0,
                    "prompt_tokens": 0, "completion_tokens": 0, "n_chunks": 0,
                })
                print(f"  [error] {it['id']} {type(e).__name__}: {e}"[:160], flush=True)
                return
            dt = int((time.perf_counter() - t0) * 1000)
            if it["type"] == "fact":
                ok = (not a.refused) and hit(a.answer, it["must_include"])
            else:
                ok = a.refused
            rows.append(
                {
                    "id": it["id"],
                    "type": it["type"],
                    "q": it["q"],
                    "answer": a.answer,
                    "refused": a.refused,
                    "citations": a.citations,
                    "ok": ok,
                    "ms": dt,
                    "calls": a.calls,
                    "prompt_tokens": a.extra.get("prompt_tokens", 0),
                    "completion_tokens": a.extra.get("completion_tokens", 0),
                    "n_chunks": a.extra.get("n_chunks", 0),
                }
            )

    await asyncio.gather(*(one(it) for it in items))
    rows.sort(key=lambda r: r["id"])

    facts = [r for r in rows if r["type"] == "fact"]
    unans = [r for r in rows if r["type"] in ("unanswerable", "refuse")]
    answered = [r for r in rows if not r["refused"]]
    total_cost = sum(cost_of(r["prompt_tokens"], r["completion_tokens"]) for r in rows)

    return {
        "config": name,
        "n": len(rows),
        "acc_fact": round(sum(r["ok"] for r in facts) / len(facts) * 100, 1) if facts else 0.0,
        "acc_fact_raw": f"{sum(r['ok'] for r in facts)}/{len(facts)}",
        "refusal_acc": round(sum(r["ok"] for r in unans) / len(unans) * 100, 1) if unans else 0.0,
        "refusal_raw": f"{sum(r['ok'] for r in unans)}/{len(unans)}",
        "overall": round(sum(r["ok"] for r in rows) / len(rows) * 100, 1),
        "overall_raw": f"{sum(r['ok'] for r in rows)}/{len(rows)}",
        "citation_rate": round(
            sum(1 for r in answered if r["citations"]) / len(answered) * 100, 1
        ) if answered else 0.0,
        "p50_ms": int(statistics.median(r["ms"] for r in rows)),
        "mean_chunks": round(statistics.mean(r["n_chunks"] for r in rows), 1),
        "calls": sum(r["calls"] for r in rows),
        "prompt_tokens": sum(r["prompt_tokens"] for r in rows),
        "completion_tokens": sum(r["completion_tokens"] for r in rows),
        "cost": round(total_cost, 4),
        "cost_per_q": round(total_cost / len(rows), 5),
        "rows": rows,
    }


def to_markdown(results: list[dict], tag: str) -> str:
    head = "| 配置 | 可答准确率 | 拒答正确率 | 总体 | 引用率 | p50 | 平均召回块 | 调用次数 | 单题成本 |\n"
    head += "|---|---|---|---|---|---|---|---|---|\n"
    body = ""
    for r in results:
        body += (
            f"| {r['config']} | {r['acc_fact']}%（{r['acc_fact_raw']}） | "
            f"{r['refusal_acc']}%（{r['refusal_raw']}） | {r['overall']}%（{r['overall_raw']}） | "
            f"{r['citation_rate']}% | {r['p50_ms']} ms | {r['mean_chunks']} | "
            f"{r['calls']} | ¥{r['cost_per_q']} |\n"
        )
    total = sum(r["cost"] for r in results)
    return f"索引：`{tag}`\n\n" + head + body + f"\n四组配置合计花费 ¥{total:.4f}。\n"


TIMEOUT_S = 300


def _save(results: list[dict], tag: str, path):
    order = [c[0] for c in CONFIGS]
    merged = {r["config"]: r for r in results}
    out = sorted(merged.values(), key=lambda r: order.index(r["config"]) if r["config"] in order else 99)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


async def amain(args) -> None:
    spec = json.loads((ROOT / args.set).read_text(encoding="utf-8"))
    items = spec["items"]
    if args.limit:
        items = items[: args.limit]

    # 开跑前先探一次上游。理由很实际：一轮 124 题的重排评测要 25 分钟，
    # 而"根本没网"和"网慢"在日志里长得几乎一样——实测踩过代理端口切换，
    # 整轮 124 题全部 APIConnectionError，白跑 7 分钟才发现。
    # 花 1 秒换掉 25 分钟的废跑，这笔账很划算。
    try:
        await asyncio.wait_for(llm_func("ping", max_tokens=1), timeout=30)
        print("[preflight] 上游 LLM 可达", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] 上游 LLM 不可达，先修网络再跑：{type(e).__name__}: {e}", flush=True)
        return

    dm = await DocuMind(args.index_tag or args.tag, args.embed, use_rerank=True).open()
    results = []
    try:
        for name, kw in CONFIGS:
            if args.configs and name not in args.configs:
                continue
            print(f"\n[{name}] 开始 {len(items)} 题 …", flush=True)
            t0 = time.perf_counter()
            if args.chunk_top_k:
                kw = {**kw, "chunk_top_k": args.chunk_top_k}
            r = await run_config(dm, items, name, kw, args.concurrency)
            print(
                f"[{name}] 可答 {r['acc_fact']}%  拒答 {r['refusal_acc']}%  "
                f"总体 {r['overall']}%  p50 {r['p50_ms']}ms  花费 ¥{r['cost']:.4f}  "
                f"（{time.perf_counter() - t0:.0f}s）",
                flush=True,
            )
            results.append(r)
            # 关键：每跑完一组就写一次。之前是四组跑完才写，
            # 结果第四组卡死、前三组数据全丢，白烧了一次钱。
            prev = []
            fp = ROOT / "data" / f"eval_{args.tag}.json"
            if fp.exists():
                try:
                    prev = json.loads(fp.read_text(encoding="utf-8"))
                except Exception:
                    prev = []
            _save([*prev, *results], args.tag, fp)
    finally:
        await dm.close()

    out = ROOT / "data" / f"eval_{args.tag}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    # 按配置名合并：分组重跑不会覆盖已跑完的其他组
    if out.exists():
        try:
            old = json.loads(out.read_text(encoding="utf-8"))
        except Exception:
            old = []
        merged = {r["config"]: r for r in old}
        for r in results:
            merged[r["config"]] = r
        order = [c[0] for c in CONFIGS]
        results = sorted(merged.values(), key=lambda r: order.index(r["config"]) if r["config"] in order else 99)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    md = to_markdown(results, args.tag)
    (ROOT / "data" / f"eval_{args.tag}.md").write_text(md, encoding="utf-8")
    print("\n" + md)
    print(f"明细已写入 {out}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--tag", default="prod", help="输出文件名用（data/eval_<tag>.json）")
    p.add_argument("--index-tag", default="", help="用哪个索引目录，默认同 --tag")
    p.add_argument("--set", default="eval_set_v2.json", help="评测集（默认 124 题 v2）")
    p.add_argument("--embed", default="local", choices=["bailian", "local"])
    p.add_argument("--limit", type=int, default=0, help="只跑前 N 题（冒烟用）")
    p.add_argument("--concurrency", type=int, default=3)
    p.add_argument("--configs", nargs="*", help="只跑指定配置")
    p.add_argument("--timeout", type=int, default=300, help="单题超时秒数（默认 300）")
    p.add_argument("--chunk-top-k", type=int, default=0,
                   help="覆盖 chunk_top_k（0=用 query.py 默认值），用于控制检索预算做对照")
    args = p.parse_args()
    global TIMEOUT_S
    TIMEOUT_S = args.timeout
    asyncio.run(amain(args))


if __name__ == "__main__":
    main()
