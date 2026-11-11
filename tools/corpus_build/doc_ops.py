"""渲染：SQL 安全、错误码、接口参考、SSE 事件、配额、变更记录、术语表。"""

from .facts_ops import (CHANGELOG, ENDPOINTS, ERROR_CODES, QUOTAS,
                        SSE_EVENTS, TERMS)

# ---------------------------------------------------------------- SQL 安全

GUARD_RULES = [
    ("G01", "只允许单条语句", "解析后 AST 里语句数量必须为 1；出现分号分隔的多条语句直接拒绝",
     "E1062", "`SELECT 1; DROP TABLE t` 这类注入在 AST 层就是两条语句，正则永远抓不全"),
    ("G02", "只允许 SELECT 及其子句", "语句类型白名单：Select / Union / With；其余一律拒绝",
     "E1063", "不靠关键词黑名单，靠 AST 节点类型判断，大小写、注释、换行都绕不过"),
    ("G03", "表名必须在表卡白名单里", "AST 中每张表都要能在 tables.yml 找到同名条目",
     "E1031", "顺带解决“模型编了一张不存在的表”这类幻觉，报错信息比执行报错更好懂"),
    ("G04", "列必须在表卡声明范围内", "每列要么在所属表的 columns 里，要么是聚合函数的别名",
     "E1032", "不允许 `SELECT *`：星号展开后可能带出未声明的敏感列"),
    ("G05", "禁止访问系统表", "表名命中 sqlite_master / sqlite_sequence / pragma_* 即拒绝",
     "E1064", "系统表能读出全库结构，等价于泄露 schema"),
    ("G06", "禁止 PRAGMA 语句", "pragma 节点单独判断，不走 SELECT 白名单",
     "E1064", "`PRAGMA writable_schema=1` 可以改 schema，是提权跳板"),
    ("G07", "禁止 ATTACH / DETACH", "ATTACH 可以把任意路径挂成库",
     "E1063", "本地文件场景下 ATTACH 等于任意文件读"),
    ("G08", "禁止危险函数", "黑名单含 load_extension / writefile / readfile / edit",
     "E1065", "SQLite 的扩展加载是原生代码执行，必须在解析层挡掉"),
    ("G09", "禁止子查询访问未声明表", "子查询里的表名同样走 G03 检查",
     "E1031", "只查最外层表名是最常见的漏检"),
    ("G10", "禁止 LIMIT 缺失的明细查询", "无聚合且无 LIMIT 的查询自动追加 LIMIT 1000",
     "E1071", "不是拒绝而是改写，因为用户意图通常是“看一眼”而非“全量导出”"),
    ("G11", "扫描行数估算上限", "EXPLAIN QUERY PLAN 估算扫描行超过 500 万即拒绝",
     "E1072", "估算比实测便宜，先 EXPLAIN 再执行是硬性顺序"),
    ("G12", "执行超时 15 秒", "sqlite3 progress_handler 每 10ms 检查累计耗时",
     "E1070", "超时中断而不是等它跑完，避免一个笛卡尔积拖垮整个进程"),
    ("G13", "连接只读", "打开时 `mode=ro` + `PRAGMA query_only=1` 双保险",
     "E1090", "即使前 12 条全被绕过，物理上还是写不进去"),
    ("G14", "敏感列脱敏", "user_phone / user_email / id_card 一律不进结果集",
     "E1061", "需要用到时只能取哈希或后四位，且要在 SQL 里显式写 substr"),
    ("G15", "禁止跨租户表前缀", "表名出现 `tenant_` 前缀且不是当前租户即拒绝",
     "E1061", "多租户场景下的越权读取，靠表名前缀隔离"),
    ("G16", "结果列数上限 50", "列数超过 50 的查询被截断并提示",
     "E1071", "列太多通常意味着模型在“把所有字段都选出来”而不是回答问题"),
    ("G17", "保留字冲突校验", "别名与 SQL 保留字冲突时自动加引号",
     None, "不拒绝，改写。中文别名不会冲突，英文别名会"),
    ("G18", "空结果不等于拒答", "执行成功但 0 行，如实返回空表并提示可能原因",
     None, "这条是产品规则不是安全规则，但它决定了评测里“该拒答”和“该报空”的边界"),
]

HEADER_GUARD = """# SQL 安全与执行沙箱

生成式 SQL 的风险不在"模型会不会写错"，而在"模型写对了一句你不想让它执行的语句"。
本项目把安全边界放在**解析层**而不是提示词层：提示词可以要求"只写 SELECT"，
但要求不是约束；`sqlglot` 解析出来的 AST 才是约束。

一条 SQL 的执行链路是固定的六步，任何一步失败都终止，不回退：

```
生成 → 解析(AST) → 18 条规则校验 → EXPLAIN 估算 → 只读连接执行 → 结果截断
         ↑失败 E1060      ↑失败 E1061~E1065      ↑超限 E1072      ↑超时 E1070
```

下面先讲 18 条规则，再讲沙箱本身，最后给几个真实的绕过尝试和它们为什么失败。
"""


def render_guard() -> str:
    out = [HEADER_GUARD, "\n## 一、18 条规则\n"]
    out.append("| 编号 | 规则 | 做法 | 拒绝码 |")
    out.append("|---|---|---|---|")
    for rid, name, how, code, _why in GUARD_RULES:
        out.append(f"| {rid} | {name} | {how} | {code or '—'} |")
    out.append("\n下面是逐条展开，重点写“为什么这么做”，因为规则本身一眼就能看懂，"
               "值钱的是当初为什么选这个做法而不是更简单的那个。\n")
    for rid, name, how, code, why in GUARD_RULES:
        out.append(f"### {rid} {name}\n")
        out.append(f"做法：{how}。")
        out.append(f"理由：{why}。")
        if code:
            out.append(f"拒绝码：`{code}`。")
        out.append("")

    out.append(
        """
## 二、沙箱本身

数据库以只读方式打开，三层保护叠加，任何一层单独失效都不至于出事：

1. **URI 模式**：`file:warehouse.sqlite?mode=ro`，SQLite 在文件层拒绝写。
2. **会话级**：连上后立刻 `PRAGMA query_only = 1`，即使拿到写语句也执行不了。
3. **语句级**：G01~G08 在解析层就不放行。

超时用 `progress_handler` 实现，每 10ms 回调一次，累计超过 15 秒返回非零值中断查询。
这个机制的好处是不需要另起线程去 kill，坏处是回调本身的频率会影响长查询性能——
10ms 是在 2 万行/秒的扫描速度下调出来的，再密会明显掉速。

并发用信号量控制，默认 8 个。这个数字不是拍脑袋：压测时 8 并发下 p99 是 1.4s，
16 并发就劣化到 4.1s，因为 SQLite 的读锁在写快照重建时会短暂升级。
"""
    )
    return "\n".join(out)


# ---------------------------------------------------------------- 错误码


def render_errors() -> str:
    out = ["""# 错误码与排障对照

错误码设计原则：**同一类根因给同一个码**，不按"哪个模块抛的"编号。
所以 `E1061` 既可能是"引用了敏感列"，也可能是"跨租户访问"，
因为对上游来说都是"这条 SQL 不该被执行"。

码段划分：

| 段 | 范围 | 含义 |
|---|---|---|
| E10xx | 1001-1003 | 请求参数问题 |
| E10xx | 1010-1012 | 权限与租户 |
| E10xx | 1020-1032 | 语义层配置与校验 |
| E10xx | 1040-1042 | 限流与配额 |
| E10xx | 1050-1052 | 模型调用 |
| E10xx | 1060-1065 | SQL 解析与安全 |
| E10xx | 1070-1072 | 执行资源 |
| E10xx | 1080-1081 | 拒答（业务正常返回） |
| E10xx | 1090-1091 | 服务端状态 |
"""]
    out.append("\n## 全量对照表\n")
    out.append("| 码 | HTTP | 含义 | 触发条件 | 处理建议 |")
    out.append("|---|---|---|---|---|")
    for code, http, name, trigger, fix in ERROR_CODES:
        out.append(f"| {code} | {http} | {name} | {trigger} | {fix} |")

    out.append(
        """
## 三个容易误判的码

### E1080 看起来像错误，其实是正常返回

HTTP 状态是 200，`refused=true`。它表示"检索到的证据不足以回答"，
是产品的正确行为而不是故障。前端不要把它当异常弹红框，
排行榜统计里它也不进错误率分母——它进的是"拒答率"。

### E1052 上下文超长，先怀疑 top_k 而不是模型

出现 E1052 时，第一反应往往是"模型上下文不够"。实际上 32k 的窗口，
一张表卡约 400 token，6 张卡加问题也就 3k，真正把上下文撑爆的
是 few-shot 范例被整段拼进去。排查顺序：先看 `top_k`，
再看 fewshots 的 `tags` 是否命中过多。

### E1072 扫描行数超限，不是结果集超限

两者经常被混。结果集超限是 `E1071`（返回行数 > 10000），
扫描行数超限是 `E1072`（执行前估算要扫 500 万行以上）。
前者加 LIMIT 能解决，后者加 LIMIT 没用——因为过滤条件本身没走索引。
"""
    )
    return "\n".join(out)


# ---------------------------------------------------------------- 接口


def render_api() -> str:
    out = ["""# HTTP 与 SSE 接口参考

Base URL 本地默认 `http://127.0.0.1:8000`，所有 `/v1/*` 需要
`Authorization: Bearer <token>`。错误响应统一为：

```json
{"error": {"code": "E1061", "message": "SQL 被安全策略拒绝", "detail": "...", "trace_id": "..."}}
```

`trace_id` 在任何响应里都有，报障时先给这个。
"""]
    out.append("\n## 一、端点清单\n")
    out.append("| 方法 | 路径 | 用途 | 鉴权 |")
    out.append("|---|---|---|---|")
    for e in ENDPOINTS:
        out.append(f"| {e['method']} | `{e['path']}` | {e['purpose']} | {e['auth']} |")

    out.append("\n## 二、逐个端点\n")
    for e in ENDPOINTS:
        out.append(f"### `{e['method']} {e['path']}`\n")
        out.append(f"{e['purpose']}。鉴权：{e['auth']}。")
        if e["params"]:
            out.append("\n参数：")
            for p in e["params"]:
                out.append(f"- {p}")
        out.append(f"\n返回：`{e['returns']}`")
        out.append(f"\n说明：{e['notes']}。\n")

    out.append("\n## 三、SSE 事件序列\n")
    out.append(
        "流式接口的事件顺序是固定的，客户端按 `event:` 字段分发即可。"
        "除 `stage` 之外每种事件在一次请求中最多出现一次（`token` 会多次）。\n"
    )
    out.append("| 事件 | 载荷字段 | 何时发 |")
    out.append("|---|---|---|")
    for name, payload, when in SSE_EVENTS:
        out.append(f"| `{name}` | {payload} | {when} |")

    out.append(
        """
典型的成功序列（full 模式）：

```
event: meta      data: {"trace_id":"t_8f3a","mode":"full"}
event: stage     data: {"name":"retrieve","status":"start"}
event: stage     data: {"name":"retrieve","status":"end","ms":412}
event: stage     data: {"name":"sqlcraft","status":"start"}
event: stage     data: {"name":"sqlcraft","status":"end","ms":1830}
event: token     data: {"text":"2026"}
event: token     data: {"text":" 年 5 月"}
...
event: table     data: {"columns":["渠道","净GMV"],"rows":[["字节",182340.5]]}
event: citations data: {"items":[{"chunk_id":"c_012","file_path":"11-指标字典.md","snippet":"roi 只用净额..."}]}
event: done      data: {"cost_cny":0.0256,"tokens_in":8140,"tokens_out":612,"elapsed_ms":9120}
```

两个实现细节值得说：

**保活**。反向代理常见的 60s 空闲超时会掐断长连接，所以服务端每 15 秒发一条
注释行 `: ping`。它不是事件，客户端 `EventSource` 会自动忽略，但连接不断。

**断流不等于失败**。如果客户端在 `done` 之前断开，服务端记 `client_abort`
而不是 `error`。这两者在 trace 里必须分开，否则错误率会被用户关页面刷高。
"""
    )
    return "\n".join(out)


# ---------------------------------------------------------------- 配额


def render_quota() -> str:
    out = ["""# 限流与配额

限流分三层，从外到内依次收窄。三层独立计数，任一命中即返回 429。

| 层 | 维度 | 默认值 | 命中码 |
|---|---|---|---|
| 网关 | 来源 IP | 120 次/分钟 | E1040 |
| 租户 | 租户 QPS | 见下表 | E1040 |
| 租户 | 日 token | 见下表 | E1041 |
| 服务 | 全局并发查询 | 8 | E1042 |

## 套餐配额

| 套餐 | QPS | 日 token 上限 | 并发 | 备注 |
|---|---|---|---|---|
"""]
    for name, qps, tokens, conc, note in QUOTAS:
        out.append(f"| {name} | {qps} | {tokens:,} | {conc} | {note} |")

    out.append(
        """
## 计费口径

token 按"输入 + 输出"分别计价，输入单价约为输出的四分之一。
一次 `full` 模式的问答典型消耗是输入 8000、输出 600 token，
换成人民币约 0.026 元。同一个问题用 `naive` 模式只要 0.004 元，
十倍差距来自检索链路的多次模型调用，不是模型更贵。

配额按自然日 0 点重置，**不做跨日结转**。月中提额不会回溯补发前半月额度。

## 退避策略

客户端收到 E1040 应按 `1s, 2s, 4s, 8s` 退避，最多重试 4 次；
收到 E1041 不要重试，等到次日 0 点；收到 E1042 建议串行化请求，
因为并发闸门是全局的，重试只会加剧排队。

一个容易被忽略的点：E1042 的响应头里带 `Retry-After`，
但它的单位是毫秒不是秒，这是历史遗留，为了兼容已有客户端没有改。
"""
    )
    return "\n".join(out)


# ---------------------------------------------------------------- 变更


def render_changelog() -> str:
    out = ["""# 版本变更记录

只记"对使用者有影响"的变更：接口、口径、默认值、配额。内部重构不进这个文件。
"""]
    for ver, date, items in reversed(CHANGELOG):
        out.append(f"\n## v{ver}（{date}）\n")
        for it in items:
            out.append(f"- {it}")
    out.append(
        """
## 三次口径变更的说明

这个产品的指标定义改过三次，每次都会让历史报表出现跳变，所以单独记一笔。

**2025-12-16，订单数改为去重。** 在此之前 `order_cnt` 用的是行数，
所有历史客单价被低估 28%。修正后客单价整体上移，运营侧的月度目标没有调整，
导致当月完成率看起来突然变好——这是一次典型的"指标修正被误读为业务增长"。

**2026-03-10，退款率分母改为不含 closed。** 之前把未支付关闭单也算进分母，
退款率被系统性低估约 4 个百分点。修正后退款率普遍上浮，
客服团队按旧口径定的考核线需要重新校准。

**2026-05-19，GMV 默认口径从含运费改为…仍然是含运费。** 这一条留在记录里
是因为讨论过程本身有价值：财务要求不含运费、运营要求含运费，最后决定
`gmv_gross` 保持含运费（与对外披露一致），另加 `gmv_net` 扣退款但仍含运费。
没有引入第三个字段，是因为三个 GMV 已经让人分不清了。
"""
    )
    return "\n".join(out)


# ---------------------------------------------------------------- 术语


def render_terms() -> str:
    out = ["""# 术语表

按"容易混"排序，不按字母序。每条给出中文、英文、定义、以及最容易和什么混。
"""]
    out.append("| 术语 | 英文 | 定义 | 易混于 |")
    out.append("|---|---|---|---|")
    for zh, en, definition, confusable in TERMS:
        out.append(f"| {zh} | {en} | {definition} | {confusable} |")
    out.append(
        """
## 三个被用坏的词

**"准确率"**。在这个产品里没有单一准确率。结果集等价（EX）是一个数，
拒答正确率是另一个数，SQL 语法可执行率是第三个数。说"准确率 90%"
必须带上口径，否则无法比较。本项目的 90.0% 特指"30 道可答题中结果集完全一致的比例"，
分母不含 10 道拒答题。

**"幻觉"**。在 Text-to-SQL 语境下，"幻觉"至少有三种：编了不存在的表、
编了不存在的列、算对了但口径错了。前两种能被 G03/G04 挡住，
第三种挡不住，只能靠金标题暴露。说"降低了幻觉"时要说清是哪一种。

**"实时"**。埋点延迟最长 6 小时，所以"实时看板"在本项目里指 T+1 的日粒度，
不是秒级。任何写着"实时"的地方，实际含义都是"当日数据次日可见"。
"""
    )
    return "\n".join(out)
