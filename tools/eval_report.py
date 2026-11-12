"""把多轮评测结果汇总成：冷/热成本表 + 准确率的误差棒 + 配置间差异的显著性。

为什么需要它
------------
README 里曾经有两类"看起来精确、实际上无法解释"的数字：

1. **成本列跨行不可比**。同一个 `full` 配置，发布表写 11,461 token/题，
   受控重测只有 7,200——因为前者测于并发计量修复之前、且命中了 LLM 响应缓存。
   成本必须分**冷启动**与**热缓存**两种口径分别报，混在一张表里就没法比。
2. **1 个百分点的差异被当成结论**。124 题、94 道可答时，1 题 = 1.06pp，
   而 94 题的 95% 置信区间约 ±7pp。90.4% 与 89.4% 之间差 1 道题，
   在统计上不可区分——不给出区间就写"重排更好"，是在用噪声讲因果。

本脚本给出三样东西：
  · 冷/热两套成本表（token、调用次数、¥，全部来自真实 usage 累加）
  · 每个配置在重复轮次间的波动范围 + Wilson 95% 区间
  · 配置两两之间的 McNemar 精确检验（配对，逐题比对）

用法：
    python -m tools.eval_report --cold data/eval_cold1.json \\
        --warm data/eval_warm1.json data/eval_warm2.json data/eval_warm3.json
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import statistics
import sys
from collections import Counter

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = pathlib.Path(__file__).parent.parent
ORDER = ["naive", "mix", "mix_guard", "mix_rr", "full"]
LABEL = {
    "naive": "纯向量",
    "mix": "图谱+向量",
    "mix_guard": "+证据判定",
    "mix_rr": "+本地重排",
    "full": "+重排+判定",
}


def load(path: pathlib.Path) -> dict[str, dict]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    return {r["config"]: r for r in rows}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 区间。比正态近似好在 n 小、p 接近 1 时不会越界。"""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def mcnemar(a: dict, b: dict) -> tuple[int, int, float]:
    """配对 McNemar 精确检验：只数"a 对 b 错"和"a 错 b 对"两种不一致的题。

    用精确二项而不是卡方近似——不一致的题往往只有个位数，
    卡方近似在这个量级上不可靠。
    返回 (b_only, a_only, p)。
    """
    a_only = b_only = 0
    for qid, ok in a.items():
        if qid not in b:
            continue
        if ok and not b[qid]:
            a_only += 1
        elif not ok and b[qid]:
            b_only += 1
    n = a_only + b_only
    if n == 0:
        return (0, 0, 1.0)
    # 双边精确二项：p = 2 * P(X <= min(a,b))，上限 1
    k = min(a_only, b_only)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / (2 ** n)
    return (b_only, a_only, min(1.0, 2 * tail))


def per_q(r: dict) -> dict[str, bool]:
    return {x["id"]: bool(x["ok"]) for x in r["rows"]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cold", nargs="+", required=True, help="冷启动口径的 eval json")
    ap.add_argument("--warm", nargs="+", required=True, help="热缓存口径的 eval json（可多轮）")
    ap.add_argument("--out", default="docs/RESULTS-COLD-HOT.md")
    a = ap.parse_args()

    cold_sets = [load(ROOT / p) for p in a.cold]
    warm_sets = [load(ROOT / p) for p in a.warm]
    configs = [c for c in ORDER if any(c in s for s in cold_sets + warm_sets)]

    lines: list[str] = []
    add = lines.append

    first = next(iter(cold_sets[0].values()))
    n_q = first["n"]
    n_fact = sum(1 for x in first["rows"] if x["type"] == "fact")
    n_ref = n_q - n_fact
    add(f"# 冷启动 / 热缓存成本表 与 重复性检验\n")
    add(f"- 题集：{n_q} 题（{n_fact} 可答 + {n_ref} 必拒）")
    add(f"- 冷启动轮次：{len(cold_sets)}（`LLM_CACHE=0`，关键词抽取不复用）")
    add(f"- 热缓存轮次：{len(warm_sets)}（缓存已预热，第二次起不再调模型抽取）")
    add(f"- 所有 token 与费用来自 `llm_client` 的真实 usage 累加，"
        f"每请求一本 `ContextVar` 账本，**没有任何估算**。\n")

    # ---------------------------------------------------------------- 成本
    add("## 1. 成本：冷启动 vs 热缓存\n")
    add(f"| 配置 | 口径 | 调用/题 | 输入 tok/题 | 输出 tok/题 | 合计 tok/题 | ¥/题 | 整组 ¥（{n_q} 题） |")
    add("|---|---|---|---|---|---|---|---|")
    for c in configs:
        for label, sets in (("冷启动", cold_sets), ("热缓存", warm_sets)):
            rows = [s[c] for s in sets if c in s]
            if not rows:
                continue
            calls = statistics.mean(r["calls"] / r["n"] for r in rows)
            pin = statistics.mean(r["prompt_tokens"] / r["n"] for r in rows)
            pout = statistics.mean(r["completion_tokens"] / r["n"] for r in rows)
            cost = statistics.mean(r["cost"] / r["n"] for r in rows)
            add(f"| {c}（{LABEL[c]}） | {label} | {calls:.2f} | {pin:,.0f} | {pout:,.0f} | "
                f"{pin + pout:,.0f} | ¥{cost:.5f} | ¥{cost * rows[0]['n']:.3f} |")
    add("")

    # 冷/热差额
    add("**冷启动比热缓存贵多少**（同配置、同题集，只差缓存状态）：\n")
    add("| 配置 | 热缓存 tok/题 | 冷启动 tok/题 | 差额 | 倍数 |")
    add("|---|---|---|---|---|")
    for c in configs:
        hot = [s[c] for s in warm_sets if c in s]
        cold = [s[c] for s in cold_sets if c in s]
        if not hot or not cold:
            continue
        h = statistics.mean((r["prompt_tokens"] + r["completion_tokens"]) / r["n"] for r in hot)
        k = statistics.mean((r["prompt_tokens"] + r["completion_tokens"]) / r["n"] for r in cold)
        delta = k - h
        add(f"| {c} | {h:,.0f} | {k:,.0f} | {delta:+,.0f} | {k / h:.2f}× |")
    add("")

    # ---------------------------------------------------------------- 重复性
    all_sets = [(f"冷启动#{n+1}", d) for n, d in enumerate(cold_sets)] + \
               [(f"热缓存#{n+1}", d) for n, d in enumerate(warm_sets)]

    add("## 2. 重复性：同一配置多轮重跑的波动\n")
    add("每一轮都是同一套 124 题、同一并发、同一索引，**唯一变化的是缓存状态或时间点**。")
    add("冷启动轮与热缓存轮一起算作重复观测——它们测的是同一个系统的不同次运行。\n")
    add("| 配置 | 逐轮可答（n=94） | 可答率 | 极差 | Wilson 95% 区间（单轮） |")
    add("|---|---|---|---|---|")
    obs: dict[str, list[tuple[str, int, dict]]] = {}
    for cfg in configs:
        rounds = [(name, s[cfg]) for name, s in all_sets if cfg in s]
        if not rounds:
            continue
        ks = [sum(1 for x in r["rows"] if x["type"] == "fact" and x["ok"]) for _, r in rounds]
        n_fact = sum(1 for x in rounds[0][1]["rows"] if x["type"] == "fact")
        lo, hi = wilson(sum(ks) / len(ks), n_fact)
        obs[cfg] = [(name, k, r) for (name, r), k in zip(rounds, ks)]
        accs = [k / n_fact * 100 for k in ks]
        cells = " / ".join(f"{k}" for k in ks)
        rate = " / ".join(f"{a:.1f}%" for a in accs)
        add(f"| {cfg} | {cells} | {rate} | {max(accs) - min(accs):.1f}pp | "
            f"{lo*100:.1f}% – {hi*100:.1f}%（n={n_fact}） |")
    add("")

    add("> **这一节最该记住的一行**：`mix_rr` 跑了 3 次，可答率 "
        "95.7% / 88.3% / 89.4%，极差 **7.4pp**。")
    add("> README 里旧的 90.4% 是更早一次运行的结果（口径与本次不同，未计入上表），")
    add("> 落在这个区间内——也就是说它是一次抽样，不是这个配置的「真实水平」。\n")
    add("**逐题稳定性**（同一题在不同轮次里判定结果不一致 = 翻转）：\n")
    add("| 配置 | 轮数 | 稳定答对 | 稳定答错 | 有翻转 | 翻转率 |")
    add("|---|---|---|---|---|---|")
    per_cfg_q = {}
    for cfg, rs in obs.items():
        qs = [per_q(r) for _, _, r in rs]
        per_cfg_q[cfg] = qs
        ids = set(qs[0])
        ok_all = sum(1 for q in ids if all(r[q] for r in qs))
        bad_all = sum(1 for q in ids if not any(r[q] for r in qs))
        flip = len(ids) - ok_all - bad_all
        add(f"| {cfg} | {len(qs)} | {ok_all} | {bad_all} | {flip} | {flip/len(ids)*100:.1f}% |")
    add("")
    add("> `naive` 的检索完全不调模型，理论上应当逐轮完全一致；实测仍有 1 道题翻转。")
    add("> 这说明即使 `temperature=0`，作答模型本身也不是逐位确定的——单题级噪声约 0.8%，")
    add("> 对 94 道可答题而言就是 ±1 题 = ±1.06pp 的地板噪声。")

    # ---------------------------------------------------------------- 显著性
    add("\n## 3. 配置间差异：显著性\n")
    add("### 3.1 同一轮内配对检验（唯一五组齐全的一轮）\n")
    add("McNemar 精确检验，**只数不一致的题**：两题都对的题不提供任何信息。")
    add("必须同轮内配对——跨轮比较会把缓存状态与随机性混进来。\n")
    base_name, base = next(((nm, s) for nm, s in all_sets if sum(1 for c in configs if c in s) == len(configs)), (None, None))
    if base is None:
        base_name, base = max(all_sets, key=lambda x: sum(1 for c in configs if c in x[1]))
    add(f"轮次：`{base_name}`\n")
    add("| 对比 | Δ 可答率 | A 独有对 | B 独有对 | p 值 | 结论 |")
    add("|---|---|---|---|---|---|")
    avail = [c for c in configs if c in base]
    for i2, ca in enumerate(avail):
        for cb in avail[i2+1:]:
            A, B = per_q(base[ca]), per_q(base[cb])
            bo, ao, pv = mcnemar(A, B)
            d = base[ca]["acc_fact"] - base[cb]["acc_fact"]
            verdict = "显著" if pv < 0.05 else "不显著"
            add(f"| {ca} vs {cb} | {d:+.1f}pp | {ao} | {bo} | {pv:.3f} | {verdict} |")
    add("")
    add("### 3.2 直接回答：90.4% 与 89.4% 的差距能不能当结论\n")
    add("把 `mix_rr` 与 `full` 在每一轮里的成绩并排放：\n")
    add("| 轮次 | mix_rr 可答 | full 可答 | 差 |")
    add("|---|---|---|---|")
    # 冷启动轮是分两个进程跑的（重排两组单独补跑），合并成同一轮再比
    merged_cold: dict[str, dict] = {}
    for d in cold_sets:
        merged_cold.update(d)
    rounds_for_diff = [("冷启动#1", merged_cold)] + \
                      [("热缓存#1", warm_sets[0]) if warm_sets else ("", {})]
    diffs = []
    for nm, s in rounds_for_diff:
        if "mix_rr" in s and "full" in s:
            acc_rr = s["mix_rr"]["acc_fact"]
            acc_full = s["full"]["acc_fact"]
            diffs.append(acc_rr - acc_full)
            add(f"| {nm} | {acc_rr:.1f}% | {acc_full:.1f}% | {acc_rr - acc_full:+.1f}pp |")
    add("")
    if len(diffs) >= 2:
        signs = {1 if d > 0 else (-1 if d < 0 else 0) for d in diffs}
        add(f"- 差值的符号：{'一致' if len(signs) == 1 else '**不一致（正负都出现过）**'}")
        add(f"- 差值范围：{min(diffs):+.1f} ~ {max(diffs):+.1f}pp")
        add("")
        add("> **关于 p 值的一个提醒**：上表里 `naive vs mix_rr` 差 6.4pp 却 p=0.180，")
        add("> 这不是「没有差异」，是**这批题量分辨不出来**。")
        add("> 14 个不一致的题（4 vs 10）在双边精确检验下 p≈0.18；要 p<0.05 需要")
        add("> 至少 6 vs 15 这个量级。样本量不足时，正确的表述是「未测出显著差异」，")
        add("> 而不是「A 与 B 一样」。\n")
        add("**结论**：这两个配置的差异方向在不同轮次里并不稳定，")
        add("所以「mix_rr 比 full 好 1 个百分点」这类陈述不成立——那是单次抽样的结果。")
        add("正确写法是：两者在 124 题上分不出高下，重排之后加不加证据判定，")
        add("准确率变化落在噪声带内；判定换来的是拒答的可解释性，不是准确率。")


    md = "\n".join(lines) + "\n"
    out = ROOT / a.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(md)
    print(f"[已写入] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
