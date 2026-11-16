"""验证一个新的 OpenAI 兼容端点能不能接进 DocuMind。

为什么需要三关，而不是"能 ping 通就行"
--------------------------------------
市面上"OpenAI 兼容"的实现差异集中在三处，恰好都是本项目依赖的：

1. **usage 字段**。成本核算是账单数字（README 第 4、15 节），拿不到 usage 就退化成估算，
   而估算正是这个项目反复强调拒绝的东西。
2. **流式 usage**。`llm_stream` 靠 `stream_options={"include_usage": True}` 拿最后一次
   usage；部分兼容实现忽略这个参数，表现为"流式能用但成本记不到"。
3. **关键词抽取的格式遵从**。LightRAG 要求模型按固定格式吐出实体/关系，
   `max_tokens=4096` 且不许有多余解释。这一步失败时不会报错，只会静默召回为空，
   表现为"每题都答证据不足"——比直接报错更难排查。

所以三关分别是：非流式 + usage / 流式 + usage / 走一遍真实问答。

用法（推荐用环境变量覆盖，不动 .env——load_dotenv 默认不 override 已有变量）：
    LLM_BASE_URL=... LLM_API_KEY=... LLM_MODEL=qwen3.8-max python -m tools.smoke_llm
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def mask(key: str) -> str:
    """永远不打印完整 key。日志和截图都可能外泄，脱敏是最便宜的保险。"""
    if not key:
        return "(未设置)"
    return f"{key[:8]}…{key[-4:]}（{len(key)} 字符）"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--question", default="订单数为什么必须去重？")
    ap.add_argument("--mode", default="naive", choices=["naive", "mix"],
                    help="naive 只测作答；mix 会额外触发 LightRAG 的关键词抽取")
    a = ap.parse_args()

    # ---- 环境自检 ----------------------------------------------------------
    base = os.environ.get("LLM_BASE_URL", "")
    key = os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "")
    print("=" * 70)
    print(f"Base URL : {base or '(未设置)'}")
    print(f"API Key  : {mask(key)}")
    print(f"Model    : {model or '(未设置)'}")
    print("=" * 70)
    if not (base and key and model):
        print("[FAIL] 三个变量必须都设置（可用环境变量覆盖 .env）")
        return 2

    # 延迟 import：让"环境没配"能快速失败，不必先加载 lightrag
    import llm_client as L
    from llm_client import llm_func, llm_stream, track_usage

    fails: list[str] = []

    # ---- 第 1 关：非流式 + usage -------------------------------------------
    print("\n[1/3] 非流式调用 + usage")
    try:
        t0 = time.perf_counter()
        txt = await llm_func("只回两个字：收到", max_tokens=16)
        dt = time.perf_counter() - t0
        print(f"  返回：{txt!r}  耗时 {dt:.2f}s")
        if not txt:
            # 同上：空 content 但消耗了输出 token，多半是推理模型。
            # 这不是"端点坏了"，是"选错模型了"——两者处置完全不同，必须分开报。
            fails.append("非流式返回空字符串（若 output_tokens>0，说明是推理模型，"
                         "content 被 reasoning 占满）")
            print("  ✗ 返回空字符串")
        else:
            print("  ✓ 有返回")
    except Exception as e:  # noqa: BLE001
        fails.append(f"非流式调用失败：{type(e).__name__}: {e}")
        print(f"  ✗ {type(e).__name__}: {e}")

    # usage 是否真的记进来了
    with track_usage() as u:
        try:
            await llm_func("只回一个字：好", max_tokens=8)
        except Exception as e:  # noqa: BLE001
            fails.append(f"usage 采集失败：{e}")
    if u.prompt_tokens > 0:
        print(f"  ✓ usage 可用：in {u.prompt_tokens} / out {u.completion_tokens} / "
              f"¥{u.cost:.6f}")
    else:
        fails.append("usage 全为 0 —— 成本无法核算，会退化成估算")
        print("  ✗ usage 为 0（该端点可能不返回 usage 字段）")

    # ---- 第 2 关：流式 + usage ---------------------------------------------
    print("\n[2/3] 流式调用 + stream usage")
    chunks: list[str] = []
    stream_usage = None
    try:
        t0 = time.perf_counter()
        first_at = None
        async for piece in llm_stream("数到五，只输出数字", max_tokens=32):
            if isinstance(piece, dict) and "__usage__" in piece:
                stream_usage = piece["__usage__"]
                continue
            if first_at is None:
                first_at = time.perf_counter() - t0
            chunks.append(piece)
        dt = time.perf_counter() - t0
        text = "".join(chunks)
        first_s = f"{first_at:.2f}s" if first_at is not None else "无"
        print(f"  收到 {len(chunks)} 段，首段 {first_s}，总 {dt:.2f}s")
        print(f"  拼接：{text[:50]!r}")
        if not chunks:
            # 分两种：真没吐字，还是把预算花在推理上了。
            # 后者非常隐蔽——usage 有 completion_tokens，但 content 是空的，
            # 表现成"模型什么都没说"。本项目 README 第 14 节记过这个问题：
            # 抽取实体必须用非思考模式，思考模式成本翻三倍且抽取质量下降。
            out_tok = (stream_usage or {}).get("completion_tokens") or 0
            if out_tok > 0:
                fails.append(
                    f"流式没有 content 增量，但 output_tokens={out_tok} —— "
                    "疑似推理模型把预算花在 reasoning 上")
                print(f"  ✗ 无 content，但 output_tokens={out_tok}：疑似推理模型")
                print("     （换非推理模型，或把 max_tokens 调到 512+ 再试）")
            else:
                fails.append("流式没有任何增量")
                print("  ✗ 流式完全没有输出")
        if stream_usage and not stream_usage.get("estimated"):
            print(f"  ✓ 流式 usage：in {stream_usage['prompt_tokens']} / "
                  f"out {stream_usage['completion_tokens']}")
        elif stream_usage:
            fails.append("流式 usage 是估算值（端点忽略了 stream_options）")
            print(f"  ✗ usage 为估算：{stream_usage}")
        else:
            fails.append("流式没有返回 usage 事件")
    except Exception as e:  # noqa: BLE001
        fails.append(f"流式调用失败：{type(e).__name__}: {e}")
        print(f"  ✗ {type(e).__name__}: {e}")

    # ---- 第 3 关：真实问答 -------------------------------------------------
    print(f"\n[3/3] DocuMind 真实问答（mode={a.mode}）")
    try:
        from query import DocuMind
        dm = await DocuMind("prod", "local", use_rerank=False).open()
        try:
            t0 = time.perf_counter()
            ans = await dm.ask(a.question, mode=a.mode, rerank=False, guard=False)
            dt = time.perf_counter() - t0
            print(f"  问题：{a.question}")
            print(f"  答案：{ans.answer[:120]}")
            print(f"  引用 {len(ans.citations)} 条，{dt:.1f}s，"
                  f"calls={ans.calls} tok={ans.tokens} ¥{ans.cost:.5f}")
            if not ans.answer.strip():
                fails.append("真实问答返回空")
            elif ans.refused and a.mode == "mix":
                # mix 模式下拒答，很可能是关键词抽取格式没被遵从
                print("  ⚠ 拒答了。mix 模式下这通常意味着关键词抽取没成功"
                      "（看上面日志有没有提取到 keywords）")
        finally:
            await dm.close()
    except Exception as e:  # noqa: BLE001
        fails.append(f"真实问答失败：{type(e).__name__}: {e}")
        print(f"  ✗ {type(e).__name__}: {e}")

    # ---- 结论 --------------------------------------------------------------
    print("\n" + "=" * 70)
    if fails:
        print(f"[FAIL] {len(fails)} 项未通过：")
        for i, f in enumerate(fails, 1):
            print(f"  {i}. {f}")
        print("\n结论：这个端点暂时不能直接替换。")
        return 1
    print("[PASS] 三关全过：非流式 / 流式 usage / 真实问答 都正常。")
    print("结论：可以改 .env 的 LLM_BASE_URL + LLM_API_KEY + LLM_MODEL 切过去。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
