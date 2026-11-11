"""渲染：建模规范与查询模板、接入指南、客户案例。"""

TEMPLATES = [
    ("T01", "单指标总量", "问「一共有多少」「总共多少」时用，不带分组",
     "SELECT COUNT(*) AS 在售书目数 FROM dim_book WHERE is_active = 1;"),
    ("T02", "按维度分组排名", "问「TOP N」「哪几个最多」时用，必须带 LIMIT 和 ORDER BY",
     "SELECT c.channel_name AS 渠道, ROUND(SUM(o.pay_amount), 2) AS GMV\n"
     "FROM fact_order o JOIN dim_channel c ON o.channel_id = c.channel_id\n"
     "WHERE o.status <> 'closed' AND o.stat_date BETWEEN '2026-05-01' AND '2026-05-31'\n"
     "GROUP BY c.channel_name ORDER BY GMV DESC LIMIT 10;"),
    ("T03", "比率类", "分子分母分开聚合再相除，不要先算每行的比率再平均",
     "SELECT ROUND(100.0 * SUM(is_click) / COUNT(*), 2) AS CTR\n"
     "FROM fact_click\n"
     "WHERE event_type IN ('impression','click') AND stat_date = '2026-05-20';"),
    ("T04", "同比", "用 dim_date 的 year 与 month 对齐，不要用自然语言算日期",
     "SELECT d.month AS 月份,\n"
     "       ROUND(SUM(CASE WHEN d.year = 2026 THEN o.pay_amount END), 2) AS 今年,\n"
     "       ROUND(SUM(CASE WHEN d.year = 2025 THEN o.pay_amount END), 2) AS 去年\n"
     "FROM fact_order o JOIN dim_date d ON o.stat_date = d.full_date\n"
     "WHERE o.status <> 'closed' AND d.month = 5 AND d.year IN (2025, 2026)\n"
     "GROUP BY d.month;"),
    ("T05", "环比", "用窗口函数对上一期取值，注意缺月要补零",
     "SELECT 月份, GMV,\n"
     "       ROUND(100.0 * (GMV - LAG(GMV) OVER (ORDER BY 月份)) / LAG(GMV) OVER (ORDER BY 月份), 2) AS 环比\n"
     "FROM (SELECT d.month AS 月份, SUM(o.pay_amount) AS GMV\n"
     "      FROM fact_order o JOIN dim_date d ON o.stat_date = d.full_date\n"
     "      WHERE o.status <> 'closed' AND d.year = 2026 GROUP BY d.month);"),
    ("T06", "去重计数", "订单数、UV、付费用户都走这个模板，DISTINCT 不能漏",
     "SELECT COUNT(DISTINCT order_id) AS 订单数 FROM fact_order WHERE status <> 'closed';"),
    ("T07", "条件聚合做交叉表", "一张表里同时出多个口径，避免跑多次查询",
     "SELECT d.month AS 月份,\n"
     "       COUNT(DISTINCT CASE WHEN o.is_refund = 1 THEN o.order_id END) AS 退款单数,\n"
     "       COUNT(DISTINCT o.order_id) AS 总单数\n"
     "FROM fact_order o JOIN dim_date d ON o.stat_date = d.full_date\n"
     "WHERE o.status <> 'closed' AND d.year = 2026 GROUP BY d.month;"),
    ("T08", "分层聚合（先明细后汇总）", "需要「人均」时先按人聚合再平均，否则被高频用户带偏",
     "SELECT ROUND(AVG(amt), 2) AS 人均消费 FROM (\n"
     "  SELECT user_id, SUM(pay_amount) AS amt FROM fact_order\n"
     "  WHERE status <> 'closed' AND stat_date BETWEEN '2026-05-01' AND '2026-05-31'\n"
     "  GROUP BY user_id);"),
    ("T09", "留存（cohort）", "首访日 + D7 是否活跃，用自连接，不要用「7 天内」近似",
     "WITH first_seen AS (\n"
     "  SELECT device_id, MIN(stat_date) AS d0 FROM fact_click GROUP BY device_id\n"
     ")\n"
     "SELECT COUNT(DISTINCT f.device_id) AS cohort,\n"
     "       COUNT(DISTINCT c.device_id) AS d7_active\n"
     "FROM first_seen f\n"
     "LEFT JOIN fact_click c ON c.device_id = f.device_id\n"
     "     AND c.stat_date = date(f.d0, '+7 day')\n"
     "WHERE f.d0 = '2026-05-01';"),
    ("T10", "明细查询", "带 LIMIT，列不要超过 8 个，否则前端渲染不下",
     "SELECT o.order_id AS 订单号, b.book_name AS 书名, o.qty AS 数量,\n"
     "       ROUND(o.pay_amount, 2) AS 实付\n"
     "FROM fact_order o JOIN dim_book b ON o.book_id = b.book_id\n"
     "WHERE o.stat_date = '2026-05-20' AND o.status <> 'closed'\n"
     "ORDER BY o.pay_amount DESC LIMIT 20;"),
    ("T11", "空值处理", "比率的分母可能为 0，用 NULLIF 而不是 CASE",
     "SELECT ROUND(100.0 * SUM(is_click) / NULLIF(COUNT(*), 0), 2) AS CTR FROM fact_click;"),
    ("T12", "中位数", "SQLite 没有 median，用窗口函数取中间位置",
     "SELECT AVG(amt) AS 中位数 FROM (\n"
     "  SELECT amt, ROW_NUMBER() OVER (ORDER BY amt) AS rn, COUNT(*) OVER () AS n\n"
     "  FROM (SELECT SUM(pay_amount) AS amt FROM fact_order WHERE status <> 'closed' GROUP BY user_id)\n"
     ") WHERE rn IN ((n + 1) / 2, (n + 2) / 2);"),
]

CASES = [
    ("电商运营团队", "把「昨天各渠道 ROI」从每周一次的人工取数变成随时可问",
     "运营有 146 个渠道，每次大促前要看投放效率。原来找数据组排队两天，现在自助提问。",
     "用 `channel_roi_rank` 指标卡 + `T02` 模板；ROI 只对 `is_paid=1` 渠道算。",
     "提问量从 0 涨到日均 180 次，数据组的需求队列清空了三分之一。"
     "代价是运营开始问「为什么」，而系统只会给数据，这个预期需要提前沟通。"),
    ("出版社编辑", "判断一本书的选题是否值得续约",
     "编辑需要看同类书的站内表现：评分、评论率、完读率。这些数据散在三个系统里。",
     "把三张事实表按 `book_id` 聚合，用 `rating_avg`、`review_rate`、`completion_rate` 三个指标卡。",
     "单个选题的评估时间从 2 小时降到 10 分钟。但编辑更想要的是「同类书平均线」，"
     "这需要预先算好分品类基准值，属后续需求。"),
    ("财务对账", "月度 GMV 与业务系统差异归因",
     "财务的 GMV 是确认收入口径（不含运费、扣退款），运营看板是含运费含退款，每月对不上。",
     "提供 `gmv_gross` 与 `gmv_net` 两个指标，另在文档里写明三个 GMV 版本的差异。",
     "对账差异从每月 6-8 万元的「不明」收敛到可解释的运费与退款两项。"
     "关键是让两边的口径写在同一份文档里，而不是各自维护一份 Excel。"),
    ("客服团队", "回答用户「我的订单为什么没退款」",
     "客服原来要查三个后台。现在想用自然语言直接查订单状态。",
     "这个需求**没有做**。原因：涉及单个用户隐私数据，与只读快照的设计冲突；"
     "且客服需要的是确定性答案，不是概率性生成。",
     "改为提供一个固定模板的订单查询页。承认能力边界比硬做更省事。"),
    ("数据新人", "入职第一周就能自己取数",
     "新人不懂星型模型，不知道 `fact_order` 和 `dim_book` 怎么 join。",
     "字段字典 + 12 个查询模板 + 术语表，三份文档覆盖 80% 的入门问题。",
     "新人首次独立取数的时间从 2 周降到 3 天。真正起作用的是「五个最容易算错的地方」那一节，"
     "它把老员工口头传授的东西写下来了。"),
]


def render_templates() -> str:
    out = ["""# 查询模板与建模规范

12 个模板覆盖了实际提问的绝大多数形态。模板的作用不是让模型照抄，
而是让它在生成时有个"形状"可参照——尤其在比率、同比、留存这三类上，
模型自己发挥的错误率明显更高。

## 一、建模的三条硬规矩

**先定粒度，再写 SQL。** 每张表的粒度都写在字段字典的第一行。
`fact_order` 是"一单一书一行"，所以任何"订单数"都必须去重。
粒度没定就写聚合，错了也不会报错，只会静默给出一个偏大的数。

**维度表和事实表分开。** 维度表放属性（书名、分类、渠道），事实表放事件（评分、点击、订单）。
合并成一张宽表看起来省事，代价是任何一个属性变更都要回刷历史事实，而且事实表会膨胀十倍。

**不要在事实表里存派生指标。** 折扣深度、客单价这类都由 SQL 现算。
存下来就会有"存的值"和"算的值"两个版本，早晚对不上。
唯一的例外是 `dim_book.rating_avg`——它是每夜重算的快照，因为逐题实时算 286 万行太慢。
这个例外在字段字典里写明了。

## 二、12 个模板
"""]
    for tid, name, when, sql in TEMPLATES:
        out.append(f"\n### {tid} {name}\n")
        out.append(f"适用：{when}。\n")
        out.append("```sql")
        out.append(sql)
        out.append("```\n")
    out.append(
        """
## 三、三个模板里的常见错法

**T03 比率类**。很多人会写成 `AVG(is_click)`，看着也对——因为 `is_click` 是 0/1。
但一旦加了 `WHERE` 过滤掉部分曝光，`AVG` 的分母就变成了"过滤后的事件数"，
和 `SUM/COUNT` 的口径不再一致。两个写法在无过滤时等价，在有过滤时分道扬镳，
这种"有时对有时错"是最难排查的。

**T05 环比**。`LAG` 在缺月时会跨过空月取值，得到的是"上一个有数据的月"而不是"上个月"。
月度数据里 2 月常有异常，跨过去看起来数值合理，实际错位一个月。
所以模板里先按月聚合、再取 LAG，中间不做过滤。

**T09 留存**。用 `date(d0, '+7 day')` 精确到天。曾经用过 `BETWEEN d0+1 AND d0+7`，
那个口径叫"7 日内活跃"，数值通常高 30% 以上，且随窗口拉长趋近 100%，
完全失去区分度。
"""
    )
    return "\n".join(out)


def render_onboard() -> str:
    return """# 接入指南

三条接入路径：HTTP、Python SDK、MCP。选哪条取决于你要做什么——
做产品集成用 HTTP，做数据分析脚本用 SDK，让 AI 客户端调用你的数据用 MCP。

## 一、五分钟跑通 HTTP

```bash
# 1. 起服务
pip install -r requirements.txt
cp .env.example .env      # 填 LLM_API_KEY
python -m uvicorn app.main:app --port 8000

# 2. 建索引（首次，约 1 元）
python run_index.py --input seed/v2 --pages 0 --tag prod

# 3. 提问
curl -X POST http://127.0.0.1:8000/v1/ask \\
  -H "Authorization: Bearer $TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{"question":"2026 年 5 月各渠道净 GMV 排名","mode":"full"}'
```

返回体里五个字段值得先认识：`answer`（自然语言结论）、`sql`（生成的查询）、
`rows`（结构化结果）、`citations`（引用来源）、`refused`（是否拒答）。
调试时加 `"debug": true`，会额外返回检索证据和判定理由。

## 二、Python SDK

```python
from documind import DocuMind

dm = DocuMind.open()                 # 读 .env，加载索引
r = dm.ask("上个月退款率最高的三个分类")
print(r.answer)
print(r.rows[:5])
for c in r.citations:
    print(c.file_path, c.score)
```

SDK 与 HTTP 共用同一套编排，区别只是没有网络层。
`dm.ask(..., guard=False)` 会关掉证据判定，用于内部评测——线上不要这么用。

## 三、MCP

MCP 是给 AI 客户端用的。起服务：

```bash
python -m app.mcp_server --transport stdio
```

三个工具：

| 工具 | 用途 | 何时被调用 |
|---|---|---|
| `ask_data` | 提一个数据问题，返回结论 + 结果表 | 用户问"多少""排名""趋势" |
| `list_tables` | 列出可查的表与字段 | 模型不确定有哪些数据时 |
| `describe_metric` | 查一个指标的口径 | 模型需要确认"GMV 含不含退款" |

工具描述是按"何时用"写的，不是按"是什么"写的。这个差别直接影响误调率：
早期写"返回数据查询结果"，模型会在需要解释概念时也调它；改成
"当用户询问具体数值或排名时使用"，误调率降了一半。

## 四、常见接入错误

**403 但 Key 是对的**。检查 `Origin` 是否在 `allowed_origins` 里（E1012）。
本地起的页面端口换了就会撞上这条。

**429 但配额还很多**。看是不是撞了网关层的 IP 限流（120 次/分钟），
这一层和租户配额是独立计数的。

**答案一直是拒答**。先确认索引真的建了——`rag_storage/<tag>/` 目录非空。
空索引不会报错，只会让检索永远返回空证据，然后判定器如实地拒答。
"""


def render_cases() -> str:
    out = ["""# 客户案例

五个真实场景，包括一个"没做"的。写"没做"的那个比写成功的四个更有用：
它说明了这个产品的能力边界在哪，以及边界是怎么判断出来的。
"""]
    for i, (who, goal, ctx, how, result) in enumerate(CASES, 1):
        out.append(f"\n## 案例 {i}：{who} —— {goal}\n")
        out.append(f"**背景**：{ctx}\n")
        out.append(f"**做法**：{how}\n")
        out.append(f"**结果**：{result}\n")
    out.append(
        """
## 一条共同规律

四个做成案例有一个共同点：问题都能用"已有的指标卡 + 分组聚合"表达。
一个没做的案例恰恰相反——它需要"查特定用户的特定订单"，那是查询，
不是分析。这个产品的定位是分析，不是查询。

判断一个需求该不该接，问三个问题就够：

1. 答案是一个数或一张小表，还是一大段文本？（要文本说明该用搜索，不是这个）
2. 口径能不能预先定义？（定义不了就没法判对错）
3. 错了的代价是"返工"还是"误导决策"？（后者需要人工复核环节）

三个都过，才值得接。
"""
    )
    return "\n".join(out)


SCENES = [
    ("S01", "月度经营复盘",
     "每月 3 号出上月复盘，回答「涨了还是跌了、为什么、下月做什么」。",
     "GMV 净额环比、订单数环比、客单价环比、各渠道贡献度、退款率变化。",
     "先拆「量」再拆「价」：GMV = 订单数 × 客单价，订单数 = 曝光 × CTR × CVR。"
     "每一步只拆一层，拆到能解释 60% 变化就停，继续拆只会得到一堆小数字。",
     ["把 GMV 环比拆成十几个因子的乘积，最后每个因子贡献 2%，谁也解释不了。",
      "用总额口径和上月净额口径对比，差额全是退款，看起来像业务下滑。",
      "忘记去年同期有促销，同比 +30% 其实只是活动位移。"]),
    ("S02", "渠道投放优化",
     "每周评估 146 个渠道的投放效率，决定下周预算怎么切。",
     "渠道 ROI、CPA、CVR、新客占比、7 日留存。",
     "ROI 必须用净额、只对 is_paid=1 的渠道算；样本少于 100 单的渠道不参与排名，"
     "因为一单退款就能让 ROI 从 3.2 跳到 1.1。",
     ["把自然流量放进 ROI 排名，它会以「零成本」霸榜。",
      "看单周 ROI 就砍渠道，忽略了转化有 7 天归因窗口。",
      "只比 ROI 不比规模，小渠道 ROI 高但拿不到量。"]),
    ("S03", "新客质量评估",
     "判断哪个渠道拉来的新客更值钱，不只看首单。",
     "new_user_cnt、首单 AOV、ltv_30d、retention_d7、repurchase_rate。",
     "必须按 cohort 看：同一天注册的一批人，观察 30 天。不要拿「本月新客的 30 日 LTV」"
     "和「上月新客的 30 日 LTV」比，后者观察期更长，天然更高。",
     ["用当月 LTV 对比历史月份，忽略窗口不完整。",
      "把首单金额当 LTV，漏掉复购。",
      "新客定义用「首次下单」而不是「首次访问」，两个口径差 40%。"]),
    ("S04", "选品与库存",
     "判断哪些书值得加印、哪些该清仓。",
     "sales_qty、库存周转、评分、评论率、完读率、分类内排名。",
     "用「分类内分位数」而不是绝对值：文学类的畅销线和技术类完全不是一回事。"
     "评分高的书不一定卖得好，两者相关性只有 0.31。",
     ["跨分类比销量，文学类永远赢。",
      "用累计销量代替近期销量，老书永远排第一。",
      "忽略赠品（book_id < 0）把销量算多了。"]),
    ("S05", "大促效果评估",
     "大促结束后算清楚「这场活动到底赚没赚」。",
     "活动期 GMV 增量、折扣深度、新客占比、活动后 7 天回落幅度。",
     "增量要减去「自然增长基线」，基线用前 4 周同期均值。"
     "只报活动期 GMV 是把存量搬到活动期的自我感动。",
     ["只报活动期 GMV，不减基线。",
      "忽略活动后回落，把透支当成增长。",
      "折扣深度按件数加权，实际应按金额加权。"]),
    ("S06", "内容质量分析",
     "找出「高分低销」和「低分高销」的书，给运营提供干预清单。",
     "rating_avg、rating_cnt、review_rate、comment_len、完读率。",
     "评分要过滤 is_valid，且要求 rating_cnt ≥ 30 才有统计意义；"
     "评论率低不一定是书不好，可能是没到读完后才引导评价。",
     ["不设最小样本量，一本 1 个 10 分的书排第一。",
      "把评论数和评分数混为一谈。",
      "把「评分低」直接等同于「质量差」，忽略了读者预期偏差。"]),
    ("S07", "用户分层运营",
     "把用户按价值分层，决定发什么券。",
     "付费频次、累计消费、最近购买间隔（RFM 三个维度）。",
     "RFM 的三个维度要各自分箱再组合，不能先加总成一个分数——"
     "加总会让「高频低额」和「低频高额」归为一类，运营动作完全不同。",
     ["把 R/F/M 加权求和成一个分，丢掉结构信息。",
      "分箱用固定阈值，忽略业务季节性。",
      "用全量历史算 R，最近购买间隔被老数据稀释。"]),
    ("S08", "退款归因",
     "退款率从 3.1% 涨到 4.4%，找出原因。",
     "refund_rate 按分类/渠道/价格带/物流时效拆解。",
     "先做「结构变化」和「同结构内变化」的分解：如果高退款品类占比上升，"
     "整体退款率会涨但每个品类都没变差。这个分解比逐维度下钻更先做。",
     ["直接下钻到最细维度，找到一堆小异常。",
      "把 refunding 和 refunded 混在一起。",
      "分母用了含 closed 的口径。"]),
    ("S09", "周报自动化",
     "把每周固定要看的 12 个数自动生成，不再手工复制。",
     "12 个固定指标 + 环比 + 同比 + 异常标注。",
     "固定指标清单要版本化。指标口径一改，历史周报全部不可比，"
     "所以每次口径变更都要在周报里加一行备注。",
     ["口径改了不备注，周报曲线出现跳变被当成业务事件。",
      "缺数据的周直接跳过，环比跨了两周。",
      "自动生成后没人看，因为异常没有标注。"]),
    ("S10", "竞品与市场",
     "想对比自家和竞品的表现。",
     "这个场景**做不到**：仓库里只有自家数据，没有竞品数据。",
     "在这种情况下，系统会拒答而不是编造。正确做法是明确说明数据边界，"
     "用行业公开报告补充，不要试图用自家数据外推行业。",
     ["用自家退款率当行业均值。",
      "把「站内评分」当成市场口碑。",
      "拿自家 DAU 增长率推测行业增速。"]),
]


def render_scenes() -> str:
    out = ["""# 分析场景手册

10 个真实场景，每个写清「要回答什么、用哪些指标、写法要点、三个结论陷阱」。
陷阱部分是重点——同一个数算对了、结论说错了，比算错更难发现。

场景 10 是个反例：它做不到，而且应该如实说做不到。
"""]

    for sid, name, bg, metrics, key, traps in SCENES:
        out.append(f"\n## {sid}　{name}\n")
        out.append(f"**背景**：{bg}\n")
        out.append(f"**用到的指标**：{metrics}\n")
        out.append(f"**写法要点**：{key}\n")
        out.append("**三个结论陷阱**：")
        for t in traps:
            out.append(f"- {t}")
        out.append("")

    out.append(
        """
## 一条贯穿所有场景的规则

**先说口径，再说结论。** 十个场景里有九个的争议不在数字本身，而在
「你这个 GMV 含不含退款」「你这个新客是首访还是首单」。
把口径写进结论的第一句，能省掉后面 80% 的来回。

这条规则在系统里是有实现的：每个指标卡都带 `caveats` 字段，
生成答案时会把相关 caveat 附在结论后面。所以答案经常看起来啰嗦——
那是刻意的，简洁的错答案不如啰嗦的对答案。
"""
    )
    return "\n".join(out)


def render_metric_tree() -> str:
    return """# 指标体系与拆解关系

指标不是一张平铺的清单，是一棵树。树的根是北极星，中间是过程指标，
叶子是可由事实表直接算出的原子指标。搞不清层级就会出现
「拿过程指标当结果指标汇报」或者「拆解到底层发现拆不动」。

## 一、北极星

本项目服务的业务把 **净 GMV** 作为北极星。选它的理由不是"金额最直观"，
而是它同时受「来的人多不多」「买不买」「买多少」「退不退」四件事影响，
任何一个环节恶化都会在它身上体现，不容易被单一动作刷高。

代价是它对单点问题不敏感：退款率涨 1 个点，净 GMV 可能只掉 0.4%。
所以北极星只能用来定方向，不能用来看问题。

## 二、三层结构

```
北极星     净 GMV
             │
结果层     订单数 × 客单价
             │        │
过程层   曝光 × CTR × CVR   品类结构 × 折扣深度
             │
原子层   fact_click.is_click / fact_order.pay_amount / dim_book.category_id ...
```

**结果层**只有两个因子。这是刻意的：拆解的第一层必须足够粗，
粗到任何人都能一眼看出"是量的问题还是价的问题"。

**过程层**是真正干活的地方。`曝光 × CTR × CVR` 这条链路上，
每个环节对应一组完全不同的运营动作：曝光对应投放，CTR 对应素材和选品，
CVR 对应详情页和价格。所以这一层的指标必须能分开看。

**原子层**是字段。它们不叫指标，叫"取数依据"。把字段当指标汇报是常见的层级混淆，
比如把 `qty` 叫"销量指标"——销量是 `SUM(qty)`，`qty` 只是它的原料。

## 三、四个拆解关系（可直接查）

| 上层 | 拆成 | 关系类型 | 注意 |
|---|---|---|---|
| 净 GMV | 订单数 × 客单价 | 乘法 | 客单价的分母必须是去重订单数 |
| 订单数 | 曝光 × CTR × CVR | 乘法 | CVR 的分母是点击不是曝光 |
| 客单价 | 件单价 × 每单件数 | 乘法 | 件单价用实付不用定价 |
| 退款率 | 退款单数 / 支付成功单数 | 除法 | 分母不含 closed |

这四个关系都能在 `25-查询模板与建模.md` 找到对应的 SQL 模板。
拆解本身不难，难的是每一步的口径要和上层对得上——
上层用净额、下层用总额，拆出来的贡献度加起来不等于总变化，
这种错误在复盘会上极难当场发现。

## 四、三个拆解错误

**错误一：乘法拆解后把贡献度直接相加。** 乘法关系的各因子贡献度不是线性可加的，
严格做法是对数分解或 Shapley 分解。实务里常用"先替换一个因子、其余保持基期"的
链式替换法，简单但依赖替换顺序。说清用了哪种方法，比用哪种更重要。

**错误二：跨层比较。** 「本月 CTR 提升 2%，但 GMV 下降 1%」不是矛盾，
因为中间还隔着 CVR 和客单价。把不同层的指标放在一句话里比较，
听众会以为你在自相矛盾。

**错误三：护栏指标缺失。** 只看北极星会做出"提高客单价但退款率翻倍"的决策。
所以每个北极星下面必须挂 2-3 个护栏指标：退款率、评分、投诉率。
护栏不参与考核，只做否决——它变差的时候，北极星的提升不算数。

## 五、指标体系怎么维护

指标树不是设计一次就完事的。三条维护规则：

1. **新增指标必须说明它挂在哪一层、父节点是谁。** 挂不上去的指标通常是"看起来有用"。
2. **删除指标要留墓碑。** 历史报表还在用，直接删会让老报表报错。
3. **口径变更走变更记录。** 这是唯一一个"改了必须留痕"的地方，
   因为口径变更的影响会持续到所有历史对比。
"""
