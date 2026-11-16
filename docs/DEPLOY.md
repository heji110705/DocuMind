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
FROM docker.1ms.run/library/python:3.12-slim AS builder
```

> ⚠️ **已知风险**：`docker.1ms.run` 这条通道时好时坏。线上镜像已经构建好了，
> 增量重建不会重新拉基础层（有缓存）。但**一旦执行 `docker system prune -a`
> 或缓存丢失，就必须先确认这个源还能用**，否则会卡在拉取。

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
