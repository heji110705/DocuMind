"""失分归因：把"答错"分成四类，判断该怪系统还是该怪题。

为什么需要这个工具
------------------
把金标从 124 题扩到 316 题之后，可答率从 81.9% 掉到 57.4%。
这个数字有两种完全不同的解释，而它们对应完全相反的动作：

    A. 金标有问题——`must_include` 里的字符串在语料里根本不存在，或者表述不一致。
       这是评测的错，必须修题。
    B. 题变难了——答案字符串确实在语料里，但只出现在某一两个块里，
       而这 122 块里 `chunk_top_k=6` 只有约 5% 的召回预算。这是检索预算的错，
       不能靠改金标来"修"，只能如实报告或加大预算。

光看聚合准确率区分不了 A 和 B，因为两者的表现都是"答错"。所以这个脚本对
每一道答错的题，去索引里查三个纯词法事实（不调 LLM、不花钱）：

    1. 这道题的所有 must_include 分组，是否都能在**某个块**里找到？
       - 全都能在某一块里找到 → 题目本身是可答的，答错是检索/生成的问题
       - 有一组在任何块里都找不到 → 金标与语料不一致，是题的错
    2. 命中的那个块，在整个语料里是"唯一"还是"常见"？
       一个只有 1 块含有的字符串，随机命中率是 1/122，靠 top-6 召回本就吃力。
    3. 命中的块有多少字、是否包含题干里的关键词？
       题干关键词与答案块不共现，说明这题需要跨块推理。

用法：
    python tools/diag_misses.py --result data/eval_prod3.json --config naive
    python tools/diag_misses.py --result data/eval_prod3.json --config naive --only new
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).parent.parent


def load_chunks(index_tag: str) -> list[dict]:
    """块原文 + 所属文件。直接读 json，不起 LightRAG（那要 30 秒和一堆显存）。"""
    p = ROOT / "rag_storage" / index_tag / "kv_store_text_chunks.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    out = []
    for cid, v in raw.items():
        out.append(
            {
                "id": cid,
                "text": v.get("content", ""),
                "file": v.get("file_path", ""),
                "tokens": v.get("tokens", 0),
            }
        )
    return out


def norm(s: str) -> str:
    """归一化：全角转半角、去掉空白和常见标点差异。

    语料是生成的，同一句话在不同文档里可能用全角括号或半角括号，
    直接 `in` 会漏。这一步只抹平排版差异，不做同义改写——
    否则就变成"用宽松匹配把错答案洗成对的"，那是把评测弄坏而不是修好。
    """
    s = s.replace("（", "(").replace("）", ")").replace("，", ",").replace("：", ":")
    s = s.replace(" ", "").replace("\u3000", "").replace("\n", "")
    s = re.sub(r"[*`|]", "", s)
    return s


def classify(item: dict, chunks: list[dict], refused: bool) -> dict:
    """给一道失分题打标签。

    三个层次，顺序不能换：

      1. 先把"题的问题"和"系统的问题"分开。必含字符串在整份语料里都搜不到，
         那是金标与语料不一致，改系统没有任何意义。
      2. 再按"系统有没有开口"分开。拒答说明证据没进上下文（检索或判定），
         与"生成了但答偏了"是两个完全不同的故障面，修法也不同。
      3. 最后才看是不是跨块。跨块题在 top_k=6 的预算下本就吃力，
         把它和单块题混在一起算准确率，会让"题库变难"这件事被隐藏。

    返回 dict 而不是字符串，方便后面做交叉统计。
    """
    groups = item.get("must_include") or []
    if not groups:
        return {"reason": "no_gold", "n_groups": 0}

    texts = [norm(c["text"]) for c in chunks]
    per_group = []
    for g in groups:
        words = [norm(w) for w in g]
        # 一组里任一关键词命中即可（与 run_eval.hit 的口径一致）
        hit_chunks = [i for i, t in enumerate(texts) if any(w in t for w in words)]
        per_group.append(hit_chunks)

    missing = [i for i, h in enumerate(per_group) if not h]
    if missing:
        # 金标里有的分组，语料里一个字都搜不到 → 这是题的问题
        return {
            "reason": "gold_absent",
            "n_groups": len(groups),
            "missing_groups": [groups[i] for i in missing],
        }

    # 每题都找到了，看它们是不是挤在同一块里（= 单块可答）
    sets = [set(h) for h in per_group]
    common = set.intersection(*sets)
    if common:
        best = min(common, key=lambda i: len(texts[i]))
        return {
            "reason": "refused" if refused else "answered_unmatched",
            "n_groups": len(groups),
            "n_chunks_with_answer": min(len(s) for s in sets),
            "best_chunk_tokens": chunks[best]["tokens"],
            "best_chunk_file": chunks[best]["file"],
        }

    # 单块装不下：需要跨块拼
    return {
        "reason": "cross_chunk",
        "refused": refused,
        "n_groups": len(groups),
        "n_chunks_with_answer": min(len(s) for s in sets),
    }


LABEL = {
    "gold_absent": "金标问题：必含字符串在语料里一个字都搜不到",
    "cross_chunk": "跨块问题：任一块都装不下全部必含项",
    "refused": "系统拒答：答案在语料里，但没答出来（检索没召回 / 判定误杀）",
    "answered_unmatched": "答了但没命中关键词：需人工看是同义表述还是真答错",
    "no_gold": "无 must_include",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--result", default="data/eval_prod3.json")
    ap.add_argument("--config", default="naive")
    ap.add_argument("--index-tag", default="prod")
    ap.add_argument("--only", choices=["all", "new", "old"], default="all",
                    help="new = eval_set_v3 里 id 以 v3- 开头的生成题")
    ap.add_argument("--show", type=int, default=0, help="打印前 N 道失分题明细")
    a = ap.parse_args()

    res = json.loads((ROOT / a.result).read_text(encoding="utf-8"))
    row = next((r for r in res if r["config"] == a.config), None)
    if row is None:
        print(f"{a.result} 里没有 config={a.config}")
        return 1

    spec = json.loads((ROOT / "eval_set_v3.json").read_text(encoding="utf-8"))
    gold = {it["id"]: it for it in spec["items"]}
    # "新旧"不用 id 前缀判断（v3 的 id 是 Q001…Q316 连号），
    # 直接拿上一版 124 题金标的 id 集合当"旧题"真值源，避免靠约定猜。
    # 注意是 v2 不是 eval_set：v1 只有 40 题、id 是 Q01 两位，取错会得到空集。
    old_ids = {it["id"] for it in json.loads(
        (ROOT / "eval_set_v2.json").read_text(encoding="utf-8"))["items"]}
    chunks = load_chunks(a.index_tag)
    print(f"索引 {a.index_tag}：{len(chunks)} 块，"
          f"字数 {sum(len(c['text']) for c in chunks):,}")

    misses = [r for r in row["rows"] if not r["ok"] and r["type"] == "fact"]
    if a.only == "new":
        misses = [r for r in misses if r["id"] not in old_ids]
    elif a.only == "old":
        misses = [r for r in misses if r["id"] in old_ids]

    buckets: dict[str, list] = {}
    details = []
    for r in misses:
        it = gold.get(r["id"], {})
        c = classify(it, chunks, refused=bool(r.get("refused")))
        buckets.setdefault(c["reason"], []).append(r)
        details.append((r, c, it))

    def _want(rid: str) -> bool:
        return a.only == "all" or ((rid not in old_ids) == (a.only == "new"))

    n_fact = sum(1 for r in row["rows"] if r["type"] == "fact" and _want(r["id"]))
    print(f"\n[{a.config} / {a.only}] 可答题 {n_fact} 道，答错 {len(misses)} 道\n")
    for k, v in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        print(f"  {len(v):3d} 道  {LABEL.get(k, k)}")

    print("\n如果 gold_absent 为 0，说明金标没有错，失分全部来自检索/生成；")
    print("此时把准确率问题归因为『题目变难』是成立的，反之则必须回去修题。")

    if a.show:
        print()
        for r, c, it in details[: a.show]:
            print(f"- {r['id']} [{c['reason']}] {r['q'][:46]}")
            print(f"    必含 {it.get('must_include')}")
            print(f"    答: {r['answer'][:70]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
