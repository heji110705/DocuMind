"""验一批评测数据"干不干净"：每题到底用了哪个模型、中途有没有切过。

为什么单独一个脚本：`model_used` 是简历数字的唯一凭证。一批 124 题里只要
有一题落到 qwen，那批数据就不能用来出简历数——不是"剔除这一题"就行，
因为切换往往意味着那一段时间的上游状态不干净，剔除坏题会引入选择偏差。

用法：

    python scripts/check_run_purity.py data/eval_3a.json
    python scripts/check_run_purity.py data/eval_3a.json --log logs/run_3a.txt
    python scripts/check_run_purity.py data/eval_3a.json --expect deepseek-chat

退出码：0 = 可用；1 = 必须重跑；2 = 判定不了（缺字段又没日志）。

两种数据形态都要能读：
- 新格式（2026-11-18 之后）：每行有 `model_used` / `fallbacks`；
- 旧格式：没有这两列，只能靠运行日志里有没有 "[llm] 主模型 ... 切备用" 来判。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 旧版 llm_client 在切换到备用模型时必打这一行；日志里有它就说明发生过切换。
SWITCH_PAT = re.compile(r"\[llm(?:-stream)?\].*切备用|切备用\s+\S+")
# 每组跑完会打 "[full] 可答 … （123s）"
ELAPSED_PAT = re.compile(r"\[(?P<cfg>[^\]]+)\].*?（(?P<sec>\d+(?:\.\d+)?)s）")


def load(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit(f"{path} 不是配置数组（顶层应为 list）")
    return data


def scan_log(path: Path | None) -> tuple[int, list[str]]:
    """返回 (切备用次数, 每组耗时行)。"""
    if path is None or not path.exists():
        return 0, []
    text = path.read_text(encoding="utf-8", errors="replace")
    switches = [ln.strip() for ln in text.splitlines() if SWITCH_PAT.search(ln)]
    elapsed = [f"{m['cfg']}: {m['sec']}s" for m in ELAPSED_PAT.finditer(text)]
    return len(switches), (switches[:5] + elapsed)


def check_group(g: dict, expect: str, log_switches: int, has_log: bool) -> tuple[int, list[str]]:
    """返回 (0 可用 / 1 作废 / 2 判定不了, 报告行)。"""
    cfg = g.get("config", "?")
    rows = g.get("rows") or []
    out: list[str] = []
    out.append(f"\n[{cfg}] {len(rows)} 题 · 总体 {g.get('overall')}% · "
               f"p50 {g.get('p50_ms')}ms · 单题 ¥{g.get('cost_per_q')}")

    if not rows:
        out.append("  ⚠ 这条配置没有逐题明细（rows 为空），只能看汇总")
        return 2, out

    used = [r.get("model_used", "") for r in rows]
    has_field = any(used)

    if has_field:
        dist = Counter(u or "<空>" for u in used)
        out.append("  model_used 分布：" + "，".join(f"{k} × {v}" for k, v in dist.most_common()))
        dirty = [r for r in rows if r.get("model_used") and not r["model_used"].startswith(expect)]
        fb = [r for r in rows if (r.get("fallbacks") or 0) > 0]
        if fb:
            ids = ", ".join(str(r["id"]) for r in fb[:20])
            out.append(f"  fallbacks>0 的题：{len(fb)} 题 → {ids}"
                       + (" …" if len(fb) > 20 else ""))
        else:
            out.append("  fallbacks>0 的题：无")
        if dirty:
            ids = ", ".join(str(r["id"]) for r in dirty[:20])
            out.append(f"  ✗ {len(dirty)} 题不是 {expect}：{ids}")
            return 1, out
        if fb:
            out.append("  ✗ 有题发生过模型切换——即使终值是主模型，这一批也不能当纯数据用")
            return 1, out
        out.append(f"  ✓ 全部 {expect}，且无切换")
        return 0, out

    # 旧格式：没有 model_used 列
    out.append("  ⚠ 这批结果的逐题明细里没有 model_used / fallbacks（旧版 run_eval 不写）")
    if not has_log:
        out.append("  ? 判定不了：补 --log 指向运行日志，或整批重跑（现在的版本会写这两列）")
        return 2, out
    if log_switches:
        out.append(f"  ✗ 日志里有 {log_switches} 次「切备用」，这批数据被污染")
        return 1, out
    out.append("  ✓ 日志里没有任何「切备用」，可视为全程主模型（旧版只在那条路径打这行）")
    return 0, out


def main() -> int:
    ap = argparse.ArgumentParser(description="验评测数据成色：model_used 分布与切换记录")
    ap.add_argument("result", help="data/eval_<tag>.json")
    ap.add_argument("--log", default="", help="同一次运行的 stdout 日志（可选，旧格式必需）")
    ap.add_argument("--expect", default="deepseek-chat", help="期望的模型名（默认 deepseek-chat）")
    ap.add_argument("--config", default="", help="只看某个配置（默认全部）")
    args = ap.parse_args()

    path = Path(args.result)
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        print(f"找不到 {path}")
        return 2

    log_path = Path(args.log) if args.log else None
    if log_path is not None and not log_path.is_absolute():
        log_path = ROOT / log_path
    switches, log_lines = scan_log(log_path)

    groups = load(path)
    if args.config:
        groups = [g for g in groups if g.get("config") == args.config]
        if not groups:
            print(f"没有名为 {args.config} 的配置")
            return 2

    print(f"结果文件：{path}")
    print(f"日志    ：{log_path if log_path and log_path.exists() else '未提供'}")
    if log_lines:
        print("日志摘录：")
        for ln in log_lines:
            print("  " + ln)

    worst = 0
    for g in groups:
        code, lines = check_group(g, args.expect, switches, bool(log_path and log_path.exists()))
        print("\n".join(lines))
        worst = max(worst, code)

    print("\n判定：", {0: "可用", 1: "作废，需整批重跑", 2: "判定不了"}[worst])
    if worst == 0:
        print("提示：peak CPU 读不到——用 /usr/bin/time -v（Linux）或 typeperf（Windows）"
              "在跑批时另开一个窗口采，别用任务管理器的瞬时值。")
    return worst


if __name__ == "__main__":
    sys.exit(main())
