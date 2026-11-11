"""造一个带标题层级 + 线框表格的测试 PDF，用来验证 loaders.py 的版面解析。

不用现成的 PDF 样例，是因为要能控制"坑"：故意放一个跨页表格、
一个用空格对齐的假表格（无线框）、一个页脚，看解析器会不会把它们搞混。
"""

from pathlib import Path

import pymupdf as fitz

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "sample-report.pdf"

PAGES = [
    {
        "h1": "InsightPilot 数据字典（节选）",
        "h2": "一、订单事实表 fact_order",
        "body": [
            "本表粒度为一单一书一行，一个订单购买多本书时会拆成多行。"
            "因此计算订单数必须使用 COUNT(DISTINCT order_id)，"
            "直接 COUNT(*) 得到的是订单行数，实测会高估 28%。",
            "金额字段 pay_amount 是实付金额，已扣除优惠并包含运费。"
            "若要还原优惠金额，使用 list_amount - (pay_amount - freight) 计算。",
        ],
        "table": [
            ["字段", "类型", "单位", "说明"],
            ["order_id", "TEXT", "", "订单号，一单多书共享"],
            ["pay_amount", "REAL", "元", "实付金额"],
            ["freight", "REAL", "元", "运费，满 99 包邮"],
            ["qty", "INTEGER", "本", "购买数量"],
            ["status", "TEXT", "", "paid/shipped/done/closed"],
        ],
    },
    {
        "h1": "",
        "h2": "二、指标口径",
        "body": [
            "退款率的分母是支付成功订单数，不含未支付的关闭单。"
            "如果分母包含 closed，退款率会被系统性低估约 4 个百分点。",
            "渠道 ROI 只对付费渠道计算，自然流量没有成本分母，除法的结果没有业务含义。",
        ],
        "table": [
            ["指标", "公式", "单位"],
            ["order_cnt", "COUNT(DISTINCT order_id)", "单"],
            ["aov", "gmv_gross / order_cnt", "元"],
            ["refund_rate", "退款单数 / 支付成功单数", "%"],
        ],
    },
    {
        "h1": "",
        "h2": "三、错误码",
        "body": [
            "E1061 表示 SQL 被安全策略拒绝，常见原因是引用了未声明的表或命中敏感列。",
            "E1080 是证据不足拒答，HTTP 状态仍为 200，属于正常返回而不是故障。",
        ],
        "table": [
            ["码", "HTTP", "含义"],
            ["E1061", "403", "SQL 被安全策略拒绝"],
            ["E1082", "408", "查询超时"],
        ],
    },
]


def draw_table(page, x0, y0, rows, col_w, row_h=22):
    """手画线框表格：find_tables 靠检测线条，所以必须真的画线。"""
    n_r, n_c = len(rows), len(rows[0])
    for r in range(n_r + 1):
        y = y0 + r * row_h
        page.draw_line((x0, y), (x0 + sum(col_w), y), color=(0.3, 0.3, 0.3), width=0.7)
    x = x0
    edges = [x0]
    for w in col_w:
        x += w
        edges.append(x)
    for x in edges:
        page.draw_line((x, y0), (x, y0 + n_r * row_h), color=(0.3, 0.3, 0.3), width=0.7)

    for r, row in enumerate(rows):
        cx = x0
        for c, cell in enumerate(row):
            page.insert_text((cx + 6, y0 + r * row_h + 15), cell, fontsize=8.5,
                             fontname="china-s")
            cx += col_w[c]


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    for i, spec in enumerate(PAGES, 1):
        page = doc.new_page(width=595, height=842)  # A4
        y = 70
        if spec["h1"]:
            page.insert_text((60, y), spec["h1"], fontsize=22, fontname="china-s")
            y += 46
        page.insert_text((60, y), spec["h2"], fontsize=15, fontname="china-s")
        y += 32
        for para in spec["body"]:
            rc = page.insert_textbox(fitz.Rect(60, y, 535, y + 90), para, fontsize=10.5,
                                     fontname="china-s", lineheight=1.7)
            y += 90 - max(0, rc)
        y += 10
        page.insert_text((60, y), "表 " + str(i), fontsize=9, fontname="china-s",
                         color=(0.4, 0.4, 0.4))
        y += 18
        draw_table(page, 60, y, spec["table"], [125, 150, 60, 140])
        page.insert_text((290, 810), f"- {i} -", fontsize=9, fontname="helv",
                         color=(0.5, 0.5, 0.5))
    doc.save(OUT)
    doc.close()
    print(f"已生成 {OUT}（{len(PAGES)} 页，{OUT.stat().st_size / 1024:.0f} KB）")


if __name__ == "__main__":
    main()
