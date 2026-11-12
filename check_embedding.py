"""检查 embedding 后端是否可用：维度、语义相似度、耗时、错误诊断。

用法：
    python check_embedding.py            # 按 .env 的 EMBED_BACKEND
    python check_embedding.py --embed local     # 强制本地
    python check_embedding.py --embed bailian   # 强制云端
"""

import argparse
import asyncio
import sys
import time

import numpy as np
from dotenv import load_dotenv

from embedders import build_embedder, default_backend

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv(".env")

TEXTS = [
    "订单数必须按订单号去重，否则一单多书会重复计数",
    "统计订单量时要去掉重复的订单号",
    "今天广州的天气怎么样",
]


def cos(a, b):
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--embed", default=default_backend(), choices=["bailian", "local"])
    args = p.parse_args()

    fn, dim, name = build_embedder(args.embed)
    print(f"后端     : {name}")
    print(f"配置维度 : {dim}")
    print("-" * 62)

    t0 = time.perf_counter()
    try:
        vecs = asyncio.run(fn(TEXTS))
    except Exception as e:
        msg = str(e).replace("\n", " ")
        print(f"[FAIL] {type(e).__name__}")
        print(f"       {msg[:500]}")
        print("-" * 62)
        low = msg.lower()
        if "401" in msg or "invalid_api_key" in low:
            print("诊断：key 无效。控制台重新建一个，注意别复制空格。")
        elif "403" in msg or "denied" in low:
            print("诊断：模型未在该工作空间开通。控制台「模型广场」开通，或改用 --embed local。")
        elif "429" in msg or "rate limit" in low:
            print("诊断：限流或额度用尽。等 1 分钟；或改用 --embed local。")
        elif "arrearage" in low or "欠费" in msg:
            print("诊断：账户欠费。充值后重试，或改用 --embed local。")
        elif "connection" in low or "timeout" in low:
            print("诊断：网络不通。本地模型首次下载需要能访问镜像站。")
        sys.exit(1)

    elapsed = time.perf_counter() - t0
    got_dim = len(vecs[0])
    print(f"[OK] 调用成功，耗时 {elapsed * 1000:.0f} ms")
    print(f"     返回 {len(vecs)} 条，实际维度 {got_dim}")

    if got_dim != dim:
        print(f"[WARN] 配置 {dim} ≠ 实际 {got_dim}，请把维度改成 {got_dim}，"
              f"否则向量库写入会报错。")
    else:
        print(f"[OK] 维度一致：{got_dim}")

    same = cos(vecs[0], vecs[1])
    diff = cos(vecs[0], vecs[2])
    print(f"[CHECK] 近义句相似度 {same:.4f}（期望 > 0.65）")
    print(f"[CHECK] 无关句相似度 {diff:.4f}（期望 < 0.5）")
    if same > diff + 0.2:
        print("[PASS] 向量空间正常：近义句明显靠近，无关句分开。")
    else:
        print("[FAIL] 区分度不足，检查模型是否选错或文本被截断。")

    print("-" * 62)
    if args.embed == "bailian":
        print("本次费用约 ¥0.00001（0.5 元/百万 token）")
    else:
        print("本次费用 ¥0（本地推理，只吃 CPU）")
        print("下一步：python run_index.py --input seed --pages 0 --embed local --tag prod")


if __name__ == "__main__":
    main()
