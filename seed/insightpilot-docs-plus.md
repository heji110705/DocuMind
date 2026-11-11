# 02-架构与取舍.md

# 架构与设计取舍

## 一、分层：为什么切在"纯函数"这条线上

```
接入层  FastAPI / SSE / 静态页 · MCP Server（stdio JSON-RPC）
   │  只负责协议转换与事件推送，不含业务判断
编排层  状态图引擎（节点 / 条件边 / 检查点 / max_steps）
   │  只负责"下一步跑谁"，不认识任何业务字段
Agent 层  6 个纯函数节点 (State, Context) -> Patch
   │  只负责"这一步做什么"，不写状态、不选下一步
能力层  语义检索 / SQL 生成 / 安全执行 / 统计 / 自检
   │  每项能力一个模块，可单独替换与单测
数据层  星型模型仓库 + 语义层配置（表卡 / 指标 / 范例 / 输出约定）
```

**这条线的价值**：如果路由逻辑写在 Agent 里（`if error: goto sqlcraft`），
那么"重试 3 次"这个策略就和"生成 SQL"这个能力粘死了，既不能单测也不能换策略。
把路由外移到图定义后，`full` / `no_critic` / `single` 三种策略只是三份装配代码，
Agent 一行不改。

**代价**：状态要显式建模成一个 Pydantic 对象，每次进出都要校验，比 dict 啰嗦。
但换来的是"任何一次运行的中间状态都能 dump 出来复现"，这对调试和评测是刚需。

---

## 二、10 项选型：为什么不用 X

| 决策 | 选了 | 为什么 | 没选什么，为什么 |
|---|---|---|---|
| 编排 | **自研状态图（≈270 行）** | 要能讲清状态合并/条件边/检查点的实现；零版本漂移；节点是纯函数，换框架只换装配 | 不用 LangGraph：版本 API 变动频繁，且"能调 API"不等于"懂编排"。若要换，只需重写 `build_graph()` 的装配 |
| 语义检索 | **手写 BM25 + 别名词典** | 卡片不足 200 条，BM25 够用且**每条召回都有 `hit_reason` 可解释** | 不用向量库：要下模型、召回不可解释、演示环境可能装不上；`Retriever` 协议留了口子 |
| SQL 安全 | **sqlglot 解析 AST 后白名单判定** | 正则必被绕过（注释/大小写/函数嵌套/Unicode 空白）；AST 是唯一可靠方式 | 不用正则黑名单；不用 `sqlparse`（只分词，不做语义校验） |
| 数据库 | **SQLite 只读快照** | 单文件、零运维、可进仓库、`mode=ro` + `query_only` 双保险；万级数据完全够 | 不用 MySQL/PG：不该让看 demo 的人先装数据库。`Repo` 协议已隔离，换库只改一个类 |
| 数据建模 | **星型模型（dim/fact 分离）** | 分析型 schema 让字段歧义大幅下降；同时展示"面向分析的重建模"能力 | 不直接查源库 40+ 字段的 `books_book`：模型极易选错列，且列名不含业务含义 |
| LLM 接入 | **OpenAI 兼容协议 + 双 Inferencer** | 换厂商只改环境变量；无 Key 时规则基线顶上，全链路仍可跑 | 不绑定单一厂商 SDK；不做"仅云端可用"的设计 |
| **输出约定** | **`semantic/defaults.yml` + 提示词渲染** | 行数 / 小数位 / 最小样本量全系统一致，**且能被测试断言**；改口径只改一处 | 不写死在提示词常量里：改一次要全文搜，还无法断言"模型真的收到了这个数" |
| 观测 | **自建 trace 落 SQLite** | 不依赖外部服务，断网可用；能讲清 span 埋点原理 | 不用 LangSmith：要 Key、要联网、黑盒。留了 exporter 口子但不假装实现 |
| 图表 | **服务端 SVG（前端手绘）** | 零 CDN 依赖，离线/内网/断网演示都不白屏 | 不用 ECharts CDN：一次网络抖动就是演示事故 |
| **Agent 接入** | **手写 MCP（JSON-RPC 2.0 over stdio）** | 协议只有四个方法，自己实现约 200 行；零新增依赖；stdout 纯净性可被子进程测试守住 | 不引官方 SDK：多一条版本漂移的依赖，而本项目卖点之一是"pip install 完就能跑" |

---

## 三、三个"不做"的决定

### 1. 不做 Agent 之间的自由对话

六边形多 Agent 互相对话看起来很酷，但在这个任务上是负收益：
每多一次 Agent 间自然语言传递，就多一次信息损耗和一次不可控的 token 成本。
本项目所有 Agent 之间传的是**结构化对象**（`Intent` / `SQLContext` / `SQLDraft` / `ResultSet` / `Insight` / `Verdict`），
只有"生成"环节用自然语言。

判断标准很简单：**能用一个 schema 表达清楚的东西，就不要用自然语言传。**

### 2. 不追求"什么都能答"

超出数据范围的问题走 `clarify` 分支，明确说"这个我答不了"。
让模型硬答的结果是：它会给你一个语法正确、语义荒谬的 SQL，然后你还得自己发现。
**拒答比错答便宜。**

### 3. 不让模型算数

统计、趋势、占比、极值全部由 `stats.py` 用 Python 算，模型只写措辞。
模型写的每个数字都要回查 `allowed_numbers(result)`，查不到就丢弃它的表述、退回规则版本。
这不是不信任模型，是**让错误有边界**：措辞错了只是措辞错，数字错了整份报告都作废。

---

## 四、性能与容量

| 指标 | 实测（规则模式） | 说明 |
|---|---|---|
| 端到端 p50 | 140 ms | 主要开销在语义检索与 SQL 执行 |
| p95 | 219 ms | 受首次查询冷启动影响 |
| 单次检索 token | ≈1.2k | 预算 2600，裁剪策略先保表卡片 |
| 仓库体积 | 2.1 MB | 万级事实表，SQLite 单文件 |

**如果数据量放大 100 倍**，会先崩的是：
1. `DimIndex` 把维度值全量读进内存（现在最大 400 个值）→ 需要改为按需查 + 前缀索引
2. SQLite 单文件并发写（现在只读，不是问题）→ 换 DuckDB（分析型）或 Postgres
3. 全表 `GROUP BY` 的聚合查询 → 需要预聚合宽表或物化视图

这三处都在 `Repo` 与 `Retriever` 的接口后面，改动不会扩散到 Agent 层。

---

## 五、可测试性是怎么设计的

| 层 | 测试方式 | 用例数 |
|---|---|---|
| 编排引擎 | 玩具图：线性 / 分支 / 重试环 / 自环 / 异常 / 检查点 | 16 |
| SQL 守卫 | 攻击样例逐条断言（多语句、子查询夹带、系统表、UNION） | 54 |
| 只读仓库 | 真库只读执行、超时、截断、数值列推断 | 17 |
| 语义检索 | 分词正确性、别名命中、二跳、预算裁剪 | 34 |
| Agent 层 | 统计口径、中文数字、指标选择、回退路径、图变体 | 31 |
| API | TestClient 打通 8 个路由含 SSE | 20 |
| 评测 | 结果集等价的各种边界（序/列名/容差/None） | 23 |
| 数据 ETL | 行数对账、日期格式、幂等 | 13 |

**关键设计**：`RunContext` 把依赖全塞进一个 dataclass，
所以节点测试只需要两个假对象，不需要 patch 模块全局变量。
`FakeProvider` 十行就能构造，LLM 路径因此可以在无网络下测。


---

# 03-评测报告.md

# InsightPilot 评测报告（结果集等价）

> 两个判定指标并列：**EX** 要求列数与金标准完全相同（列名不比较、行序不比较、数值 1e-6 相对容差、字符串去首尾空白且大小写敏感、None 与空串不等）；**覆盖一致**允许预测多带列，只要金标准每一列都能在预测里按值找到对应列、行不错位。
> 所有指标由 `scripts/run_eval.py` 实跑写入 `docs/eval_results.json` 后渲染，没有手填值；个别来自直接 SQL 抽检的数字已在文中就地标注。

> **先看重复性再读表**：同一配置重复运行的 EX 极差在本项目实测为 3–4 个百分点（最近一次两轮为 0–3.4pp，见 `docs/03b-重复性检验.md`）。本表是**单次运行**结果，任何小于该量级的组间差异都不应被解释为模块效果。

## 1. 结论

4 组配置在 60 题上的 EX 分别为 full 50.0%、no_critic 50.0%、no_schema 46.7%、single 45.0%；最高 50.0%（full/no_critic），最低 45.0%（single），极差 5.0 个百分点。

full 比 no_schema 高 3.3 个百分点（50.0% vs 46.7%），差异对应 SchemaLinker 是否按相关性挑指标。no_critic 与 full 的差距为 +0.0 个百分点，`attempts>1` 的题数为 0 次。

本次为真实模型模式（`llm` / api），数字可代表该模型在该提示词下的表现，但不可跨模型版本或提示词版本直接比较；局限性见第 7 节。

## 2. 消融对比

| 配置 | EX | 覆盖一致 | 首答正确率 | 平均尝试 | p50 | p95 |
|---|---|---|---|---|---|---|
| full（完整 6 Agent） | 50.0%（30/60） | 56.7%（34/60） | 50.0%（30/60） | 0.98 | 5748 ms | 6657 ms |
| no_critic（关闭 Critic） | 50.0%（30/60） | 55.0%（33/60） | 50.0%（30/60） | 0.98 | 5391 ms | 6243 ms |
| no_schema（去掉相关性排序） | 46.7%（28/60） | 60.0%（36/60） | 46.7%（28/60） | 0.98 | 5616 ms | 6861 ms |
| single（单链路、无重试、无 Critic） | 45.0%（27/60） | 58.3%（35/60） | 45.0%（27/60） | 0.98 | 5768 ms | 6631 ms |

> **EX** 要求列数与金标准完全相等；**覆盖一致**允许预测多带列，只要金标准的每一列都能在预测里按值找到对应列、行也不错位。两者之差就是「答对了但投影多给了列」的比例。本项目的目标是「答案包含正确信息」，所以覆盖一致更接近真实可用性；EX 作为严格参考保留。
> p95 为最近秩百分位；样本不足 20 时退化为最大值（本次 n=60，不涉及）。延迟是端到端图执行耗时，含 SQL 执行，不含评测脚本读写 JSON 的开销。
> 平均尝试按**全题数**计算：被 intake 判为 out_of_scope 走 clarify 的题没有进入 executor，attempts 记 0，因此均值会略低于 1.00。

## 3. 成本与调用量

| 配置 | 模型调用次数 | 输入 token | 输出 token | 单题 token | 单题成本 | 单题模型耗时 |
|---|---|---|---|---|---|---|
| full（完整 6 Agent） | 178 | 270,393 | 21,637 | 4867 | ¥0.00523 | 5594 ms |
| no_critic（关闭 Critic） | 178 | 270,363 | 21,690 | 4868 | ¥0.00523 | 5294 ms |
| no_schema（去掉相关性排序） | 181 | 294,476 | 21,972 | 5274 | ¥0.00564 | 5523 ms |
| single（单链路、无重试、无 Critic） | 177 | 292,632 | 21,618 | 5238 | ¥0.00560 | 5544 ms |

> 本轮全部配置合计花费约 **¥1.302**。单价取 DeepSeek-chat 公开价（输入 ¥1 / 百万 token，输出 ¥2 / 百万 token），可用 `IP_PRICE_IN_PER_M`、`IP_PRICE_OUT_PER_M` 覆盖。这个数字用于**横向比较配置之间**，不作账单依据。
> 调用次数是真实 HTTP 请求数，不含回退到模板的那些——回退时没有花 token，所以「单题成本 × 题数」会略低于真实账单。

## 4. 分类型 EX

| 配置 | T1 单表聚合 | T2 分组排序 | T3 多表关联 | T4 时间窗 | T5 比率 | T6 口径陷阱 | 合计 |
|---|---|---|---|---|---|---|---|
| full | 90.0%（9/10） | 70.0%（7/10） | 60.0%（6/10） | 50.0%（5/10） | 20.0%（2/10） | 10.0%（1/10） | 50.0%（30/60） |
| no_critic | 90.0%（9/10） | 70.0%（7/10） | 60.0%（6/10） | 50.0%（5/10） | 20.0%（2/10） | 10.0%（1/10） | 50.0%（30/60） |
| no_schema | 90.0%（9/10） | 60.0%（6/10） | 50.0%（5/10） | 50.0%（5/10） | 20.0%（2/10） | 10.0%（1/10） | 46.7%（28/60） |
| single | 90.0%（9/10） | 60.0%（6/10） | 50.0%（5/10） | 40.0%（4/10） | 20.0%（2/10） | 10.0%（1/10） | 45.0%（27/60） |

> 题型定义：T1 单表聚合、T2 分组排序、T3 多表关联、T4 时间窗、T5 比率、T6 口径陷阱；每类 10 题。

## 5. 失败分析

### 5.1 失败原因分布

| 配置 | failed | rejected | clarify | wrong | EX=False 合计 |
|---|---|---|---|---|---|
| full | 0 | 1 | 1 | 28 | 30 |
| no_critic | 0 | 0 | 1 | 29 | 30 |
| no_schema | 0 | 0 | 1 | 31 | 32 |
| single | 0 | 0 | 1 | 32 | 33 |

> `wrong` = SQL 执行成功（status=ok）但结果集不等价；它是最值得看的一类，说明问题出在 SQL 结构/口径而不是执行链路。

### 5.2 逐题失败清单

<details><summary>full：30/60 题失败</summary>

| 题号 | 类型 | status | 原因 |
|---|---|---|---|
| Q07 | T1 | ok | 取值/口径不符（第 1 行第 1 列：651 vs 236） |
| Q16 | T2 | ok | 投影列数不符（列数不同：2 vs 3） |
| Q18 | T2 | ok | 投影列数不符（列数不同：3 vs 2） |
| Q20 | T2 | ok | 取值/口径不符（第 1 行第 2 列：794 vs 794.27） |
| Q23 | T3 | ok | 投影列数不符（列数不同：3 vs 6）；缺 GROUP BY、缺 JOIN |
| Q25 | T3 | ok | 行数/分组粒度不符（行数不同：10 vs 50） |
| Q26 | T3 | ok | 取值/口径不符（第 1 行第 3 列：21.9 vs 21.92） |
| Q30 | T3 | ok | 投影列数不符（列数不同：4 vs 5） |
| Q32 | T4 | ok | 行数/分组粒度不符（行数不同：3 vs 2） |
| Q34 | T4 | ok | 投影列数不符（列数不同：2 vs 3） |
| Q38 | T4 | ok | 投影列数不符（列数不同：2 vs 3）；缺 GROUP BY |
| Q39 | T4 | ok | 投影列数不符（列数不同：2 vs 1）；缺 GROUP BY |
| Q40 | T4 | ok | 投影列数不符（列数不同：3 vs 2）；缺 JOIN |
| Q41 | T5 | ok | 投影列数不符（列数不同：3 vs 4） |
| Q42 | T5 | ok | 投影列数不符（列数不同：1 vs 3） |
| Q45 | T5 | rejected | 自检拒绝：结论未通过自检：查询返回 0 行，任何结论都不成立；应如实说明无数据，而不是解释成零 |
| Q46 | T5 | ok | 取值/口径不符（第 1 行第 4 列：0.81 vs 81.25） |
| Q47 | T5 | ok | 取值/口径不符（第 1 行第 1 列：63 vs 25.2） |
| Q48 | T5 | ok | 取值/口径不符（第 1 行第 1 列：174 vs 69.6） |
| Q49 | T5 | ok | 投影列数不符（列数不同：4 vs 5） |
| Q50 | T5 | ok | 投影列数不符（列数不同：3 vs 1） |
| Q51 | T6 | ok | 投影列数不符（列数不同：3 vs 2） |
| Q53 | T6 | ok | 投影列数不符（列数不同：4 vs 5） |
| Q54 | T6 | ok | 投影列数不符（列数不同：4 vs 3） |
| Q55 | T6 | ok | 投影列数不符（列数不同：4 vs 2） |
| Q56 | T6 | ok | 取值/口径不符（第 1 行第 4 列：0.2 vs 14） |
| Q57 | T6 | ok | 投影列数不符（列数不同：5 vs 4）；缺 GROUP BY、缺 JOIN |
| Q58 | T6 | ok | 取值/口径不符（第 1 行第 3 列：199 vs 351） |
| Q59 | T6 | ok | 投影列数不符（列数不同：4 vs 7） |
| Q60 | T6 | clarify | 意图被判为澄清/超范围，未进入 SQL 生成 |

</details>

<details><summary>no_critic：30/60 题失败</summary>

| 题号 | 类型 | status | 原因 |
|---|---|---|---|
| Q07 | T1 | ok | 取值/口径不符（第 1 行第 1 列：651 vs 236） |
| Q16 | T2 | ok | 投影列数不符（列数不同：2 vs 3） |
| Q18 | T2 | ok | 投影列数不符（列数不同：3 vs 2） |
| Q20 | T2 | ok | 取值/口径不符（第 1 行第 2 列：794 vs 794.27） |
| Q23 | T3 | ok | 投影列数不符（列数不同：3 vs 4）；缺 GROUP BY、缺 JOIN |
| Q25 | T3 | ok | 行数/分组粒度不符（行数不同：10 vs 50） |
| Q26 | T3 | ok | 取值/口径不符（第 1 行第 3 列：21.9 vs 21.92） |
| Q30 | T3 | ok | 投影列数不符（列数不同：4 vs 5） |
| Q32 | T4 | ok | 行数/分组粒度不符（行数不同：3 vs 2） |
| Q34 | T4 | ok | 投影列数不符（列数不同：2 vs 3） |
| Q38 | T4 | ok | 投影列数不符（列数不同：2 vs 3）；缺 GROUP BY |
| Q39 | T4 | ok | 投影列数不符（列数不同：2 vs 4） |
| Q40 | T4 | ok | 投影列数不符（列数不同：3 vs 2）；缺 JOIN |
| Q41 | T5 | ok | 投影列数不符（列数不同：3 vs 4） |
| Q42 | T5 | ok | 投影列数不符（列数不同：1 vs 3） |
| Q45 | T5 | ok | 投影列数不符（列数不同：3 vs 5） |
| Q46 | T5 | ok | 投影列数不符（列数不同：4 vs 6） |
| Q47 | T5 | ok | 取值/口径不符（第 1 行第 1 列：63 vs 26.4） |
| Q48 | T5 | ok | 取值/口径不符（第 1 行第 1 列：174 vs 69.2） |
| Q49 | T5 | ok | 投影列数不符（列数不同：4 vs 5） |
| Q50 | T5 | ok | 投影列数不符（列数不同：3 vs 1） |
| Q51 | T6 | ok | 投影列数不符（列数不同：3 vs 2） |
| Q53 | T6 | ok | 投影列数不符（列数不同：4 vs 5） |
| Q54 | T6 | ok | 取值/口径不符（第 1 行第 2 列：12 vs 9.01） |
| Q55 | T6 | ok | 投影列数不符（列数不同：4 vs 2） |
| Q56 | T6 | ok | 取值/口径不符（第 1 行第 4 列：0.2 vs 14） |
| Q57 | T6 | ok | 投影列数不符（列数不同：5 vs 4）；缺 GROUP BY、缺 JOIN |
| Q58 | T6 | ok | 投影列数不符（列数不同：4 vs 3） |
| Q59 | T6 | ok | 投影列数不符（列数不同：4 vs 6） |
| Q60 | T6 | clarify | 意图被判为澄清/超范围，未进入 SQL 生成 |

</details>

<details><summary>no_schema：32/60 题失败</summary>

| 题号 | 类型 | status | 原因 |
|---|---|---|---|
| Q07 | T1 | ok | 取值/口径不符（第 1 行第 1 列：651 vs 236） |
| Q13 | T2 | ok | 取值/口径不符（第 1 行第 2 列：100.2 vs 5） |
| Q16 | T2 | ok | 投影列数不符（列数不同：2 vs 3） |
| Q18 | T2 | ok | 投影列数不符（列数不同：3 vs 2） |
| Q20 | T2 | ok | 取值/口径不符（第 1 行第 2 列：794 vs 794.27） |
| Q23 | T3 | ok | 投影列数不符（列数不同：3 vs 4） |
| Q25 | T3 | ok | 行数/分组粒度不符（行数不同：10 vs 50） |
| Q26 | T3 | ok | 行数/分组粒度不符（行数不同：10 vs 14）；缺 JOIN |
| Q29 | T3 | ok | 投影列数不符（列数不同：3 vs 2） |
| Q30 | T3 | ok | 投影列数不符（列数不同：4 vs 5） |
| Q32 | T4 | ok | 行数/分组粒度不符（行数不同：3 vs 2） |
| Q34 | T4 | ok | 行数/分组粒度不符（行数不同：5 vs 10） |
| Q38 | T4 | ok | 投影列数不符（列数不同：2 vs 4） |
| Q39 | T4 | ok | 投影列数不符（列数不同：2 vs 4） |
| Q40 | T4 | ok | 投影列数不符（列数不同：3 vs 2） |
| Q41 | T5 | ok | 投影列数不符（列数不同：3 vs 4） |
| Q42 | T5 | ok | 投影列数不符（列数不同：1 vs 3） |
| Q44 | T5 | ok | 取值/口径不符（第 1 行第 1 列：5001 vs 124.19） |
| Q45 | T5 | ok | 投影列数不符（列数不同：3 vs 5） |
| Q47 | T5 | ok | 取值/口径不符（第 1 行第 1 列：63 vs 25.2） |
| Q48 | T5 | ok | 取值/口径不符（第 1 行第 1 列：174 vs 69.2） |
| Q49 | T5 | ok | 取值/口径不符（第 1 行第 4 列：0.657 vs 65.67） |
| Q50 | T5 | ok | 投影列数不符（列数不同：3 vs 1） |
| Q51 | T6 | ok | 投影列数不符（列数不同：3 vs 2） |
| Q53 | T6 | ok | 投影列数不符（列数不同：4 vs 7） |
| Q54 | T6 | ok | 投影列数不符（列数不同：4 vs 3） |
| Q55 | T6 | ok | 投影列数不符（列数不同：4 vs 2） |
| Q56 | T6 | ok | 投影列数不符（列数不同：5 vs 4） |
| Q57 | T6 | ok | 投影列数不符（列数不同：5 vs 4）；缺 GROUP BY、缺 JOIN |
| Q58 | T6 | ok | 投影列数不符（列数不同：4 vs 3） |
| Q59 | T6 | ok | 投影列数不符（列数不同：4 vs 10） |
| Q60 | T6 | clarify | 意图被判为澄清/超范围，未进入 SQL 生成 |

</details>

<details><summary>single：33/60 题失败</summary>

| 题号 | 类型 | status | 原因 |
|---|---|---|---|
| Q07 | T1 | ok | 取值/口径不符（第 1 行第 1 列：651 vs 236） |
| Q13 | T2 | ok | 取值/口径不符（第 1 行第 2 列：100.2 vs 5） |
| Q16 | T2 | ok | 投影列数不符（列数不同：2 vs 3） |
| Q18 | T2 | ok | 投影列数不符（列数不同：3 vs 2） |
| Q20 | T2 | ok | 取值/口径不符（第 1 行第 2 列：794 vs 794.27） |
| Q23 | T3 | ok | 投影列数不符（列数不同：3 vs 4） |
| Q25 | T3 | ok | 行数/分组粒度不符（行数不同：10 vs 50） |
| Q26 | T3 | ok | 行数/分组粒度不符（行数不同：10 vs 14） |
| Q29 | T3 | ok | 投影列数不符（列数不同：3 vs 2） |
| Q30 | T3 | ok | 投影列数不符（列数不同：4 vs 5） |
| Q32 | T4 | ok | 行数/分组粒度不符（行数不同：3 vs 2） |
| Q34 | T4 | ok | 行数/分组粒度不符（行数不同：5 vs 10） |
| Q36 | T4 | ok | 取值/口径不符（第 1 行第 1 列：2026-41 vs 2026-W41） |
| Q38 | T4 | ok | 投影列数不符（列数不同：2 vs 4） |
| Q39 | T4 | ok | 投影列数不符（列数不同：2 vs 4） |
| Q40 | T4 | ok | 投影列数不符（列数不同：3 vs 2） |
| Q41 | T5 | ok | 投影列数不符（列数不同：3 vs 4） |
| Q42 | T5 | ok | 投影列数不符（列数不同：1 vs 3） |
| Q44 | T5 | ok | 取值/口径不符（第 1 行第 1 列：5001 vs 124.19） |
| Q45 | T5 | ok | 投影列数不符（列数不同：3 vs 5） |
| Q47 | T5 | ok | 取值/口径不符（第 1 行第 1 列：63 vs 25.2） |
| Q48 | T5 | ok | 取值/口径不符（第 1 行第 1 列：174 vs 69.2） |
| Q49 | T5 | ok | 取值/口径不符（第 1 行第 4 列：0.657 vs 65.67） |
| Q50 | T5 | ok | 投影列数不符（列数不同：3 vs 1） |
| Q51 | T6 | ok | 投影列数不符（列数不同：3 vs 2） |
| Q53 | T6 | ok | 投影列数不符（列数不同：4 vs 5） |
| Q54 | T6 | ok | 投影列数不符（列数不同：4 vs 3） |
| Q55 | T6 | ok | 投影列数不符（列数不同：4 vs 2） |
| Q56 | T6 | ok | 投影列数不符（列数不同：5 vs 4） |
| Q57 | T6 | ok | 投影列数不符（列数不同：5 vs 4）；缺 GROUP BY、缺 JOIN |
| Q58 | T6 | ok | 投影列数不符（列数不同：4 vs 2） |
| Q59 | T6 | ok | 投影列数不符（列数不同：4 vs 6） |
| Q60 | T6 | clarify | 意图被判为澄清/超范围，未进入 SQL 生成 |

</details>

## 6. 低分排查记录

模型模式下的失败构成（按 `compare` 给出的差异类型归类，每条可回查）：

| 差异类型 | 题数 | 常见含义 |
|---|---|---|
| 列数不同 | 20 | 输出列数不一致，多为模型多给了未被问到的描述列 |
| 单元格不等 | 8 | 行列数都对但取值不同（口径或聚合层级差异） |
| 行数不同 | 2 | 过滤条件或 TOP-N 阈值与预期不一致 |

> 分类只是把差异摊开，不等于根因。同一类里可能同时存在「多给了一列」和「少给了关键列」，必须回看第 5 节的逐题明细。

本次所有配置的 EX 均不低于 0.3 的防呆线，未触发低分专项排查；评测框架的自检项（`cache_hits`、`attempts_gt1`、失败分布）仍记录在 JSON 与上表中。

## 7. 局限性声明

- 本报告为 api 模式结果，但模型版本、温度、提示词变化都会改变数字，跨版本不可直接比较。
- **数据快照极窄**：仓库只有 2026-10-18 与 2026-11-01 两天（且都是周日），T4 时间类题目没有「上个月/按周趋势」的真实跨度，区分度有限；任何关于时间分析的结论都不具备统计意义。
- **样本量小**：60 题、每题权重 1.67%，百分比的 95% 置信区间约 ±12 个百分点；两组相差 1–2 题（1.7–3.3 个百分点）不能说明模块优劣。
- **结果集等价 ≠ 语义正确**：列名与行序不参与比较，同一数字的不同口径（如收藏次数 vs 收藏人数）可能都被判为「等价」；评测只能证明「算出了同一张表」，不能证明「选对了业务口径」。

## 8. 运行元信息与复现命令

| 项 | 值 |
|---|---|
| 生成时间 | 2026-11-03 02:26 |
| 评测集 | `data/gold.jsonl`（60 题） |
| 模式 | api |
| Inferencer | llm |
| 配置数 | 4（full, no_critic, no_schema, single） |
| p95 口径 | nearest-rank |
| 缓存命中 | 0（应为 0） |

```bash
python scripts/run_eval.py --variants <见各配置> --mode auto --out docs/
```


---

# 03b-重复性检验.md

# 重复性检验与消融可信度

> 数据来源：2 个运行目录，共 8 次配置运行；模式 api。全部数字由本脚本从各目录的 `eval_results.json` 汇总，无手填。

## 1. 结论

同一配置重复运行的 EX 极差最大为 **3.3 个百分点**（43.3% ~ 46.7%，配置 `no_schema`）。

四组配置的平均 EX 落在 45.0% ~ 50.8% 之间，极差 5.8 个百分点。

组间极差大于重复波动，差异**具备进一步验证的价值**，但 n 仍然很小（每配置 2 次），不能作为显著性结论；如需下判断应扩到每配置 ≥5 次。

## 2. 逐配置明细

| 配置 | 运行次数 | EX 各次 | 均值 | 极差 | 首答正确均值 | p50 均值 |
|---|---|---|---|---|---|---|
| full（6 Agent 全开） | 2 | 50.0% / 50.0% | 50.0% | 0.0pp | 50.0% | 5535 ms |
| no_critic（去掉自检） | 2 | 51.7% / 50.0% | 50.8% | 1.7pp | 50.8% | 5485 ms |
| no_schema（去掉语义层检索） | 2 | 43.3% / 46.7% | 45.0% | 3.3pp | 45.0% | 5593 ms |
| single（单 Agent，无检索/重试/自检） | 2 | 45.0% / 45.0% | 45.0% | 0.0pp | 45.0% | 5620 ms |

> 极差单位 pp = 百分点。波动带 **±1.7pp**（取各配置极差的半宽，样本量小时这是粗略上界而非置信区间）。

## 3. 分类型 EX（各次均值）

| 配置 | T1 | T2 | T3 | T4 | T5 | T6 |
|---|---|---|---|---|---|---|
| full（6 Agent 全开） | 90.0% | 70.0% | 60.0% | 50.0% | 20.0% | 10.0% |
| no_critic（去掉自检） | 90.0% | 70.0% | 60.0% | 50.0% | 25.0% | 10.0% |
| no_schema（去掉语义层检索） | 90.0% | 55.0% | 50.0% | 45.0% | 20.0% | 10.0% |
| single（单 Agent，无检索/重试/自检） | 90.0% | 55.0% | 50.0% | 45.0% | 20.0% | 10.0% |

## 4. 逐题稳定性

同一题的判定在多次运行间是否翻转，是判断单次评测可信度的最直接证据。下表只统计出现翻转的题（同配置内部比较，不含跨配置）：

| 配置 | 题号 | 各次判定 |
|---|---|---|
| no_critic | Q46 | 对 / 错 |
| no_schema | Q17 | 错 / 对 |
| no_schema | Q36 | 错 / 对 |
| single | Q17 | 错 / 对 |
| single | Q36 | 对 / 错 |

共 5 个「配置×题目」出现判定翻转；这些题在单次运行里被算作正确或错误都带有偶然性，做单题分析时应避开。

## 5. 方法学结论（可写进简历/面试）

1. `temperature=0` 不等于可复现：以 `full` 为例，重复运行间 SQL 文本完全一致的题数是 38/60（63%），模型服务端的批处理与路由都会引入噪声。
2. 60 题样本下，单配置 EX 波动约 ±1.7pp，因此**任何小于该量级的消融差距都不值得解释**，更不该写成因果。
3. 若要主张模块有效，正确做法是每配置多次运行取均值并做区间估计，或把评测集扩到数百题——本报告选择把噪声如实暴露，而不是挑一次好看的数字。

复现：`D:/Python312/python.exe scripts/repeatability.py --runs _work\v5a _work\v5b`


---

# 06-评测集审计.md

# 评测集审计（v1 → v2）

> 一句话结论：60 道题里有 **8 道题的问法与金标准自相矛盾**（题面问"哪一本"，金标准给 5 行），
> 另有 **1 处小数位与本项目输出约定不一致**。这两类问题让 EX 系统性偏低，且偏低的
> 部分与模型能力无关。修正后原始版本保留在 `data/gold.v1.jsonl`，可随时对照。

---

## 1. 为什么要审这一遍

第一轮评测跑完，35 个失败里有 26 个是「列数不同」。按这个数字，最直觉的结论是
「模型老爱多给列，加一条投影约束就能收掉」。

但逐题看 SQL 之后发现两件事：

1. **26 个"列数不同"里只有 4 个是纯投影问题**——其余 22 个虽然列数也不对，但取值
   或行数同样不对，把列数改对也不会变正确。用列覆盖指标（见 `03-评测报告.md`）
   离线重算验证了这一点：EX 41.7%、覆盖一致 48.3%，差距只有 4 题。
2. **另有约 8 道题，模型的行为比金标准更符合题面**。这些题不看不知道，一看就发现
   是考卷本身写歪了。

第 2 点是这次审计的直接动因：**用一份自相矛盾的考卷去调提示词，只会把模型往错的方向调。**

---

## 2. 审计用的三条判据

审计不是"看着不顺眼就改"。每条修改必须命中下面三条之一，否则不动。

| 判据 | 含义 | 例子 |
|---|---|---|
| **A. 题面与金标准矛盾** | 题面的数量词、单复数、时间范围，与金标准返回的形状冲突 | 问"哪一本书"（单数），金标准 `LIMIT 5` |
| **B. 违背已声明的规格** | 金标准违反了 `data/semantic/defaults.yml` 里写死的输出约定 | 约定连续量保留 2 位，金标准写了 1 位 |
| **C. 题面缺失必要参数** | 金标准依赖了题面没给的阈值（如"书太少的社"具体是多少本） | —（本次无符合条件的修改） |

**不属于以上三条的，一律不改**，哪怕它让模型扣分。典型的是"金标准多带了一列
分子/分母"——那是正常的分析习惯，不该为了迁就模型而删掉。

---

## 3. 修改清单

### 3.1 题面修正（8 条，判据 A）

这 8 道题的共同点：题面用"哪一本 / 哪一个 / 谁"（单数），金标准却返回 5 或 10 行。
两者不可能同时成立——模型按单数答 1 行是**正确**的，按金标准答 5 行也是"正确"的，
取决于你信哪边。这种题没有唯一答案，EX 判定它本身就无意义。

修法是**只改题面，不动任何 SQL**：把数量要求写进问题里，让题面与金标准重新一致。

| 编号 | v1 题面 | v2 题面 | 金标准行数 |
|---|---|---|---|
| Q29 | 哪位作者的书被讨论得最多？ | 哪些作者的书被讨论得最多？**给我前十位** | 10 |
| Q51 | 最受欢迎的是哪本书？ | 最受欢迎的是哪几本书？**给我前五名** | 5 |
| Q52 | 评分最高的是哪本书？ | 评分最高的是哪几本书？**给我前五名** | 5 |
| Q53 | 最贵的书是哪本？值不值这个价？ | 最贵的几本书是哪几本？**给我前五本**，顺便说说值不值这个价 | 5 |
| Q54 | 哪个分类的口碑最好？ | 哪些分类的口碑最好？**给我前五名** | 5 |
| Q55 | 大家最爱讨论哪个分类？ | 大家最爱讨论哪些分类？**给我前五名** | 5 |
| Q56 | 哪本书争议最大？ | 哪几本书争议最大？**给我前五本** | 5 |
| Q58 | 哪家出版社的书最受欢迎？ | 哪些出版社的书最受欢迎？**给我前十名** | 10 |

原始问法保留在每题的 `question_v1` 字段里，`data/gold.v1.jsonl` 是完整快照。

**这类修改会不会让题目变简单？**
会——它把"猜数量"这一层不确定性去掉了。所以报告里同时给出 v1 / v2 的分数对照
（见 `03-评测报告.md` 第 7 节），读者可以自己判断提升里有多少来自题目澄清。
诚实的说法是：**这 8 题原本就测不出能力，修正后测的是"听懂数量要求"，而不是更强的能力。**

### 3.2 小数位修正（1 条，判据 B）

| 编号 | v1 | v2 | 依据 |
|---|---|---|---|
| Q09 | `ROUND(AVG(rating_count), 1)` | `ROUND(AVG(rating_count), 2)` | `defaults.yml` 规定连续量保留 2 位 |

注意这条是**规格先行、考卷对齐规格**，不是"照模型的答案改考卷"。顺序很重要：
先有 `defaults.yml` 里的"连续量 2 位"这条规则，再发现金标准只写了 1 位，于是改金标准。
如果反过来——先看模型答了什么，再把金标准改成一样——那就是作弊。

---

## 4. 看清了但**不改**的问题

审计的价值一半在"改了什么"，一半在"知道什么不该改"。以下三类问题保留原样。

### 4.1 金标准带"展示性列"

例：Q47「精装书占多少比例？」，金标准返回 3 列（精装数、总数、占比），
模型返回 1 列（占比）。按"回答这个问题需要什么"，1 列就够；按"分析师会怎么展示"，
3 列更好核对。

**为什么不改**：两种都对。删掉分子分母属于降低信息量，不是修正错误。
这类差异交给「覆盖一致」指标吸收——覆盖率会把"1 列"判错，"3 列"判对，
而覆盖率本来就允许这一层自由度。

### 4.2 明确题目的列集本就没有唯一解

T2 / T3 有一批"分析型"问题（如「哪些书评分挺高、但评论区里骂声不少？」），
合理答案的列集有五六种写法。用列数严格相等的 EX 去判这类题，本身就不合适。

**为什么不改**：改成"哪几列算对"需要逐题拍板，成本高且主观。更诚实的做法是
**承认 EX 在开放式分析题上不适用**，在报告里按题型分开看，而不是硬凑一个总数。

### 4.3 阈值类题目的"拍脑袋参数"

例：Q45「只看看书多的社」，金标准用 `HAVING COUNT(*) >= 100`，模型用 `>= 10`。
100 这个数题面里没有，模型不可能猜中。

**为什么不改**：这类题应该改写题面把阈值说清楚，或者由语义层给出默认阈值。
本次没做，是因为它牵动 `defaults.yml` 里 `min_group_size` 的取值，需要和
"分组排名默认排除小样本"这条规则一起重新设计，属于下一轮的事。

### 4.4 计量单位歧义：Q07

> 题面：「有多少本书是被人加进书架、标记成在读的？」
> 金标准：`SELECT COUNT(*) ...` → 651（**记录数**）
> 模型：`SELECT COUNT(DISTINCT book_id) ...` → 236（**书数**）

题面字面问"多少本书"，模型按书去重更贴题；但 `fact_shelf` 的粒度是"一行一条收藏记录"，
按记录数计也说得通，两种理解都成立。

**为什么不改**：修法有两个方向（把题面改成"多少条记录"，或把金标准改成去重计数），
任一方向都正确，但都会开启"逐题重写题面"的口子。本次只修**数量词与返回行数自相矛盾**
那一类（判据 A，8 条），计量单位这类歧义单独记录、不修改，在失败分析里作为已知项列出。

这条边界很重要：**审计的目的是修正"不可能同时成立"的矛盾，不是把每道题都调到模型答对为止。**
后者叫拟合，不叫审计。

---

## 5. 对分数的实际影响

| 版本 | 题面 | 金标准 | EX（两轮） |
|---|---|---|---|
| v1 | 原始 60 题 | `gold.v1.jsonl` | 41.7% / 45.0% |
| v2 | 修正 8 题数量词 | `gold.jsonl` + Q09 小数位 | 见 `03-评测报告.md` |

**读法提醒**：v1 → v2 的提升里，有一部分是"题目变清楚了"，不是"系统变强了"。
把这两件事混在一起讲，就是自欺。报告里把两个版本的分数并列，读者自行判断。

---

## 6. 复现

```bash
# 用审计前的原始评测集跑一遍（对照用）
python scripts/run_eval.py --variants full --mode auto --gold data/gold.v1.jsonl --out _work/v1

# 用当前评测集跑
python scripts/run_eval.py --variants full --mode auto --out docs/
```

`--gold` 是审计时补的参数。不加它，这份文档里"v1 与 v2 对照"的说法就无法验证——
凡是写进文档的对照，必须有一条能跑的命令。


---

# 01-实现契约.md

# 实现契约 v1（并行开发基线）

> 本文件是四个并行开发 Agent 的唯一事实来源。任何签名以本文件为准，
> 不允许"我觉得这样更好"就改接口。要改先改本文件并通知集成方。

## 0. 环境

- 解释器：`D:/Python312/python.exe`（Python 3.12）
- 已装：pydantic 2.13、fastapi、uvicorn、httpx、sqlglot 30、pandas、numpy、jinja2、matplotlib、pytest、pyyaml
- **禁止**引入未安装的库（不要写 `import langgraph`、`import chromadb`、`import sqlalchemy`）。
- 编码：所有文件 UTF-8，中文注释可用。路径一律用 `pathlib.Path`。
- 禁止硬编码绝对路径；需要路径时从 `app.core.config.SETTINGS` 取。

## 1. 目录所有权（谁写哪里，不许越界）

| 目录 | 负责人 | 内容 |
|---|---|---|
| `app/core/*` | 集成方 | config / models / protocols / errors（只读，别人别改） |
| `app/harness/*` | Agent B | 状态图引擎、RunContext、检查点 |
| `app/obs/*` | Agent B | trace 记录与存储 |
| `app/db/*` | Agent C | SQL 守卫、只读仓库、异常 |
| `app/semantic/*` | Agent D | 语义层加载、BM25 检索 |
| `scripts/etl.py`、`data/*.yml`、`data/gold.jsonl` | Agent A | 数据与语义层内容 |
| `app/llm/*`、`app/agents/*` | 集成方 | Provider、Inferencer、6 个 Agent |
| `app/api/*`、`web/*` | Agent E（后续） | FastAPI 路由、SSE、前端 |
| `app/eval/*` | Agent F（后续） | 评测与消融 |
| `tests/*` | 各人写自己模块的测试，文件名前缀区分 | `test_harness_*.py`、`test_db_*.py`、`test_sem_*.py` |

## 2. 必须实现的签名

### 2.1 Agent B —— `app/harness/graph.py`

```python
from dataclasses import dataclass, field
from typing import Any, Callable
from app.core.models import TurnState

@dataclass
class NodeResult:
    patch: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"                       # ok | retry | reject | error
    meta: dict[str, Any] = field(default_factory=dict)

NodeFn = Callable[[TurnState, "RunContext"], NodeResult | dict[str, Any]]

@dataclass
class RunReport:
    path: list[str] = field(default_factory=list)      # 实际走过的节点序列（含重复）
    status: str = "ok"                                  # ok | error | max_steps
    steps: int = 0
    elapsed_ms: int = 0
    error: str = ""
    edge_taken: list[str] = field(default_factory=list) # 每次分支选中的 key

class Graph:
    def __init__(self, name: str) -> None: ...
    def node(self, name: str, fn: NodeFn) -> "Graph": ...
    def edge(self, src: str, dst: str) -> "Graph": ...
    def branch(self, src: str, router: Callable[[TurnState, NodeResult], str],
               mapping: dict[str, str]) -> "Graph": ...
    def entry(self, name: str) -> "Graph": ...
    def terminal(self, *names: str) -> "Graph": ...
    def validate(self) -> None: ...
    def run(self, state: TurnState, ctx: "RunContext") -> tuple[TurnState, RunReport]: ...
```

规则：

1. `run` 用循环实现，`max_steps` 取自 `ctx.settings.max_steps`；超限返回 `status="max_steps"`，不抛异常。
2. 节点返回 `dict` 时自动包成 `NodeResult(patch=...)`。
3. **每个节点执行前后必须开一个 trace span**（调 `ctx.trace.span(name)`），并把
   `status / meta / 耗时 / patch 的字段名列表` 记进去。
4. **每步结束写检查点**（`ctx.checkpoint.save(turn_id, seq, state)`，可空实现）。
5. 节点抛异常时：span 记 error，`RunReport.status="error"`，**不吞异常也不崩进程**，
   返回当前 state 并让调用方决定（API 层会把它变成 500 或降级答复）。
6. `validate()` 检查：entry 已设置；所有 edge/branch 的目标节点存在；至少一个 terminal。
7. `branch` 的 router 返回的 key 若不在 mapping 里，抛 `KeyError` 并写进 RunReport.error。

### 2.2 Agent B —— `app/harness/context.py`

```python
@dataclass
class RunContext:
    settings: Settings
    provider: LLMProvider | None
    retriever: Retriever
    repo: Repo
    trace: Trace
    inferencer: Inferencer | None = None
    checkpoint: Checkpoint | None = None
    cache: SemanticCache | None = None
    extras: dict[str, Any] = field(default_factory=dict)
```

`Checkpoint` 与 `SemanticCache` 在 `app/harness/checkpoint.py` 中给出**可用的最小实现**
（SQLite 表 `checkpoints(turn_id, seq, created_at, state_json)`），允许 `None` 时跳过。

### 2.3 Agent B —— `app/obs/trace.py`

```python
class TraceStore:
    def __init__(self, path: Path) -> None: ...     # 自建表，幂等
    def save_span(self, turn_id: str, seq: int, node: str, status: str,
                  ms: int, meta: dict, error: str = "") -> None: ...
    def save_turn(self, turn_id: str, session_id: str, question: str, mode: str,
                  status: str, elapsed_ms: int, payload_json: str) -> None: ...
    def list_turns(self, limit: int = 50) -> list[dict]: ...     # 按时间倒序
    def get_turn(self, turn_id: str) -> dict: ...                # {"turn": {...}, "spans": [...]}
    def stats(self) -> dict: ...                                 # 总量/成功率/平均耗时/平均步数

class TraceRecorder:                                  # 实现 core.protocols.Trace
    def __init__(self, store: TraceStore, state: TurnState) -> None: ...
    def span(self, node: str): ...                    # contextmanager，yield 一个可写 meta 的句柄
    def annotate(self, **fields) -> None: ...         # 写到当前 span 的 meta
    def finish(self, status: str, payload: dict | None = None) -> None: ...
```

表结构（必须与 API 层一致）：

```sql
CREATE TABLE IF NOT EXISTS turns(
  turn_id TEXT PRIMARY KEY, session_id TEXT, question TEXT, mode TEXT,
  status TEXT, elapsed_ms INTEGER, created_at TEXT, payload_json TEXT);
CREATE TABLE IF NOT EXISTS spans(
  turn_id TEXT, seq INTEGER, node TEXT, status TEXT, ms INTEGER,
  meta_json TEXT, error TEXT, PRIMARY KEY(turn_id, seq));
```

### 2.4 Agent C —— `app/db/guard.py`

```python
class GuardResult(BaseModel):
    ok: bool
    sql: str            # 通过时为规范化 + 已补 LIMIT 的 SQL
    reason: str = ""    # 拒绝原因，面向人可读
    tables: list[str] = []

def validate_sql(sql: str, allowed_tables: set[str], row_limit: int) -> GuardResult: ...
```

必须拦住的（每一项都要有单测）：

1. 多条语句（`a; b`）。
2. 任何非 SELECT 根节点：INSERT / UPDATE / DELETE / CREATE / DROP / ALTER / TRUNCATE / MERGE。
3. `ATTACH`、`DETACH`、`PRAGMA`、`VACUUM`、`REINDEX`、`ANALYZE`。
4. 访问 `sqlite_master` / `sqlite_schema` / `temp.` 前缀。
5. 表名不在 `allowed_tables` 内（解析 AST 取所有 Table 节点，含 CTE 别名豁免）。
6. 无 LIMIT 时**外包一层** `SELECT * FROM (<sql>) LIMIT {row_limit}`；已有 LIMIT 且大于上限时替换为上限。
7. 空 SQL、只有注释、解析失败。

实现要点：`sqlglot.parse(sql, read="sqlite")`；用 `expression.find_all(...)` 遍历；
CTE 名字要收集进"允许的临时表名"集合，否则会把 `WITH t AS (...) SELECT * FROM t` 误杀。

### 2.5 Agent C —— `app/db/repo.py`

```python
class QueryFailed(Exception):
    def __init__(self, message: str, stage: str = "execute", hint: str = "") -> None: ...

class WarehouseRepo:                                   # 实现 core.protocols.Repo
    def __init__(self, path: Path, row_limit: int, timeout_ms: int) -> None: ...
    def table_names(self) -> set[str]: ...
    def schema_digest(self) -> str: ...                # md5(所有表名+列名)，用于缓存键
    def run_query(self, sql: str) -> ResultSet: ...     # 已含 guard，再执行
    def explain(self, sql: str) -> str: ...
```

执行要求：

- 连接串用只读 URI：`file:{path}?mode=ro`，`uri=True`。
- 连接后执行 `PRAGMA query_only=ON`（双保险）。
- 超时用 `conn.set_progress_handler(fn, 2000)`，`fn` 在超过 deadline 时返回非 0 中断。
- 取数用 `cursor.fetchmany(row_limit + 1)`；多出来一行说明被截断，置 `truncated=True` 并丢弃。
- 推断 `numeric_cols`：该列所有非空值都是 int/float 才算。
- 任何 `sqlite3.Error` 转成 `QueryFailed(stage="execute", hint=...)`，hint 要给可操作提示
  （例如 "no such column" → "检查字段名是否在 context.tables 里"）。

### 2.6 Agent D —— `app/semantic/loader.py` + `retriever.py`

```python
@dataclass
class SemanticBundle:
    tables: list[TableCard]
    metrics: list[MetricCard]
    fewshots: list[FewShot]
    synonyms: dict[str, list[str]]
    join_hints: list[str]

def load_semantic(directory: Path) -> SemanticBundle: ...

class SemanticRetriever:                               # 实现 core.protocols.Retriever
    def __init__(self, bundle: SemanticBundle) -> None: ...
    def search(self, question: str, *, budget: int,
               hints: list[str] | None = None) -> SQLContext: ...
    def all_tables(self) -> list[TableCard]: ...
    def metric(self, key: str) -> MetricCard | None: ...
    def metrics(self) -> list[MetricCard]: ...
```

检索要求（自己实现 BM25，~80 行，不引第三方）：

1. 分词：中文按 2-gram + 英文/数字按词；统一小写；去停用词表（自建 30 个左右）。
2. 三路召回：表卡片（对 `name + meaning + 所有列文本`）、指标（`name + aliases + definition`）、
   范例（`question`）。
3. 别名扩展：命中 `synonyms` 的 key 时把 value 里的词加进 query。
4. 结构加权：表名/指标名**精确出现**在问题里 → score × 1.5；同义词命中 → × 1.2。
5. **二跳扩展**：Top1 表的所有 `joins` 对手表强制加入（分数记 0.01），保证 JOIN 路径可用。
6. 预算裁剪：按 `len(text) * 0.6` 估 token，累计超过 `budget` 就丢弃低分项，记录 `dropped` 数。
   裁剪顺序：先保表卡片，再保指标，最后保范例（范例至少留 2 条）。
7. 返回的每个卡片必须填 `hit_reason`（如 `"name_exact"` / `"alias:评分人数"` / `"bm25:0.83"` / `"join:fact_rating"`），
   这是 trace 可读性的关键，也是面试能讲"为什么召回这张卡"的依据。

### 2.7 集成方（我）—— `app/llm/*`、`app/agents/*`

```python
class OpenAICompatProvider:      # complete(system, user, json_mode) -> LLMResult
class TemplateInferencer:        # 规则 + metric.sql_template，无网络
class LLMInferencer:             # 调 provider，解析 JSON
def build_inferencer(settings, bundle, provider) -> Inferencer
```

`metric.sql_template` 的槽位约定（Agent A 写模板、我这边填充）：

| 槽位 | 含义 | 为空时替换成 |
|---|---|---|
| `{where}` | `WHERE ...` 条件（含时间窗、分类过滤） | 空串 |
| `{group}` | `GROUP BY ...` | 空串 |
| `{having}` | `HAVING ...` | 空串 |
| `{order}` | `ORDER BY ...` | 空串 |
| `{limit}` | `LIMIT n` | 空串 |

模板示例：`SELECT category AS 分类, ROUND(AVG(rating_avg),2) AS 平均评分 FROM dim_book {where} {group} {order} {limit}`

## 3. 数据契约（Agent A 产出，全组依赖）

### 3.1 `data/warehouse.sqlite`
严格按 `data/schema.sql`。

### 3.2 `data/semantic/tables.yml`

```yaml
version: 1
tables:
  - name: dim_book
    grain: 一行一本书
    meaning: 书籍维度表，含书名、作者、出版社、分类、价格与站内评分表现
    columns:
      - {name: book_id, type: INTEGER, meaning: 书籍唯一标识, synonyms: [书, 图书]}
      - {name: rating_avg, type: REAL, meaning: 站内平均评分 0-10, synonyms: [评分, 打分]}
    caveats:
      - rating_avg 与 douban_rating 不同源，不可混用
    joins:
      - fact_rating.book_id = dim_book.book_id
```

### 3.3 `data/semantic/metrics.yml`

```yaml
version: 1
metrics:
  - key: book_cnt
    name: 书籍数
    aliases: [多少本书, 图书数量, 书目数]
    definition: dim_book 的行数，一本一条
    formula: COUNT(dim_book.book_id)
    unit: 本
    tables: [dim_book]
    caveats: [含无评分的书]
    sql_template: "SELECT COUNT(*) AS 书籍数 FROM dim_book {where}"
```

### 3.4 `data/semantic/fewshots.yml`

```yaml
version: 1
examples:
  - question: 一共有多少本书
    sql: SELECT COUNT(*) AS 书籍数 FROM dim_book
    note: 计数类，直接用行数
    tags: [计数, 单表]
```

### 3.5 `data/semantic/synonyms.yml`

```yaml
version: 1
synonyms:
  分类: [类型, 题材, 品类]
  出版社: [出版方, 出版机构]
  ...
joins:
  - dim_book.book_id = fact_rating.book_id
  - dim_book.book_id = fact_click.book_id
```

### 3.6 `data/semantic/defaults.yml`（输出约定）

三组数值（`limits` / `format` / `stats`），缺字段时用 `loader.FALLBACK_DEFAULTS` 兜底、不报错：

```yaml
version: 1
limits:
  ranking: 10        # 排名/榜单类默认行数
  single: 1          # 「哪一本/哪一个」这类单数问题返回行数
  detail: 50         # 明细类行数上限
format:
  decimal_places: 2  # 比率/占比/均值保留位数
  percent_scale: 100.0
  integer_places: 0
stats:
  min_group_size: 5  # 分组排名的最小样本量
```

**约束**：只接受数值。写 `"10 行"` 这类字符串会被忽略并回落到兜底值——这是有意为之，
因为把非数值塞进 SQL 会生成语法错误的语句，而静默回落至少还能跑。

**为什么放进契约**：这层是「模型不该临场决定的事」。行数、小数位如果每次让模型自己定，
同一类问题两次跑出来形状就不同，评测里会表现为大量"列数/行数不同"，看起来像模型能力问题，
实际是约定缺失。

### 3.7 `data/gold.jsonl`
每行一个 JSON：
```json
{"id": "Q01", "type": "T1", "question": "一共有多少本书", "sql": "SELECT COUNT(*) FROM dim_book", "note": "单表计数"}
```
题面经审计修改过的条目额外带 `question_v1` 字段，保存原始问法；完整快照见 `data/gold.v1.jsonl`。

## 4. 完成标准（每个人交活前自查）

1. `D:/Python312/python.exe -m pytest tests/ -q` 自己模块的测试全绿。
2. 不引入未安装依赖；`python -c "import app.xxx"` 能过。
3. 不写 `print()` 调试残留；用 `logging`。
4. 公共函数有 docstring，说明"为什么这样"而不只是"做了什么"。
5. 不修改 `app/core/*`；确实需要改，先在自己的报告里写明理由。
