# HTTP 与 SSE 接口参考

Base URL 本地默认 `http://127.0.0.1:8000`，所有 `/v1/*` 需要
`Authorization: Bearer <token>`。错误响应统一为：

```json
{"error": {"code": "E1061", "message": "SQL 被安全策略拒绝", "detail": "...", "trace_id": "..."}}
```

`trace_id` 在任何响应里都有，报障时先给这个。


## 一、端点清单

| 方法 | 路径 | 用途 | 鉴权 |
|---|---|---|---|
| GET | `/healthz` | 存活探针，不查库 | 无 |
| GET | `/readyz` | 就绪探针，检查快照与语义层 | 无 |
| POST | `/v1/ask` | 一次问答，同步返回完整结果 | Bearer |
| POST | `/v1/ask/stream` | SSE 流式问答 | Bearer |
| GET | `/v1/sessions/{id}` | 取会话历史 | Bearer |
| DELETE | `/v1/sessions/{id}` | 删除会话及其 trace | Bearer |
| GET | `/v1/metrics` | 列出可用指标卡 | Bearer |
| GET | `/v1/metrics/{key}` | 取单个指标的完整定义 | Bearer |
| GET | `/v1/tables` | 列出表卡 | Bearer |
| GET | `/v1/traces/{trace_id}` | 取一次运行的完整 trace | Bearer |
| POST | `/v1/feedback` | 提交答案反馈（对/错/部分对） | Bearer |
| POST | `/v1/admin/reload` | 热重载语义层配置 | Bearer + admin 角色 |
| GET | `/v1/admin/quota` | 查当前租户配额用量 | Bearer + admin |
| POST | `/v1/admin/explain` | 只做 SQL 安全校验，不执行 | Bearer + admin |

## 二、逐个端点

### `GET /healthz`

存活探针，不查库。鉴权：无。

返回：`{"status":"ok","version":"1.4.2"}`

说明：K8s liveness 用，2s 超时。

### `GET /readyz`

就绪探针，检查快照与语义层。鉴权：无。

返回：`{"ready":true,"warehouse":"ok","semantic":"ok"}`

说明：快照缺失返回 503 + E1090。

### `POST /v1/ask`

一次问答，同步返回完整结果。鉴权：Bearer。

参数：
- question (必填, ≤500 字)
- session_id (可选)
- mode (naive|mix|mix_rr|full, 默认 full)
- top_k (默认 6)
- debug (默认 false)

返回：`answer / sql / rows / trace_id / citations / refused / cost`

说明：p50 1.1s（naive）～9.8s（full）。

### `POST /v1/ask/stream`

SSE 流式问答。鉴权：Bearer。

参数：
- 同上
- accept: text/event-stream 必填

返回：`事件流：meta → stage* → token* → table → citations → done`

说明：每 15s 发一次 ping 保活。

### `GET /v1/sessions/{id}`

取会话历史。鉴权：Bearer。

参数：
- id (路径参数)
- limit (默认 20)

返回：`turns 数组`

说明：超过 24h 自动过期。

### `DELETE /v1/sessions/{id}`

删除会话及其 trace。鉴权：Bearer。

参数：
- id

返回：`204 无内容`

说明：审计日志保留 90 天不受影响。

### `GET /v1/metrics`

列出可用指标卡。鉴权：Bearer。

参数：
- q (模糊搜索)

返回：`metrics 数组（key/name/unit）`

说明：用于前端指标下拉。

### `GET /v1/metrics/{key}`

取单个指标的完整定义。鉴权：Bearer。

参数：
- key

返回：`definition / formula / caveats`

说明：key 不存在返回 422 E1032。

### `GET /v1/tables`

列出表卡。鉴权：Bearer。

返回：`tables 数组`

说明：只列已声明表，不读数据库元数据。

### `GET /v1/traces/{trace_id}`

取一次运行的完整 trace。鉴权：Bearer。

参数：
- trace_id

返回：`spans 数组（含耗时、token、SQL）`

说明：trace 保留 7 天。

### `POST /v1/feedback`

提交答案反馈（对/错/部分对）。鉴权：Bearer。

参数：
- trace_id
- verdict (correct|wrong|partial)
- note (可选 ≤200 字)

返回：`{"ok":true}`

说明：写 feedback 表，不回流训练。

### `POST /v1/admin/reload`

热重载语义层配置。鉴权：Bearer + admin 角色。

参数：
- force (默认 false)

返回：`{"reloaded":true,"metrics":32}`

说明：校验失败则保留旧配置并返回 E1030。

### `GET /v1/admin/quota`

查当前租户配额用量。鉴权：Bearer + admin。

返回：`qps / daily_tokens / used`

说明：用量按小时聚合，非实时。

### `POST /v1/admin/explain`

只做 SQL 安全校验，不执行。鉴权：Bearer + admin。

参数：
- sql

返回：`allowed / reasons / estimated_rows`

说明：排查 E1061 用。


## 三、SSE 事件序列

流式接口的事件顺序是固定的，客户端按 `event:` 字段分发即可。除 `stage` 之外每种事件在一次请求中最多出现一次（`token` 会多次）。

| 事件 | 载荷字段 | 何时发 |
|---|---|---|
| `meta` | trace_id, mode, session_id | 连接建立后立即发一条 |
| `stage` | name, status(start|end), ms | 每个编排节点进出各一条 |
| `token` | text | 答案正文增量，逐 token |
| `table` | columns, rows | 结构化结果，一次性发完 |
| `chart` | svg | 服务端渲染的 SVG 字符串 |
| `citations` | chunk_id, file_path, snippet, score | 答案定稿后发一条，最多 5 条 |
| `error` | code, message | 任何阶段失败，发完即关流 |
| `done` | cost_cny, tokens_in, tokens_out, elapsed_ms | 结束标记，客户端据此收流 |

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
