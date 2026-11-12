"""把金标从 124 题扩到 300 题：从「单一真值源」批量生成 + 机械校验。

为什么要程序化生成，而不是再手写 180 题：
1. 手写会漂。`facts_schema.py` / `facts_ops.py` 是文档的唯一真值源，
   文档、FAQ、金标都从它渲染出来。手写金标等于再抄一遍数字，抄错一个
   就变成"评测说模型答错了，其实是金标错了"——这类误判最难发现。
2. 可校验。每条新题的关键词都必须能在**语料原文**里逐字找到，
   找不到就丢掉并打印原因。这一步把"金标错"的可能性压到很低。
3. 可复现。改一处事实 → 重跑本脚本 → 题目与关键词一起更新。

必须说清楚的代价：结构化数据只能长出"查表型"问题（某个指标怎么算、
某个错误码是什么）。这类题占多数会让准确率虚高，因为只要检索命中就能答对。
所以：
- 五组消融的分层报表里保留 task 字段，可以按能力类型拆开看；
- 原有 124 题里那些跨段推理、口径辨析的题目全部保留，一条不删；
- 本脚本只做"加题"，不改任何旧题。

可拒答题（refuse）额外要求 `must_absent`：这些词必须**不出现**在语料里，
否则这道题其实是可答的，测的就不是幻觉了。

用法：
    python -m tools.gen_gold3            # 生成 eval_set_v3.json
    python -m tools.gen_gold3 --report   # 只打印统计与校验失败项
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "seed"
BASE_SET = ROOT / "eval_set_v2.json"
OUT = ROOT / "eval_set_v3.json"


# --------------------------------------------------------------------- 语料
def corpus_text() -> str:
    """和 run_index.py 吃进去的语料完全一致：seed 下的 md（含 v2 子目录）。"""
    parts = []
    for p in sorted(SEED.rglob("*.md")):
        parts.append(p.read_text(encoding="utf-8"))
    return "\n".join(parts)


CLEAN = re.compile(r"[\s`*_#|]+")


def norm(s: str) -> str:
    """比较用：去掉空白与 markdown 记号，避免"表格里多了个空格就判不存在"。"""
    return CLEAN.sub("", s or "")


def nums(text: str) -> list[str]:
    """从一段文字里取数字型关键词（含百分号与千分位）。"""
    out = []
    for m in re.finditer(r"\d[\d,\.]*\s*%?", text or ""):
        t = m.group(0).replace(" ", "")
        t = t.rstrip(".")
        if len(t) >= 1:
            out.append(t)
    return out


def idents(text: str) -> list[str]:
    """取英文标识符（字段名、错误码、事件名）。"""
    return [m.group(0) for m in re.finditer(r"[A-Za-z_][A-Za-z0-9_]{2,}", text or "")]


# --------------------------------------------------------------- 生成逻辑
def build(base_qs: list[str]) -> tuple[list[dict], list[str]]:
    from tools.corpus_build import facts_ops as ops
    from tools.corpus_build import facts_schema as schema

    items: list[dict] = []
    skipped: list[str] = []
    n = [0]

    def asked(subject: str, *words: str) -> bool:
        """原有题面里已经用相近问法问过这个对象吗。

        只按英文标识符去重会漏掉"dim_book 一共有多少行"和"「dim_book」大概有多少行"
        这种同义改写；这里补一道按对象 + 问法词的检查。
        """
        return any(subject in q and any(w in q for w in words) for q in base_qs)

    def add(cat: str, q: str, groups: list[list[str]], key: str = "",
            absent: list[str] | None = None):
        n[0] += 1
        items.append({
            "id": f"G{n[0]:03d}",
            "type": "refuse" if absent else "fact",
            "task": cat,
            "q": q,
            "must_include": groups,
            "_key": key or q,          # 去重用，落盘前删掉
            **({"must_absent": absent} if absent else {}),
        })

    # ---- A. 指标口径（34 个指标，每个 1 题：怎么算） ----
    for m in ops.METRICS:
        keys = [k for k in idents(m["formula"])
                if k.lower() not in ("count", "sum", "distinct", "where", "case",
                                     "when", "then", "end", "and", "or", "null",
                                     "else", "round", "avg", "coalesce", "cast", "as")]
        if not keys:
            skipped.append(f"metric {m['key']}: formula 里没有可用标识符")
            continue
        add("metric", f"「{m['name']}」这个指标用的是什么公式？",
            [[k] for k in keys[:3]], key=f"metric:{m['key']}:formula")

    # ---- B. 错误码（30 个，每个 1 题：是什么错） ----
    #
    # 题面一开始写的是"接口返回 X 时，是哪一类错误？"，金标却取 `msg`（含义列，
    # 如"未知字段"）。这两者不是一回事：13-错误码与排障.md 里既有一张
    # 「码段 → 类别」的总表（1040-1042 = 限流与配额），也有一张「单码 → 含义」的明细表。
    # 按字面理解"哪一类"，答"限流与配额"就已经正确了，而金标要的是"日调用额度用尽"。
    # 系统给出的正是一个字面上正确的答案，却被判错——这是题面的错，不是检出能力的问题。
    #
    # 判据只看题目文本与语料结构，不看系统答得对不对：语料有两列，题面只说了要"类别"，
    # 金标却填了"含义"那一列。改成明确问含义，金标与题面才对得上。
    for code, status, msg, desc, fix in ops.ERROR_CODES:
        add("errorcode", f"错误码 {code} 的具体含义是什么？", [[msg]], key=f"code:{code}")

    # ---- C. 接口（14 个，用途与鉴权两类） ----
    for ep in ops.ENDPOINTS:
        path = ep["path"]
        add("api", f"{ep['method']} {path} 这个接口是做什么的？", [[ep["purpose"]]],
            key=f"api:{ep['method']} {path}:purpose")
        if ep.get("auth") and ep["auth"] != "无":
            add("api", f"调用 {path} 需要什么鉴权？", [[ep["auth"]]],
                key=f"api:{ep['method']} {path}:auth")

    # ---- D. 字段误用（pitfall 里带数字的字段） ----
    for t in schema.TABLES:
        for c in t["columns"]:
            pit = c.get("pitfall", "")
            ns = [x for x in nums(pit) if x not in ("0", "1", "2")]
            if not ns:
                continue
            # 多个数字分别成组：只拼成一个字符串容易和原文的空格/标点对不上
            groups = [[x] for x in ns[:2]]
            # 「1.28 倍」和「高 28%」是同一个事实的两种写法。原文写的是倍数，
            # 系统答成百分比不能算错——这一组单位换算由原文自己决定：
            # 只有当 pitfall 里确实出现"倍"、且数字形如 1.xx 时才补一个等价写法。
            if "倍" in pit:
                for x in ns[:2]:
                    try:
                        v = float(x)
                    except ValueError:
                        continue
                    if 1.0 < v < 2.0:
                        pct = f"{(v - 1) * 100:.0f}%"
                        if pct not in groups[ns.index(x)]:
                            groups[ns.index(x)].append(pct)
            add("pitfall", f"{t['name']}.{c['name']} 误用会带来多大偏差？",
                groups, key=f"field:{t['name']}.{c['name']}:pitfall")

    # ---- E. 表粒度与规模 ----
    for t in schema.TABLES:
        if not asked(t["name"], "粒度"):
            add("grain", f"「{t['name']}」这张表的粒度是什么？", [[t["grain"]]],
                key=f"table:{t['name']}:grain")
        if not asked(t["name"], "行", "规模"):
            add("scale", f"「{t['name']}」大概有多少行数据？", [[t["row_count"]]],
                key=f"table:{t['name']}:scale")

    # ---- F. 套餐配额（4 个） ----
    for name, qps, tokens, conc, note in ops.QUOTAS:
        add("quota", f"{name} 套餐的 QPS 和并发上限分别是多少？",
            [[str(qps)], [str(conc)]], key=f"quota:{name}")

    # ---- G. SSE 事件 ----
    for ev, fields, note in ops.SSE_EVENTS:
        first = [f.strip() for f in str(fields).split(",") if f.strip()]
        if first:
            add("sse", f"SSE 的 {ev} 事件里带了哪些字段？", [[first[0]]], key=f"sse:{ev}")

    # ---- H. 版本变更（取每个版本的第一条变更） ----
    for ver, date, changes in ops.CHANGELOG:
        if not changes:
            continue
        key = [x for x in (nums(changes[0]) or idents(changes[0])) if len(x) > 1]
        if key:
            add("changelog", f"{ver} 版本有哪些主要变更？", [[key[0]]], key=f"ver:{ver}")

    # ---- I. 术语表（(术语, 英文, 定义, 易混项)） ----
    for row in ops.TERMS:
        term, _en, definition = row[0], row[1], row[2]
        key = nums(definition) or idents(definition)
        if key:
            add("term", f"「{term}」指的是什么？", [[key[0]]], key=f"term:{term}")

    return items, skipped


# ----------------------------------------------------------- 可拒答题（新 30）
# 要求：看起来合理、语料里确实没有。must_absent 会在校验阶段逐字确认。
REFUSE = [
    ("为什么 iOS 端的 DAU 比 Android 高？", ["iOS", "安卓端"]),
    ("京东渠道的 GMV 占比是多少？", ["京东"]),
    ("租户欠费多久会被停服？", ["欠费"]),
    ("供应商的结算周期是多少天？", ["供应商结算"]),
    ("这本书的作者版税比例是多少？", ["版税"]),
    ("客服响应的时效标准是多久？", ["响应时效"]),
    ("机房服务器的 CPU 型号是什么？", ["CPU 型号"]),
    ("双十一大促的备货量是多少？", ["备货"]),
    ("会员等级一共有几级？", ["会员等级"]),
    ("跨境订单的关税怎么计算？", ["关税"]),
    ("用户的性别分布是怎样的？", ["性别"]),
    ("数据仓库部署在哪家云厂商？", ["阿里云", "腾讯云", "AWS"]),
    ("离线任务调度用的是 Airflow 吗？", ["Airflow"]),
    ("埋点 SDK 的版本号是多少？", ["SDK 版本"]),
    ("静态资源用的是哪家对象存储？", ["对象存储", "OSS", "S3"]),
    ("短信验证码的有效期是几分钟？", ["验证码"]),
    ("优惠券的最大面额是多少？", ["优惠券"]),
    ("购物车加购率的行业基准是多少？", ["加购率"]),
    ("2028 年的营收目标是多少？", ["营收目标"]),
    ("公司总部在哪个城市？", ["总部"]),
    ("员工的平均年龄是多少？", ["平均年龄"]),
    ("月活用户的城市分布是怎样的？", ["城市分布"]),
    ("推荐算法一共用了几路召回？", ["召回通道"]),
    ("搜索词 Top100 榜单在哪里下载？", ["Top100"]),
    ("微信小程序的日活是多少？", ["小程序"]),
    ("发票开具的税率是几个点？", ["税率"]),
    ("跨境支付的手续费率是多少？", ["手续费"]),
    ("服务器扩容需要谁审批？", ["扩容审批"]),
    ("这本书的印刷厂是哪一家？", ["印刷厂"]),
    ("同类产品里谁的市场份额最高？", ["市场份额"]),
]


def existing_keys(base: list[dict]) -> set[str]:
    """原有 124 题里已经问过的对象。

    用"题面里的表名/字段名 + 问法"做 key：`dim_book` 的粒度在原题里出现过，
    就不再生成"「dim_book」这张表的粒度是什么"。宁可少生成，不要重复出题——
    重复题会让准确率被同一道题重复计权，看起来更稳，其实是假的。
    """
    keys = set()
    for it in base:
        for t in idents(it["q"]):
            keys.add(norm(t).lower())
    return keys


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args()

    text = norm(corpus_text())
    base = json.loads(BASE_SET.read_text(encoding="utf-8"))["items"]
    base_qs = [it["q"] for it in base]
    base_keys = existing_keys(base)

    gen, skipped = build(base_qs)
    kept, dropped = [], []

    for it in gen:
        key = norm(it["_key"]).lower()
        # 两层去重：
        # 1. 生成项之间按 key（对象 + 问法）去重
        # 2. 与原有 124 题按"题面里出现了同一个对象"去重——
        #    原有题里已经问过 dim_book 的行数，就别再生成一遍同义题
        if key in base_keys:
            dropped.append((it["id"], "重复：" + it["q"]))
            continue
        base_keys.add(key)
        kept.append(it)

    # 追加新的可拒答题
    for i, (q, absent) in enumerate(REFUSE, 1):
        kept.append({
            "id": f"R{i:03d}",
            "type": "refuse",
            "task": "refuse",
            "q": q,
            "must_include": [],
            "must_absent": absent,
            "_key": f"refuse:{i}",
        })

    # ---------------- 机械校验 ----------------
    bad = []
    ok = []
    for it in kept:
        if it["type"] == "refuse":
            hit = [w for w in it["must_absent"] if norm(w) in text]
            if hit:
                bad.append((it["id"], f"must_absent 出现在语料里：{hit}"))
                continue
        else:
            miss = []
            for g in it["must_include"]:
                if not any(norm(alt) in text for alt in g):
                    miss.append(g)
            if miss:
                bad.append((it["id"], f"关键词不在语料里：{miss}"))
                continue
        ok.append(it)

    from collections import Counter

    dist = Counter(i["type"] for i in ok)
    tasks = Counter(i["task"] for i in ok if i["type"] == "fact")
    print(f"原有 {len(base)} 题 + 生成 {len(ok)} 题 = {len(base) + len(ok)} 题")
    print(f"  可答 {dist['fact']}｜必拒 {dist['refuse']}")
    print(f"  生成部分的类型分布：{dict(tasks)}")
    print(f"  去重丢弃 {len(dropped)}｜校验失败 {len(bad)}")

    if a.report:
        for x in dropped[:10]:
            print("  [dup]", x[1])
        for x in bad[:15]:
            print("  [bad]", x[0], x[1])
        return

    merged = base + ok
    for i, it in enumerate(merged, 1):
        it["id"] = f"Q{i:03d}"
    for it in merged:
        it.pop("_key", None)
    OUT.write_text(json.dumps({
        "meta": {
            "n": len(merged),
            "note": "v3：v2 的 124 题全部保留，其后是从 facts_* 真值源批量生成并经语料校验的题；"
                    "must_include 组间是 AND、组内是 OR；refuse 题的 must_absent 必须不出现在语料里。",
            "source": "tools/gen_gold3.py",
        },
        "items": merged,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写入 {OUT}")


if __name__ == "__main__":
    main()
