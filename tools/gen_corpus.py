"""把 corpus_build 下的渲染器写成 seed/v2/*.md。

用法：
    python -m tools.gen_corpus            # 生成
    python -m tools.gen_corpus --stats    # 只统计不写

为什么要生成而不是手写：
    这些文档里的每个数字（字段口径、错误码、配额、变更版本）都必须与金标题一致。
    手写时改一处忘三处，评测会把这种不一致当成模型错误。
    生成保证了「文档说的」和「判分依据」来自同一个源。
"""

import argparse
import sys
from pathlib import Path

from tools.corpus_build.doc_faq import render_faq, render_runbook
from tools.corpus_build.doc_ops import (render_api, render_changelog,
                                        render_errors, render_guard,
                                        render_quota, render_terms)
from tools.corpus_build.doc_schema import render_fields, render_metrics
from tools.corpus_build.doc_more import (render_cases, render_metric_tree,
                                         render_onboard, render_scenes,
                                         render_templates)
from tools.corpus_build.doc_more2 import render_antipatterns
from tools.corpus_build.doc_sys import (render_capacity, render_frontend,
                                        render_permission, render_prompt,
                                        render_quality)

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "seed" / "v2"

DOCS = [
    ("10-字段字典.md", render_fields),
    ("11-指标字典.md", render_metrics),
    ("12-SQL安全与沙箱.md", render_guard),
    ("13-错误码与排障.md", render_errors),
    ("14-接口与SSE参考.md", render_api),
    ("15-限流与配额.md", render_quota),
    ("16-版本变更记录.md", render_changelog),
    ("17-术语表.md", render_terms),
    ("18-常见问题.md", render_faq),
    ("19-运维手册.md", render_runbook),
    ("20-权限与多租户.md", render_permission),
    ("21-数据质量规则.md", render_quality),
    ("22-提示词工程.md", render_prompt),
    ("23-容量与压测.md", render_capacity),
    ("24-前端交互规范.md", render_frontend),
    ("25-查询模板与建模.md", render_templates),
    ("26-接入指南.md", render_onboard),
    ("27-客户案例.md", render_cases),
    ("28-分析场景手册.md", render_scenes),
    ("29-指标体系与拆解.md", render_metric_tree),
    ("30-反模式清单.md", render_antipatterns),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    OUT.mkdir(parents=True, exist_ok=True)
    total = 0
    rows = []
    for name, fn in DOCS:
        text = fn().strip() + "\n"
        total += len(text)
        rows.append((name, len(text)))
        if not args.stats:
            (OUT / name).write_text(text, encoding="utf-8")

    # 旧语料（01-06 那 5 篇）也算进来，最终块数取决于合并后的总量
    legacy = 0
    for p in (ROOT / "seed").glob("*.md"):
        legacy += len(p.read_text(encoding="utf-8"))

    for name, n in rows:
        print(f"  {name:<24} {n:>7,} 字")
    print("-" * 40)
    print(f"  新增合计 {total:>7,} 字，{len(rows)} 篇")
    print(f"  旧语料   {legacy:>7,} 字")
    allc = total + legacy
    print(f"  全部     {allc:>7,} 字")
    print(f"  估算块数（chunk 600 token ≈ 1000 字）: {allc / 1000:.0f}")
    print(f"  估算块数（chunk 800 token ≈ 1330 字）: {allc / 1330:.0f}")
    if not args.stats:
        print(f"\n已写入 {OUT}")


if __name__ == "__main__":
    main()
