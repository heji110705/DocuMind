"""failover 演练：手动注入坏 key → 确认切到备用 → 恢复。

为什么单独一个脚本、**不进 CI**：
1. 它真的会打上游（一次坏 key + 两次真调用），CI 里每次跑都是白烧钱；
2. 它依赖真实的 `LLM_API_KEY` / `LLM_FALLBACK_*`，CI 里没有这些变量，
   硬塞假值只会测出"假红"；
3. 它会把进程内的 key 临时改坏——在共享 runner 上做这件事，风险和收益不成比例。

用法（仓库根目录，先确认 .env 里有 fallback 三项）：

    python scripts/drill_fallback.py             # 完整演练
    python scripts/drill_fallback.py --dry-run   # 只打印配置，不发请求

演练四步：
    1. 真 key + 关 failover  → 期望 model_used = 主模型
    2. 坏 key + 开 failover  → 期望切到备用，fallbacks=1
    3. 坏 key + 关 failover  → 期望直接抛 LLMAuthError（不重试、不切）
    4. 恢复真 key            → 期望回到主模型

任何一步不符合预期就返回 1。脚本只在内存里改 os.environ，不写回 .env，
进程结束即恢复——不会把你的 key 弄丢。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import llm_client as L  # noqa: E402


def mask(value: str) -> str:
    if not value:
        return "<empty>"
    return f"{value[:6]}...{value[-4:]}" if len(value) > 12 else "***"


def show_config() -> bool:
    """打印当前配置；返回是否具备演练条件。"""
    primary = os.environ.get("LLM_MODEL", "deepseek-chat")
    fb_model = os.environ.get("LLM_FALLBACK_MODEL", "")
    fb_url = os.environ.get("LLM_FALLBACK_BASE_URL", "")
    fb_key = os.environ.get("LLM_FALLBACK_API_KEY", "")
    enabled = L.fallback_enabled()
    print("当前配置")
    print(f"  主模型      : {primary}  (key {mask(os.environ.get('LLM_API_KEY', ''))})")
    print(f"  备用模型    : {fb_model or '<未配置>'}")
    print(f"  备用端点    : {fb_url or '<未配置>'}")
    print(f"  备用 key    : {mask(fb_key)}")
    print(f"  FAILOVER    : {os.environ.get('LLM_FALLBACK_ENABLED', '<未设置>')} → {enabled}")
    if not (fb_model and fb_url and fb_key):
        print("\n[跳过] 备用模型三项没配齐，演练没有意义。")
        print("       在 .env 里填 LLM_FALLBACK_MODEL / _BASE_URL / _API_KEY 后再跑。")
        return False
    if not os.environ.get("LLM_API_KEY"):
        print("\n[跳过] 主模型 LLM_API_KEY 为空，无法演练 failover。")
        return False
    return True


async def one_call(tag: str) -> dict:
    """打一次真实调用，返回本次账本快照。"""
    L.USAGE.reset()
    t0 = time.perf_counter()
    text = await L.llm_func("只回两个字：收到", max_tokens=16, temperature=0)
    dt = time.perf_counter() - t0
    snap = L.USAGE.snapshot()
    print(f"  {tag}: reply={text!r} model_used={snap['model_used'] or '<空>'} "
          f"fallbacks={snap['fallbacks']} calls={snap['calls']} {dt:.1f}s")
    return snap


def reset_client() -> None:
    """key 变了必须重建客户端——AsyncOpenAI 在构造时就把 key 记住了。"""
    L._client = None


async def main() -> int:
    ap = argparse.ArgumentParser(description="failover 演练（会打真实上游，勿进 CI）")
    ap.add_argument("--dry-run", action="store_true", help="只打印配置，不发请求")
    args = ap.parse_args()

    if not show_config():
        return 2
    if args.dry_run:
        print("\n[dry-run] 不发请求。")
        return 0

    real_key = os.environ["LLM_API_KEY"]
    fb_enabled_raw = os.environ.get("LLM_FALLBACK_ENABLED", "")
    fb_model = os.environ["LLM_FALLBACK_MODEL"]
    bad_key = "sk-drill-invalid-key-0000"
    fails = 0

    try:
        print("\n[1/4] 真 key + 关 failover → 应该走主模型")
        os.environ["LLM_FALLBACK_ENABLED"] = "0"
        reset_client()
        snap = await one_call("基线")
        if snap["model_used"] and snap["model_used"] == os.environ.get("LLM_MODEL", "deepseek-chat"):
            print("  PASS")
        else:
            print("  FAIL 没走主模型（检查 .env 的 LLM_MODEL 与 key）")
            fails += 1

        print("\n[2/4] 坏 key + 开 failover → 应该切到备用")
        os.environ["LLM_API_KEY"] = bad_key
        os.environ["LLM_FALLBACK_ENABLED"] = "1"
        reset_client()
        snap = await one_call("坏 key")
        if snap["model_used"] == fb_model and snap["fallbacks"] >= 1:
            print("  PASS 已切到备用")
        else:
            print(f"  FAIL 期望 model_used={fb_model} 且 fallbacks>=1")
            fails += 1

        print("\n[3/4] 坏 key + 关 failover → 应该直接 401 失败，不重试不切")
        os.environ["LLM_FALLBACK_ENABLED"] = "0"
        L.USAGE.reset()
        try:
            await L.llm_func("只回两个字：收到", max_tokens=16, temperature=0)
        except L.LLMAuthError:
            print(f"  PASS 抛了 LLMAuthError（retries={L.USAGE.retries}，应为 0）")
            if L.USAGE.retries != 0:
                print("  FAIL 401 不该重试")
                fails += 1
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL 期望 LLMAuthError，实际 {type(e).__name__}: {e}")
            fails += 1
        else:
            print("  FAIL 坏 key 居然成功了")
            fails += 1

        print("\n[4/4] 恢复真 key → 应该回到主模型")
        os.environ["LLM_API_KEY"] = real_key
        os.environ["LLM_FALLBACK_ENABLED"] = fb_enabled_raw or "0"
        reset_client()
        snap = await one_call("恢复")
        if snap["model_used"] == os.environ.get("LLM_MODEL", "deepseek-chat") and snap["fallbacks"] == 0:
            print("  PASS 已恢复")
        else:
            print("  FAIL 没回到主模型")
            fails += 1
    finally:
        # 无论在内存里改过什么，退出前都还原（进程结束本来也会丢，这里是为了
        # 让同一个进程里后续的调试行为也正常）。
        os.environ["LLM_API_KEY"] = real_key
        if fb_enabled_raw:
            os.environ["LLM_FALLBACK_ENABLED"] = fb_enabled_raw
        else:
            os.environ.pop("LLM_FALLBACK_ENABLED", None)
        reset_client()

    print("\n结果：", "全部通过" if fails == 0 else f"{fails} 步不符合预期")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
