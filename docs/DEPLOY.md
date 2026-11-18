# 部署手册（阿里云 ECS）

> 本文的每一条都在真实机器上验证过，标注了验证方式。
> 环境：阿里云 ECS 经济型 e，2 vCPU / 2 GiB，华南3（广州），Ubuntu 22.04，
> 公网 IP `8.163.90.97`，免费试用至 2027-02-15。
> 当前状态：`http://8.163.90.97/` 可访问，容器 `documind:1.5`，镜像 2.19 GB。

---

## 1. 为什么最后是"ECS + Docker Compose"

先后试过三条路，前两条都不通，记录在此避免重复踩：

| 路线 | 结果 | 卡在哪 |
|---|---|---|
| HuggingFace Spaces | ❌ | 免费 Docker Space 已取消，创建即 402 要求 PRO |
| 本机 Docker Desktop | ❌ | 官方 CDN 对当前网络返回 403，国内镜像站不分发安装包；WSL2 需管理员权限重启 |
| 阿里云 ECS（免费试用） | ✅ | 见下文 |

---

## 2. 架构

```
公网 :80  ──►  docker-compose（documind-documind-1）
                 │  ports: "80:8000"
                 │  env_file: .env
                 ├── /app/rag_storage  ← 挂载宿主机 ./rag_storage（读写）
                 └── /app/data         ← 挂载宿主机 ./data（读写）
```

目录：`/opt/documind/`（代码 + Dockerfile + docker-compose.yml + .env + 索引）

**为什么索引和 data 是读写挂载、不是 `:ro`**：原仓库版写的是 `:ro`，但 LightRAG 查询时
会写 kv 缓存，只读挂载有查询报错风险。原版 `:ro` 从未在真实部署里跑过。

---

## 3. 部署时趟平的四个坑（每个都对应 Dockerfile 里的一处改动）

### 3.1 `docker.io` 在大陆被 DNS 污染，且 BuildKit 不吃 daemon.json 的加速配置

`docker.io` 解析到 `31.13.94.23`（Facebook 的 IP），拉取直接失败。
**关键点**：即使你在 `/etc/docker/daemon.json` 里配了 `registry-mirrors`，
BuildKit 构建时也不读它——它只影响 `docker pull`。

所以 `FROM` 必须直写镜像源：

```dockerfile
FROM docker.m.daocloud.io/library/python:3.12-slim AS builder
```

> 最初用的是 `docker.1ms.run`，2026-11-16 换成了 daocloud。原因：前者是第三方加速站，
> 稳定性没有保证，而它失效时的报错是 DNS/超时，很容易被误读成"网络问题"。
> 换源当天的验证方式不是"改完重建"，而是先单独跑
> `docker pull docker.m.daocloud.io/library/python:3.12-slim`——拉得下来，才说明源是活的；
> 改完立刻全量重建的话，失败一次就是站点下线。
>
> **注意**：改 `FROM` 会让构建缓存全部失效（镜像名变了，层全不命中）。
> 所以这次只改了仓库与服务器上的 Dockerfile，**没有立即重建 documind**——
> 它下次构建时才会走新源，那次构建会是全量重装（依赖 + 600MB 模型预热）。

### 3.2 `fastembed` 不读 `HF_HOME`，只认 `FASTEMBED_CACHE_PATH`

构建期把模型烤进镜像，运行时却找不到——因为它找的是另一个变量。
Dockerfile 里两个都要设，并且 runtime 层要 `COPY` 模型目录。

### 3.3 2C2G 上"构建 + 600MB 模型预热"同跑 → swap 死亡螺旋

表现为：构建中途 SSH 断连、控制台都连不上，只能强制重启。
解法：构建期只装依赖，模型预热拆成独立层，并加 2G swap 兜底。

### 3.4 原 Dockerfile 只预热 embedding、漏了 reranker

首次提问时现场下载 600MB 的重排模型，表现为"第一问卡 40 秒"。
VPS 版把两者拆成两个独立的 `RUN` 层——任一成功即永久缓存，
大文件走 hf-mirror 断流时只重试它自己，不连坐。

---

## 4. 内存约束（**这是本机最脆弱的地方**）

| 项 | 数值 | 来源 |
|---|---|---|
| 物理内存 | 1.7 GiB 可用 | `free -h` |
| 容器常驻 | **1.2 GiB**（单进程 1215 MB） | `docker stats` |
| 宿主其余进程 | ≈ 100 MB（dockerd / containerd / systemd） | `ps aux` |
| swap | 2 GB → 本次扩到 **4 GB** | `swapon --show` |
| 磁盘 | 40 GB，已用 32% | `df -h` |

**2026-11-16 发生过一次真实宕机**：站点连续 30 秒无响应、SSH banner 超时、
Workbench 也连不上，但控制台显示实例"运行中"、CPU 23%。
判定为内存压力导致的整机卡死（无 OOM kill 记录，是 swap 抖动而非 oom-killer）。
处置：控制台**强制重启**，容器 `restart: unless-stopped` 自动拉起，约 60 秒恢复。

**加固措施（已执行）**：

```bash
fallocate -l 2G /swap2 && chmod 600 /swap2 && mkswap /swap2 && swapon /swap2
echo "/swap2 none swap sw 0 0" >> /etc/fstab
sysctl -w vm.swappiness=10          # 优先用物理内存，减少过早交换
```

**尚未解决**：`docker-compose.yml` 里 `mem_limit: 3g` 大于物理内存 1.7G，
**等于不设限**。真要限制，得先降容器常驻（主要是本地重排模型约 600 MB），
否则一设限就会立刻 OOM 被杀。这件事没有做，因为要拿"高精度"档换。

---

## 5. 改动怎么上线（三种改动，三条命令）

**先理解一件事**：`.dockerignore` 排除了 `rag_storage/` 和 `.env`，
所以索引和配置**不在镜像里**，靠 compose 挂载；代码是 `COPY . .` 打进镜像的。

| 你改了什么 | 要重建镜像吗 | 命令 |
|---|---|---|
| 代码 / 前端静态文件 | **要** | `rsync 代码 → docker compose up -d --build` |
| `.env` | 不要 | `docker compose up -d --force-recreate`（env 只在创建容器时读） |
| `rag_storage/`（索引） | 不要 | `rsync 目录 → docker compose restart` |

**千万别在 2C2G 上 `docker system prune -a`**。依赖层和模型层是分开缓存的，
只改代码只重跑最后几层（几十秒）；缓存一清就要重下 600MB，大概率重演 3.3 的卡死。

### 5.1 两种同步通道，选一

**通道 A：走 git（推荐，代码有版本、可回滚）**

```bash
# 服务器上（首次）
cd /opt/documind && git init && git remote add origin <repo> && git fetch && git checkout -f main
# 之后更新
cd /opt/documind && git pull && docker compose up -d --build
```

需要给服务器配 deploy key（仓库公开则可以匿名拉）。
**注意**：`.env` 与 `rag_storage/` 不在仓库里，git 操作不会碰它们，这是好事。

**通道 B：走 rsync（最省事，但必须排除部署定制文件）**

```bash
rsync -av --delete \
  --exclude='.git' --exclude='.env' --exclude='rag_storage' --exclude='data' \
  --exclude='Dockerfile' --exclude='docker-compose.yml' \
  ./ root@8.163.90.97:/opt/documind/
```

**排除 `Dockerfile` 和 `docker-compose.yml` 是关键**——服务器那两份是部署定制版
（`80:8000` 端口、`ALLOW_UPLOAD=0`、`RERANK_THREADS=2`、读写挂载），
被仓库默认版覆盖就直接翻车。

> 本文档所在目录 `deploy/` 就是服务器那两份定制文件的副本（与线上 `diff` 无差异），
> 所以通道 B 如果想保留定制，把 `deploy/` 里的文件放到服务器上即可。

---

## 6. 验证与自愈

```bash
# 存活
curl -s -o /dev/null -w '%{http_code}\n' http://8.163.90.97/          # 期望 200
curl -s http://8.163.90.97/readyz | head -c 200                        # 期望 ready:true

# 容器
docker ps --format 'table {{.Names}}\t{{.Status}}'
# 期望 Up ... (healthy)

# 日志（模型加载、索引规模）
docker logs documind-documind-1 2>&1 | tail -30
```

`restart: unless-stopped` + healthcheck（30s 间隔，3 次失败）保证容器级故障自愈。
**但主机级卡死它救不了**——那种情况只能控制台强制重启，见第 4 节。

---

## 7. 首次从零部署的步骤

```bash
# 1. 装 Docker（阿里云源，避开 docker.io）
curl -fsSL https://mirrors.aliyun.com/docker-ce/linux/ubuntu/gpg | gpg --dearmor -o /usr/share/keyrings/docker.gpg
echo "deb [arch=amd64 signed-by=/usr/share/keyrings/docker.gpg] https://mirrors.aliyun.com/docker-ce/linux/ubuntu jammy stable" > /etc/apt/sources.list.d/docker.list
apt-get update && apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

# 2. 加 swap（必须先做，否则构建阶段就会卡死）
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile

# 3. 放代码与索引
mkdir -p /opt/documind && cd /opt/documind
# 传代码、deploy/Dockerfile.vps → Dockerfile、deploy/docker-compose.vps.yml → docker-compose.yml
# 传 rag_storage/（索引，约 100 MB）和 .env

# 4. 构建并启动
docker compose up -d --build
```

---

## 8. 安全管理组

入方向只需：`22`（SSH，建议限源 IP）、`80`（HTTP）。
`8000` / `8080` 是调试期开的，公网演示**不需要**，建议关掉。

> 当前是**无鉴权开放模式**（`.env` 未配 `API_KEYS`）：任何拿到 IP 的人都能提问，
> 每次提问都在消耗 `.env` 里那个 LLM key 的额度。
> 防刷的三种办法见 README「已知限制」一节。

---

## 9. 模型切换（2026-11-16 实测）

线上一度跑 `deepseek-chat`（按量计费），为省下额度切到**百炼 `qwen3.7-flash`**（免费额度）。
切换不是改个环境变量就行——**千问系列默认开着思考模式**，不关会把预算全花在推理上。

同一句"只回两个字：收到"的实测：

| 模型 | 默认（开思考） | 加 `enable_thinking: false` |
|---|---|---|
| qwen3.8-max | 输出 19 token | **1 token** |
| qwen3.7-flash | 输出 273 token | **1 token** |

首字延迟从 3.8 s 降到 0.6 s。

**代码侧改动**：`llm_client.py` 增加 `extra_body` 透传（`_ALLOWED` 里加一项 +
从 `LLM_EXTRA_BODY` 读 JSON 合并进 `extra_body`）。注意**不能**直接把厂商字段
`setdefault` 到请求参数上——openai SDK 只认它认识的顶层参数名，其余必须走 `extra_body`，
否则报 `TypeError: unexpected keyword argument`（实测踩过，三关全挂）。

**24 题同条件对照**（都关缓存、`mix_guard`、并发 2）：

| 模型 | 可答率 | p50 | 调用数 |
|---|---|---|---|
| deepseek-chat | 75.0%（18/24） | 2.80 s | 68 |
| qwen3.7-flash | 79.2%（19/24） | 3.17 s | 69 |

差 1 题（n=24，在噪声内），延迟 +13%，调用数相同。据此切换。

**已知未处理**：`PRICE_IN_PER_M` / `PRICE_OUT_PER_M` 仍是 DeepSeek 单价，
界面上显示的 ¥ 金额对百炼不成立，只能看 token 量。

---

## 10. 开放模式的防刷闸门（2026-11-16）

线上是**无鉴权**的（`.env` 未配 `API_KEYS`），任何人拿到 IP 都能提问，每次都在烧 LLM 额度。
`app/limits.py` 加了三层限制，全部可用环境变量覆盖：

| 变量 | 默认 | 含义 |
|---|---|---|
| `OPEN_IP_PER_MIN` | 6 | 单个 IP 每分钟最多几次 |
| `OPEN_IP_PER_DAY` | 100 | 单个 IP 每天最多几次 |
| `OPEN_DAILY_TOTAL` | 300 | 全站每天最多几次 |

**只拦 `POST /v1/ask*`**，不拦 `/healthz`、`/readyz` 和静态资源——否则监控和首屏会被自己限流。

实测（连发 9 次）：`200 200 200 200 200 200 429 429 429`，同期限流期间 `/`、`/healthz`、`/readyz` 均 200。

设计上两个取舍：
- `check` / `hit` 分两步：参数校验失败的请求不消耗额度；
- fail-open：限流器自身异常时放行，宁可少挡一次，也不要让一个 bug 把 Demo 变成全站 500。

纯内存计数，重启清零。演示场景下这比引入 Redis 划算。

---

## 11. 同机部署 InsightPilot（2026-11-16）

两个项目共用一台 2C2G，端口分工：DocuMind `:80`、InsightPilot `:8001`。

实测内存占用（`docker stats`）：

| 容器 | 常驻 | 限额 |
|---|---|---|
| documind | 897 MiB | 1.696 GiB（compose 写的 3g 大于物理内存，等于不设限） |
| insightpilot | **71 MiB** | 384 MiB |

两个加起来还不到 1 GB，宿主剩余 399 MiB 可用。**比部署前（只剩 99 MiB）反而更宽裕**——
因为 DocuMind 重启后重排模型是懒加载的，没有常驻。

压测：并发 4 个 DocuMind 提问期间，InsightPilot 仍 78 ms 返回 200；swap 用了 405 MB 但没出现卡死。

部署要点（与 DocuMind 不同、容易踩的）：
1. `data/runtime.sqlite` 是**追踪日志库，271 MB**，不是数仓（数仓 `warehouse.sqlite` 只有 1.7 MB）。
   上传时排除它，传输量从 273 MB 降到 4.2 MB。
2. `FROM python:3.12-slim` 必须换国内源，原因同第 3.1 节。
3. pip 走阿里云源，否则单包能等十几秒。
4. `ports: "8001:8000"`——容器内仍是 8000，healthcheck 继续有效，不用改代码。
5. `mem_limit: 384m` + `memswap_limit: 768m`：设了上限，最坏情况是它自己被 OOM kill 然后重启，
   而不是把整机拖死（11-16 那次整机卡死就是因为没有上限）。

---

## 12. 成本账：单价从哪来，为什么必须改（2026-11-16）

切到百炼之后，`PRICE_IN_PER_M` / `PRICE_OUT_PER_M` 还停在 DeepSeek 的 1.07 / 4.29，
而线上的界面会把「token × 单价」直接算成 ¥ 展示出来。**单价不改，那个 ¥ 就是假账**——
一个主打"全链路成本可计量"的项目，这里不能糊。已按百炼控制台的公开价改掉：

| 模型 | 输入（元/百万 token） | 输出（元/百万 token） | 用途 |
|---|---|---|---|
| `qwen3.7-flash` | **0.2** | **0.8** | DocuMind 线上 |
| `qwen3.8-flash` | 0.8 | 2.7 | InsightPilot 线上 |
| `deepseek-chat`（历史） | 1.07 | 4.29 | 第 2 节各表的 ¥ 口径 |

来源：百炼控制台「模型广场 → 具体模型 → 模型价格」（阶梯计费，本项目提示词都在 ≤32k 档，
单价按该档取）。免费额度内的实际扣费是 **¥0**，单价只用于"token 量 → 金额"的折算展示。

**为什么换模型就一定要改单价**：同一份 4,000 token 的请求，
`deepseek-chat` 折 ¥0.0056、`qwen3.7-flash` 折 ¥0.0011——差 5 倍。
不改的话，界面上"多跑一个开关贵多少"的横向比较全部失真。

改法（两个环境变量，改完 `docker compose up -d --force-recreate`，不用重建镜像）：

```
PRICE_IN_PER_M=0.2
PRICE_OUT_PER_M=0.8
```

---

## 13. 公开演示的四条加固（2026-11-16）

审计发现的问题，都属于"平时不显、演示时会撞上"的那类：

| 问题 | 改法 | 验证 |
|---|---|---|
| 限流先计数后处理，输错一次问题也吃额度 | `hit()` 挪到响应之后，且只在 `status < 400` 时记 | 连发 8 次 400，随后一次正常提问仍 200（旧逻辑第 7 次就该 429） |
| 开放模式并发写死 2，三个人同时点就 429 | 默认 4，可用 `OPEN_CONCURRENCY` 覆盖 | 3 个并发提问全部 200（旧上限下第 3 个会 429） |
| 上传先读完整个请求体、再校验 20MB | 先看 `Content-Length`，再分块读、超限即停 | `test_read_upload_stops_at_limit`（21MB 请求体 → 413） |
| 限流只认 `client.host`，挂 nginx 后全塌成一个桶 | 优先 `X-Forwarded-For`，回落 `client.host` | 代码级；线上直连时 XFF 为空，行为不变 |

另外把 `/docs` 与 `/openapi.json` 默认关掉了（`DOCS=1` 可开）：
这两个地址会把全部接口列在公网上，包括 `/v1/documents` 这种写接口。

---

## 14. 线上模型切到 qwen-plus（2026-11-18）

`qwen3.7-flash` 的 1M 免费额度在 11-18 下午耗尽，线上开始对访客返回 500
（真实原因是上游 `403 Free quota exhausted`）。已切到 **`qwen-plus`**：

```
LLM_MODEL=qwen-plus        # .env，改完 docker compose up -d --force-recreate
```

**代价与口径**：
- 延迟变高：同一批题 p50 从 2.2 s 升到 3.5 s（`/v1/ask` 实测），重排档更明显。
- **成本列失真**：`PRICE_IN_PER_M` / `PRICE_OUT_PER_M` 仍是 0.2 / 0.8（qwen3.7-flash 的价），
  qwen-plus 的单价不同，所以界面上的 ¥ **只能当同口径的相对比较，不能当账单**。
  要修准需要按控制台单价改这两个变量，本次未改（没有核实到的价格不往上写）。

**已知缺陷**：qwen-plus 偶发引用越界（只有 6 块却写 `[16]`），
`parse_citations` 会丢弃越界编号，于是 `/v1/ask` 返回的 `chunks` 为空、界面来源空白。
已在证据块开头加了"共 N 条、编号 1–N"，但**没有根治**（30 题里仍出现约 1/4）。
