"""生成 120 题金标（90 可答 + 30 必拒）。

设计原则（写进 README 也用这一段）：
1. 可答题按**能力类型**分层，不按文档段落找题——按段落找题会退化成
   "检索命中即正确"，测不出跨段推理和口径辨析。
2. must_include 用**唯一且稳定**的短串：数字优先，其次是全小写字段名。
   不写整句，因为整句要求模型复述措辞，测的是模仿不是理解。
3. 必答题全部可以在语料里逐字核对；脚本会做一次机械校验
   （每个关键词必须出现在语料原文中），校验不过直接报错。
4. 必拒题问的是"语料确实没有但看起来合理"的东西，不是"明显荒谬"的东西。
   后者测不出幻觉。

用法：
    python -m tools.gen_gold            # 校验 + 写出 eval_set_v2.json
    python -m tools.gen_gold --check    # 只校验
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (id, 能力类型, 问题, [[关键词组...]] )
# 关键词组之间是 AND，组内是 OR
ANSWERABLE = [
    # ---------------- A. 字段口径（20） ----------------
    ("Q001", "grain", "fact_order 这张表的粒度是什么？", [["一单一书", "订单行"]]),
    ("Q002", "pitfall", "订单数直接用 COUNT(*) 不去重，会高估多少？", [["28"], ["订单"]]),
    ("Q003", "scale", "dim_book 一共有多少行？", [["42,180", "42180"]]),
    ("Q004", "unit", "dim_book 的 rating_avg 用的是几分制？", [["0-10", "10 分"]]),
    ("Q005", "pitfall", "评分类指标不过滤 is_valid 会让数值虚高多少？", [["3.2"]]),
    ("Q006", "field", "计算 DAU 时应该用哪个字段去重？", [["device_id"]]),
    ("Q007", "pitfall", "fact_click 里没有登录态的事件占多少比例？", [["31%"]]),
    ("Q008", "pitfall", "用 list_price 乘数量算 GMV，会系统性高估多少？", [["18%"]]),
    ("Q009", "field", "fact_order 里赠品的 book_id 有什么特征？", [["负值"]]),
    ("Q010", "rule", "订单满多少元包邮？", [["99"], ["包邮"]]),
    ("Q011", "rule", "不满包邮门槛时运费是多少？", [["8 元", "8元", "8"]]),
    ("Q012", "formula", "discount_amount 和 pay_amount、freight 的关系是什么？",
     [["freight"], ["list_amount"]]),
    ("Q013", "pitfall", "做周同比时，除了 iso_week 还必须用哪个字段？", [["iso_year"]]),
    ("Q014", "schema", "dim_category 一共几级分类？", [["两级", "2 级", "二级"]]),
    ("Q015", "scale", "dim_date 覆盖到哪一年为止？", [["2027"]]),
    ("Q016", "enum", "bridge_book_tag 的 tag_type 有哪几种取值？", [["editorial"], ["campaign"]]),
    ("Q017", "enum", "fact_click 里已废弃、但仍残留在 2023 年数据里的事件类型是什么？", [["view"]]),
    ("Q018", "field", "fact_order 的 stat_date 是怎么来的？", [["派生", "pay_time"]]),
    ("Q019", "pitfall", "出版社更名后会换 publisher_id 吗？", [["不换", "不变", "保留"]]),
    ("Q020", "ratio", "dim_author 里 country 为「未知」的占比是多少？", [["23%"]]),

    # ---------------- B. 指标口径（16） ----------------
    ("Q021", "metric", "gmv_net 会扣掉退款中的订单吗？", [["仍计入", "不扣", "只有 refunded"]]),
    ("Q022", "metric", "客单价 aov 的分母用的是哪个指标？", [["order_cnt"]]),
    ("Q023", "metric", "ARPPU 的分母是活跃用户还是付费用户？", [["付费用户"]]),
    ("Q024", "metric", "退款率的分母包不包括未支付的关闭订单？", [["不包括", "不含"]]),
    ("Q025", "metric", "CTR 的分母是什么？", [["曝光"]]),
    ("Q026", "metric", "渠道 ROI 的分子用的是总额还是净额？", [["净额", "gmv_net"]]),
    ("Q027", "metric", "7 日留存指的是第 7 天当天活跃，还是 7 天内任意一天活跃？",
     [["第 7 天"]]),
    ("Q028", "metric", "会话是按多长时间无操作切分的？", [["30 分钟"]]),
    ("Q029", "metric", "渠道 ROI 排名对样本量有什么门槛？", [["100"], ["单"]]),
    ("Q030", "metric", "ltv_30d 的观察窗口是多少天？", [["30"], ["天"]]),
    ("Q031", "metric", "折扣深度的分母是哪个字段？", [["list_amount"]]),
    ("Q032", "metric", "付费转化率 pay_rate 的分子和分母分别是什么？",
     [["paid_user_cnt"], ["dau_uv"]]),
    ("Q033", "metric", "复购率的定义里，下单次数要达到几次才算复购？", [["2 次", "≥2", "两次"]]),
    ("Q034", "metric", "MAU 能不能由每天的 DAU 相加得到？", [["不能"], ["相加"]]),
    ("Q035", "scale", "本项目的指标字典一共声明了多少个指标？", [["32"], ["指标"]]),
    ("Q036", "metric", "order_line_cnt 和 order_cnt 大概差多少倍？", [["1.28"]]),

    # ---------------- C. SQL 安全（10） ----------------
    ("Q037", "guard", "SQL 安全规则一共有几条？", [["18 条", "18条"], ["规则", "条"]]),
    ("Q038", "guard", "表名不在表卡白名单里时返回哪个错误码？", [["E1031"]]),
    ("Q039", "guard", "为什么不用正则做 SQL 安全校验？", [["绕过", "AST"]]),
    ("Q040", "guard", "访问系统表被拒绝时返回什么码？", [["E1064"]]),
    ("Q041", "guard", "扫描行数的估算上限是多少？", [["500 万", "500万"]]),
    ("Q042", "guard", "单条 SQL 的执行超时是多少秒？", [["15"], ["超时"]]),
    ("Q043", "guard", "超时检查的 progress_handler 回调间隔是多少？", [["10ms", "10 ms"]]),
    ("Q044", "guard", "结果集行数超过多少会被截断并报 E1071？", [["10,000", "10000"]]),
    ("Q045", "guard", "SQL 执行的并发闸门默认值是多少？", [["8"], ["并发"]]),
    ("Q046", "guard", "哪两个列属于敏感列，一律不进结果集？", [["user_phone"], ["id_card"]]),

    # ---------------- D. 错误码（10） ----------------
    ("Q047", "error", "E1052 表示什么问题？", [["上下文超长", "上下文"]]),
    ("Q048", "error", "E1080 证据不足拒答，在接口上是按正常返回还是按错误返回？", [["正常"]]),
    ("Q049", "error", "E1042 是什么错误？", [["并发", "闸门"]]),
    ("Q050", "error", "SQL 里出现分号分隔的多条语句，会返回哪个码？", [["E1062"]]),
    ("Q051", "error", "E1071 和 E1072 的区别是什么？", [["结果集"], ["扫描"]]),
    ("Q052", "error", "请求来源不在白名单时返回哪个错误码？", [["E1012"]]),
    ("Q053", "error", "语义层 YAML 校验失败返回什么码？", [["E1030"]]),
    ("Q054", "error", "sqlglot 无法解析生成的 SQL 时返回什么码？", [["E1060"]]),
    ("Q055", "error", "日调用额度用尽对应哪个错误码？", [["E1041"]]),
    ("Q056", "error", "数据库只读快照缺失或损坏时返回什么码？", [["E1090"]]),

    # ---------------- E. 接口与 SSE（10） ----------------
    ("Q057", "api", "/readyz 会检查哪两样东西？", [["warehouse"], ["semantic"]]),
    ("Q058", "api", "citations 事件最多返回几条引用？", [["5 条", "5条", "最多 5"]]),
    ("Q059", "api", "SSE 的保活 ping 间隔是多少秒？", [["15"], ["秒"]]),
    ("Q060", "api", "naive 模式的 p50 延迟是多少秒？", [["1.1"]]),
    ("Q061", "api", "SSE 的 done 事件里，用哪个字段表示本次成本（人民币）？", [["cost_cny"]]),
    ("Q062", "api", "/v1/ask 的 question 参数长度上限是多少字符？", [["500"]]),
    ("Q063", "api", "MCP 里用来查询指标口径的工具叫什么？", [["describe_metric"]]),
    ("Q064", "api", "MCP Server 用的是哪种传输方式？", [["stdio"]]),
    ("Q065", "api", "反馈接口 /v1/feedback 的 verdict 支持哪几种取值？", [["partial"]]),
    ("Q066", "api", "调试参数 debug=true 会额外返回什么？", [["检索证据", "证据"], ["判定"]]),

    # ---------------- F. 配额与成本（6） ----------------
    ("Q067", "quota", "free 套餐的每日 token 上限是多少？", [["200,000", "200000"]]),
    ("Q068", "quota", "business 套餐的 QPS 配额是多少？", [["30"], ["QPS"]]),
    ("Q069", "cost", "一次 full 模式的问答，典型成本是多少元？", [["0.026"]]),
    ("Q070", "cost", "6 万字语料建一次索引大约花多少钱？", [["0.9"]]),
    ("Q071", "quota", "E1042 响应头里的 Retry-After 单位是什么？", [["毫秒"]]),
    ("Q072", "cost", "输入 token 的单价大约是输出 token 的多少？", [["四分之一", "1/4"]]),

    # ---------------- G. 部署与运维（6） ----------------
    ("Q073", "ops", "容器镜像大约多大？", [["1.2 GB", "1.2GB"]]),
    ("Q074", "ops", "索引期的内存峰值大约是多少？", [["1.4 GB", "1.4GB"]]),
    ("Q075", "ops", "trace 数据保留多少天？", [["7 天", "7天"]]),
    ("Q076", "ops", "审计日志保留多少天？", [["90"], ["审计"]]),
    ("Q077", "ops", "并发压测的性能拐点出现在几并发？", [["8"], ["并发"]]),
    ("Q078", "ops", "送进重排的文本被截断到多少字符？", [["512"]]),

    # ---------------- H. 权限（4） ----------------
    ("Q079", "auth", "五个角色里，哪个角色不能提问？", [["auditor", "审计"]]),
    ("Q080", "auth", "viewer 角色被明确禁止的操作是什么？", [["导出"]]),
    ("Q081", "auth", "访问令牌的有效期是多久？", [["8 小时", "8小时"]]),
    ("Q082", "auth", "租户隔离依赖几样东西？", [["三样", "三"]]),

    # ---------------- I. 数据质量（4） ----------------
    ("Q083", "quality", "30 条数据质量规则分成哪两个级别？", [["阻塞"], ["告警"]]),
    ("Q084", "quality", "Q29 校验的是哪两个指标之间的大小关系？", [["UV"], ["PV"]]),
    ("Q085", "quality", "校验 rating_avg 一致性时抽样多少本书？", [["100"], ["本"]]),
    ("Q086", "quality", "GMV 日环比突变的告警阈值是多少？", [["50%"]]),

    # ---------------- J. 评测与架构（8） ----------------
    ("Q087", "eval", "在 30 道可答题的评测里，1 道题值多少个百分点？", [["3.3"]]),
    ("Q088", "eval", "扩到 120 题后，1 道题大约值多少个百分点？", [["0.83"]]),
    ("Q089", "eval", "为什么不用 LLM 当评测裁判？", [["不可复现", "稳定"]]),
    ("Q090", "eval", "mix 配置的分数为什么反而低于 naive？", [["干扰"]]),
    ("Q091", "eval", "判定器改成「先摘录再判定」后，误拒率从 20% 降到了多少？", [["0%"]]),
    ("Q092", "eval", "评测主指标 EX 的全称含义是什么？", [["结果集"]]),
    ("Q093", "arch", "整个项目一共做了 10 项选型，其中语义检索用的是什么方案？", [["BM25"]]),
    ("Q094", "arch", "MCP 协议手写实现大约多少行代码？", [["200"]]),
]

UNANSWERABLE = [
    ("Q095", "竞品对比", "你们和市面上的 Text2SQL 商业产品相比，准确率高多少？"),
    ("Q096", "路线图", "2027 年第一季度打算上线哪些新功能？"),
    ("Q097", "组织", "这个项目组目前有几个后端工程师？"),
    ("Q098", "薪酬", "数据分析培训生这个岗位的转正薪资是多少？"),
    ("Q099", "用户数据", "用户 88213 上个月买了哪几本书？"),
    ("Q100", "数据库", "接入 Oracle 时需要改哪几个配置文件？"),
    ("Q101", "合同", "和最大的一家出版社签的年度合同金额是多少？"),
    ("Q102", "经营", "公司去年全年的营业收入是多少？"),
    ("Q103", "客户名单", "目前付费客户里有哪些是上市公司？"),
    ("Q104", "训练", "这个项目用的模型是在什么数据上预训练的？"),
    ("Q105", "参数", "deepseek-chat 的参数量是多少亿？"),
    ("Q106", "地域", "服务部署在哪个城市的哪个可用区？"),
    ("Q107", "对比", "你们产品的 NPS 评分是多少？"),
    ("Q108", "SLA", "服务不可用时按合同要赔多少钱？"),
    ("Q109", "灾备", "灾备切换的 RTO 和 RPO 分别是多少？"),
    ("Q110", "许可", "商业授权一年的费用是多少？"),
    ("Q111", "发票", "开具增值税专用发票需要提供哪些材料？"),
    ("Q112", "数据", "2026 年 5 月 20 日全站 GMV 的具体数值是多少？"),
    ("Q113", "明细", "book_id 为 10023 的那本书叫什么名字？"),
    ("Q114", "渠道", "字节渠道在 5 月实际花了多少投放费用？"),
    ("Q115", "招聘", "这个岗位面试一共几轮，每轮考什么？"),
    ("Q116", "办公", "团队平时在哪个办公区上班，几点打卡？"),
    ("Q117", "法务", "用户数据的跨境传输适用哪条法律条款？"),
    ("Q118", "性能", "在 32 核机器上单机能支撑多少 QPS？"),
    ("Q119", "代码", "tests 目录下一共有多少个测试文件？"),
    ("Q120", "淘汰", "v1.0 版本里被删掉的那个功能叫什么？"),
    ("Q121", "合作", "有没有和高校实验室做过联合研究？"),
    ("Q122", "指标", "行业平均的图书电商退款率是多少？"),
    ("Q123", "客服", "用户投诉后多久内必须回复？"),
    ("Q124", "硬件", "推荐的服务器 CPU 型号和主频是什么？"),
]

META = {
    "version": 2,
    "corpus": "seed/insightpilot-docs-plus.md + seed/v2/*.md（23 篇，104,603 字，116 块 @600/80）",
    "answerable": 94,
    "unanswerable": 30,
    "judge": "关键词命中（每组至少命中一个，组间 AND），不使用 LLM 打分",
    "taxonomy": "按能力类型分层：grain/pitfall/metric/guard/error/api/quota/ops/auth/quality/eval/arch",
    "note": "可答题答案均可在语料原文逐字核对；不可答题的正确答案是拒答（refused=true）",
}


def load_corpus() -> str:
    parts = []
    for p in sorted((ROOT / "seed").rglob("*.md")):
        parts.append(p.read_text(encoding="utf-8"))
    return "\n".join(parts)


def check(corpus: str) -> list[str]:
    errs = []
    ids = set()
    for iid, typ, q, groups in ANSWERABLE:
        if iid in ids:
            errs.append(f"{iid} 重复")
        ids.add(iid)
        for g in groups:
            if not any(alt in corpus for alt in g):
                errs.append(f"{iid} 关键词在语料中找不到：{g}  （问题：{q}）")
    for iid, typ, q in UNANSWERABLE:
        if iid in ids:
            errs.append(f"{iid} 重复")
        ids.add(iid)
    return errs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--out", default="eval_set_v2.json")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    corpus = load_corpus()
    errs = check(corpus)
    print(f"语料 {len(corpus):,} 字符")
    print(f"可答 {len(ANSWERABLE)} 题（{len({t for _, t, _, _ in ANSWERABLE})} 个能力类型）")
    print(f"必拒 {len(UNANSWERABLE)} 题")
    if errs:
        print(f"\n[FAIL] {len(errs)} 处校验不通过：")
        for e in errs:
            print("  -", e)
        sys.exit(1)
    print("[OK] 所有关键词都能在语料原文中找到")

    if args.check:
        return

    items = []
    for iid, typ, q, groups in ANSWERABLE:
        items.append({"id": iid, "type": "fact", "task": typ, "q": q, "must_include": groups})
    for iid, typ, q in UNANSWERABLE:
        items.append({"id": iid, "type": "refuse", "task": typ, "q": q})

    meta = dict(META)
    meta["answerable"] = len(ANSWERABLE)
    meta["unanswerable"] = len(UNANSWERABLE)
    payload = {"meta": meta, "items": items}
    out = ROOT / args.out
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写出 {out}（{len(items)} 题）")


if __name__ == "__main__":
    main()
