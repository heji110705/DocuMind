"""渲染：仓库字段字典 + 指标字典。

这两个文档是语料里信息密度最高的部分——几乎每句话都能出一道具名题，
也是「模型必须读到具体字段才答得对」这类题的唯一来源。
"""

from .facts_ops import METRICS
from .facts_schema import TABLES

HEADER_FIELD = """# 数据仓库字段字典

本文是 `warehouse.sqlite` 的字段级说明，覆盖 10 张表、84 个字段。
写法约定：每个字段给出**物理定义**（类型/单位）、**业务含义**、**取数口径**、
**已知误用**、**同义词**五项。取数口径写的是"从哪来、算进了什么、排除了什么"，
不是"它大概表示什么"——后者是文档里最容易糊弄、也最容易让下游算错的地方。

阅读顺序建议：先看每张表开头的「粒度」和「口径坑」，再看字段。
粒度错了，后面每个字段都白看。
"""


def render_fields() -> str:
    out = [HEADER_FIELD]
    for i, t in enumerate(TABLES, 1):
        out.append(f"\n## {i}. `{t['name']}`\n")
        out.append(f"- **粒度**：{t['grain']}")
        out.append(f"- **规模**：{t['row_count']}")
        out.append(f"- **定位**：{t['meaning']}")
        out.append("")
        if t["caveats"]:
            out.append("**口径坑（先读这一段）**")
            for c in t["caveats"]:
                out.append(f"- {c}")
            out.append("")
        out.append("### 字段\n")
        for c in t["columns"]:
            unit = f"，单位 {c['unit']}" if c["unit"] else ""
            syn = "、".join(c["syn"]) if c["syn"] else "无"
            out.append(f"#### `{c['name']}`\n")
            out.append(f"- 类型：`{c['type']}`{unit}")
            out.append(f"- 含义：{c['meaning']}")
            out.append(f"- 口径：{c['calc']}")
            out.append(f"- 误用：{c['pitfall']}")
            out.append(f"- 同义词：{syn}")
            out.append("")
        if t["joins"]:
            out.append("**关联关系**\n")
            for j in t["joins"]:
                out.append(f"- `{j}`")
            out.append("")
    out.append(
        """
## 附：五个最容易算错的地方

把上面 84 个字段里真正会让人返工的点单独列一遍，按"踩坑频率"排序：

1. **订单数没去重**。`fact_order` 是一单一书一行，`COUNT(*)` 得到的是订单行数。
   实测差 28%，对应 `E1021` 那次线上事故。
2. **评分没过滤无效记录**。`is_valid=0` 的行是刷分、作者自评和重复提交，
   不加过滤会让均分虚高约 3.2%，且高分书被系统性放大。
3. **UV 用 user_id 去重**。31% 的事件没有登录态，`user_id` 为 NULL 被聚合函数直接丢掉，
   算出来的 DAU 会比真实值低三成。
4. **拿定价当成交价**。`list_price` 与 `pay_amount` 之间夹着券、活动价和会员价，
   直接用定价乘数量做 GMV，系统性高估 18%~25%。
5. **ISO 周跨年**。12 月 31 日可能属于次年第 1 周，按 `year` + `iso_week` 分组
   会切出一个只有 2 天的"第 53 周"，同比曲线在这种地方必然出现尖刺。

这五条没有一条是"数据库写错了"，全部是口径没对齐。文档能做的只有把口径写明，
剩下的只能靠评测集把错误打出来——所以每一类都至少配了一道金标题。
"""
    )
    return "\n".join(out)


HEADER_METRIC = """# 指标字典与口径

本文件声明 32 个指标，是 `metrics.yml` 的人读版本。每条给出别名、定义、公式、单位、
口径注意事项。别名不是装饰：模型把「卖了多少本」映射到 `sales_qty` 靠的就是这个字段，
同义词典覆盖不到的别名会直接表现为"选错指标"。

关于公式里的一个约定：**公式里出现的指标名是引用，不是重复定义**。
比如 `aov` 的分母写 `order_cnt`，实际计算时展开成 `COUNT(DISTINCT order_id)`。
这样改一处去重逻辑，所有引用它的指标一起生效。
"""


def render_metrics() -> str:
    out = [HEADER_METRIC]
    for i, m in enumerate(METRICS, 1):
        out.append(f"\n## {i}. {m['name']}（`{m['key']}`）\n")
        out.append(f"- 别名：{'、'.join(m['aliases'])}")
        out.append(f"- 定义：{m['definition']}")
        out.append(f"- 公式：`{m['formula']}`")
        out.append(f"- 单位：{m['unit']}")
        out.append(f"- 依赖表：{', '.join('`' + t + '`' for t in m['tables'])}")
        out.append("- 口径注意：")
        for c in m["caveats"]:
            out.append(f"  - {c}")
        out.append("")

    out.append(
        """
## 指标之间的四组常见混淆

指标字典最容易失效的地方不是"没定义"，而是"定义了两个看起来一样的东西"。
下面四组是客服工单里重复出现最多的，背下来比背公式有用。

### 第一组：GMV 的三个版本

`gmv_gross` 含退款、`gmv_net` 扣已退款、财务口径的"确认收入"还要再扣未发货部分。
三者单调递减，差值就是退款额。对外汇报一律说清用的是哪一个；只说"GMV"时默认
`gmv_gross`，因为运营侧的看板默认展示它。

### 第二组：ARPU 与 ARPPU

分母差一个"是否付费"。`arpu` 的分母是全部活跃用户，`arppu` 只算付过钱的。
前者用来判断整体变现效率，后者用来判断付费用户的消费能力。把 ARPU 和 ARPPU
放在一张图里比较没有意义——它们的量级差异来自分母，不来自业务变化。

### 第三组：订单数与订单行数

`order_cnt` 去重，`order_line_cnt` 不去重。差的那部分就是"一单多书"。
客单价必须用 `order_cnt` 做分母，用行数做分母会得到"单本书均价"，
名字听起来差不多，数值差 28%。

### 第四组：留存与复购

留存看的是"还来不来"，复购看的是"还买不买"。同一批用户，7 日留存可能 40%，
30 日复购只有 12%。这两个数不矛盾，也不能互相替代。写结论时如果只有其中一个，
要说明另一个没测，而不是拿它俩互相解释。
"""
    )
    return "\n".join(out)
