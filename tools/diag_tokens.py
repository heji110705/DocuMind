"""把一次提问的 token 花在哪儿拆开看：哪一步、多少次调用、各占多少。

为什么需要这个工具
------------------
README 上的"单题 ¥0.0038 / 3,353 token"其实**低估**了真实消耗：
LightRAG 把关键词抽取的结果缓存在 kv_store_llm_response_cache.json 里，
同一道题第二次跑就不再调模型。所以评测跑出来的 calls 往往只有 1 次/题，
而冷启动时是 2~3 次。

这个脚本在 OpenAI 客户端外面套一层记录器，把每次调用的
(prompt_tokens, completion_tokens, 耗时, 提示词特征) 抓下来，
再按提示词内容判断它属于链路上的哪一步：

    rewrite   多轮指代消解（只有带 history 才有）
    extract   LightRAG 关键词抽取（检索的前置步骤）
    judge     证据充分性判定
    answer    作答

用法：
    python -m tools.diag_tokens "毛利率是怎么算的？" --mode mix
    python -m tools.diag_tokens "毛利率是怎么算的？" --mode mix --guard
    python -m tools.diag_tokens "那它有哪些口径坑？" --mode mix --history
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def classify(prompt: str, system: str | None) -> str:
    """按提示词特征判断这一步是什么。

    特征都是从 prompts.py / LightRAG 模板里抄的稳定标记，
    不用正则去猜语义——猜错了会把成本归因到错误的步骤上，
    那比不给归因更糟。
    """
    head = (system or "") + "\n" + prompt
    if "把【本轮问题】改写成" in head:
        return "rewrite 指代消解"
    if "第一步 摘录" in head or "你在做 RAG 的证据审核" in head:
        return "judge 证据判定"
    if "【证据】" in head or "与问题直接相关的原文" in head:
        return "answer 作答"
    if "entity" in head.lower() or "实体" in head or "keywords" in head.lower():
        return "extract 关键词抽取"
    return "other 其他"


async def run(question: str, mode: str, guard: bool, use_history: bool, tag: str,
              rerank: bool = False) -> int:
    import llm_client as L
    from query import DocuMind

    real = L.client()
    records: list[dict] = []

    class _Proxy:                      # 包住 chat.completions.create
        pass

    class _Completions:
        async def create(self, **kw):
            t0 = asyncio.get_event_loop().time()
            r = await real.chat.completions.create(**kw)
            dt = asyncio.get_event_loop().time() - t0
            if getattr(r, "usage", None):
                msgs = kw.get("messages") or []
                sysmsg = next((m["content"] for m in msgs if m.get("role") == "system"), None)
                user = next((m["content"] for m in reversed(msgs) if m.get("role") == "user"), "")
                records.append({
                    "step": classify(user, sysmsg),
                    "in": r.usage.prompt_tokens,
                    "out": r.usage.completion_tokens,
                    "sec": dt,
                    "head": user[:40].replace("\n", " "),
                })
            return r

    class _Chat:
        completions = _Completions()

    proxy = _Proxy()
    proxy.chat = _Chat()
    L._client = proxy

    dm = DocuMind(tag=tag, embed="local", use_rerank=True)
    await dm.open()
    history = None
    if use_history:
        history = [{"q": "毛利率是怎么算的？", "a": "毛利率 = 毛利 / 净收入，毛利率只算商品成本，不含运费和券。"}]
    try:
        ans = await dm.ask(question, mode=mode, rerank=rerank, guard=guard, history=history)
    finally:
        await dm.close()
        L._client = real

    print(f"\n问题：{question}")
    print(f"配置：mode={mode} guard={guard} history={use_history} rerank={rerank}\n")
    print(f"{'步骤':<18}{'输入':>8}{'输出':>8}{'耗时':>8}  提示词开头")
    print("-" * 78)
    for r in records:
        print(f"{r['step']:<18}{r['in']:>8,}{r['out']:>8,}{r['sec']:>7.1f}s  {r['head']}")
    tin = sum(r["in"] for r in records)
    tout = sum(r["out"] for r in records)
    print("-" * 78)
    print(f"{'合计':<18}{tin:>8,}{tout:>8,}   调用 {len(records)} 次")
    print(f"\n按步骤汇总：")
    for step in sorted({r["step"] for r in records}):
        rs = [r for r in records if r["step"] == step]
        s = sum(x["in"] + x["out"] for x in rs)
        print(f"  {step:<18} {len(rs)} 次  {s:>7,} tok  占 {(s / (tin + tout) * 100) if tin + tout else 0:5.1f}%")
    print(f"\n答案：{ans.answer[:80]}")
    print(f"账本：calls={ans.calls} tokens={ans.tokens} cost=¥{ans.cost:.5f}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--mode", default="mix", choices=["naive", "local", "global", "hybrid", "mix"])
    ap.add_argument("--guard", action="store_true", help="加证据判定（默认不加）")
    ap.add_argument("--history", action="store_true", help="带一轮历史（触发指代消解）")
    ap.add_argument("--rerank", action="store_true", help="开本地重排（慢，20 秒量级）")
    ap.add_argument("--tag", default="prod")
    a = ap.parse_args()
    return asyncio.run(run(a.question, a.mode, a.guard, a.history, a.tag, a.rerank))


if __name__ == "__main__":
    raise SystemExit(main())
