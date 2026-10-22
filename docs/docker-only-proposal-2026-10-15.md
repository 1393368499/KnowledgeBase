# 纯 Docker 部署方案（讨论稿 · 修订 2）

日期：2026-10-15 · 状态：**讨论稿，未实施** · 修订依据：方案审阅意见 1–9

## 0. 本轮边界

- **只改本文档，不实施**：不改 `.env`、不改 compose、不重建镜像、不写产品代码。
- **不启停/不删除任何服务或数据**；不恢复旧迁移目标；不启动后台持续目标。
- **停止**沿用独立 WSL 发行版 `KnowledgeBase-Runtime` / `wsl.exe` / `56339` 那条路线。

---

## 1. 结论摘要（简化后的推荐）

1. **一套 Compose 统一管理全栈**（web / worker / beat / PostgreSQL / Redis / Qdrant），服务间用服务名寻址；**停止独立 WSL 路线**。
2. **存储方案分两步**：
   - **方案 A（沿用现有 `./vector_data/qdrant` bind mount）**：满足"不搬动数据"，可作为**过渡**运行；但**它不合格为全功能交付**——已知 `kb-9-g1` 检索 500 仍在，且 9p 故障条件未被消除。
   - **方案 B（Docker named volume）**：是**面向可靠交付**的目标形态（官方明确建议 named volume，见 §4），但它**属于"存储位置变更"**——数据从项目根 `vector_data/` 移到 Docker 管理的卷里。**是否接受这条约束变更，需要你决定**；在获批前**按 A 运行**，且**不把 B 说成"完全没有迁移"**（B 仍要把现有索引搬进卷，或重建索引）。
3. 本文**不再把非关键事项推给你**：副本与冷备**默认保留**；`kb-9-g1` 的索引修复属于**既有系统目标内的工作**（需要调用 AS711 嵌入接口），本轮只是还没执行——**不另设永久审批流程**。

---

## 2. 当前事实（本轮实测）

| 项 | 事实 |
|---|---|
| Web | 跑在 **Windows 本机**（8000，PID 19672，`python`）——"Windows solo" |
| Worker/Beat | 有镜像 `kb-worker:local`（`deploy/worker/Dockerfile`，`python:3.13-slim`，uid 10001），由 `deploy/docker-compose.worker.yml` 经 `host.docker.internal` 访问宿主服务 |
| 根 compose | `docker-compose.yml`：`name: knowledgebase`，**只有** postgres / redis / qdrant |
| PostgreSQL | `postgres:17.6-alpine3.22`，命名卷 `knowledgebase-pgdata`，`127.0.0.1:55432`；**无 pgvector**（仅 `pg_trgm 1.6`、`unaccent 1.1`） |
| Redis | `redis:7.4-alpine`，命名卷 `knowledgebase-redisdata`，`127.0.0.1:56379`；**DB0 有 193 条业务消息（不得消费/清空）** |
| Qdrant | `qdrant/qdrant:v1.15.5`，`127.0.0.1:56333`；**bind mount `./vector_data/qdrant` → `/qdrant/storage`** |
| 容器内文件系统 | `findmnt -T /qdrant/storage` → **`9p`**，源 `D:\[.../vector_data/qdrant]`，`msize=65536` |
| 向量数据 | **9 个集合 / 1865 文件 / 实占约 11 MB**（逻辑 3.44 GiB，多为稀疏空洞） |
| 原件 | `media/raw` 271 文件 / 0.15 MB |
| 原件写入实现 | **内容寻址** `<raw>/<sha256 前两位>/<sha256>`；先写 `*.part`（同一文件系统）边写边算哈希，再 `os.replace` **原子替换**；已存在则**复用**（`reused=True`）——见 `apps/knowledge/storage.py` |
| 镜像 COPY 面 | `requirements.txt`、`config/`、`apps/`、`governance/`、`manage.py` |
| 模板/静态位置 | **在 `apps/webui/templates`、`apps/webui/static`**（属 `COPY apps/` 范围内） |
| 生产服务器依赖 | **`gunicorn==26.2.0`、`uvicorn==0.54.0` 已在 `requirements.txt`** |
| 静态配置 | `STATIC_URL="static/"`、`STATIC_ROOT=<项目根>/staticfiles`；**未安装 whitenoise** |
| WSGI/ASGI | `config/wsgi.py`、`config/asgi.py` 均存在 |
| 关键 settings | `KB_QDRANT_URL`/`QDRANT_URL`、`KB_QDRANT_MODE`（默认 server）、`KB_CELERY_BROKER_URL`/`CELERY_BROKER_URL`（默认= `REDIS_URL`）、`REDIS_URL`、`RAW_STORE_DIR`=`<项目根>/media/raw`、`KB_SKIP_DOTENV`、`KB_UPLOAD_PROCESS_MODE`（默认 auto）、`KB_QDRANT_COLLECTION_PREFIX`（默认 `kb`） |

---

## 3. Docker Desktop 内部容器 vs 额外独立 WSL

| | **Docker Desktop 内部 Linux 容器** | **额外独立 WSL**（已弃用） |
|---|---|---|
| 是什么 | Docker Desktop 的**工具 VM**（默认 WSL2 后端）里跑容器 | 另建的一个普通 WSL 发行版，与 Docker 无关 |
| 文件系统 | 容器层与**命名卷在该 VM 的 ext4**（实测命名卷 `findmnt` → `ext4 /dev/sdd`） | 自己的 ext4（`/dev/sdf`） |
| 生命周期 | `docker compose` 统一声明、可健康检查、服务名互通 | 需 `wsl.exe` + 自写启动/保活，**进不了 compose** |
| 数据在项目根 | 命名卷 ❌ / bind mount ✅（但跨 9p） | ✅（VHDX 在 `vector_data/`） |

**要点**：容器本来就跑在 Linux+ext4 上。**问题不是"容器里没有 ext4"，而是"数据经 Windows 路径以 9p 跨进容器"**。所以"不用额外 WSL"≠"不用 ext4"——Docker Desktop 的命名卷本身就是 ext4（已实测）。官方对 WSL2 后端与绑定挂载的说明见 <https://docs.docker.com/desktop/features/wsl/>。

---

## 4. 根因表述与证据边界

**已观察到的现象（事实）**
- Qdrant 容器内 `/qdrant/storage` 的文件系统是 **9p**（bind 自 Windows 目录）。
- `kb-9-g1` 查询返回 **HTTP 500**（1 个点）；`kb-7-g1`(0 点)、`kb-11-g1`(2 点) 正常。
- 容器重启后集合能加载（日志可见 `Recovered collection kb-9-g1: 1/1 (100%)`），但**查询仍 500**。

**证据边界（措辞必须收敛）**
- 上述现象与 Qdrant 官方"不兼容文件系统"的说明**高度吻合**，官方对该类问题的建议是使用 **named volume**，并列出 `OutputTooSmall` 等错误特征：<https://qdrant.tech/documentation/common-errors/>（Incompatible filesystem / OutputTooSmall）。
- **但不能断言**"ext4 上永远不会出问题"，**也不能断言**"每次 500 都已被证明由 9p 造成"。当前证据是：**故障与 9p 挂载同时存在且官方文档高度一致**；"换到 ext4 后不再复现"属于**待验证**，不是已证结论。
- 同理：**移动文件不修复已写坏的 segment**。`OutputTooSmall` 是读已有 segment 时触发；把相同字节搬到 ext4，**坏的仍是坏的**。因此 `kb-9-g1` 需要**重建**，这与存储方案解耦。
- **健康检查不能冒充数据健康**：本轮真实发生过一次误判——健康检查只看"端口有响应"，把遗留探针实例（提供 `codex-runtime-probe-20261015`）当成了目标实例。任何验收都必须核对**集合名 / 点数 / 真实查询结果**。

---

## 5. 存储方案对照（约束取舍写清）

| | **A 沿用 bind mount** | **B Docker named volume** | **C PG + pgvector** |
|---|---|---|---|
| 摆脱 9p | ❌ | ✅ | ✅（不再涉及 9p） |
| 是否搬动数据 | **不搬动**（原地） | **要搬**：把现有索引写进卷，或重建索引 → **属于存储位置变更，不是"没迁移"** | 要把向量导出/灌入 PG |
| 数据在项目根 | ✅ | ❌ **需你批准调整该项目根约束** | ❌ 同类冲突 |
| 现有向量 | 沿用（坏的仍坏） | 复制可沿用；或重建 | 可**导出复用**（见下） |
| 需 AS711 嵌入 | 仅重建损坏库时需要 | 仅重建损坏库时需要（复制路线不需要） | 稠密向量可导出复用；**逻辑上并非必须重新嵌入** |
| 代码改动 | 无 | 仅 compose + 一次数据搬迁 | **大**：等于换检索引擎 |
| 交付定性 | **过渡方案**（不搬动数据，但**不合格为全功能交付**：已知 500 仍在、9p 条件未消除） | **面向可靠交付的目标形态** | 改造与回归面最大，**本阶段不推荐** |

**关于 C（修订）**：pgvector **并不逻辑上要求重新嵌入**——库中健康的向量可以导出并复用；之所以仍不推荐，是因为**改造面大**（`vectorindex.py` 的集合/代次/原子切换、`pipeline.py` 的稠密+稀疏+**RRF 融合**+payload 过滤都要用 SQL 重建），且**当前 PG 镜像没有 pgvector**（需换镜像并对现有数据卷做兼容处理）。这是"改造大"，不是"做不到"。

**关于 B（修订）**：B 是**可靠交付**的目标形态（官方建议 named volume），但必须诚实地称其为**存储位置变更**：数据将由 Docker 管理，**不再落在项目根 `vector_data/`**。这条约束变更需要你明确同意；在你同意之前，**按 A 作为过渡运行**。

---

## 6. 统一 Compose 形态（含修订项）

目标：**一条 `docker compose up -d` 起全栈**，服务间用**服务名**互通（去掉 `host.docker.internal`）。

```
name: knowledgebase           # 保持既有项目名与卷名，避免卷被重命名而丢数据
services:
  postgres:  命名卷 knowledgebase-pgdata     （沿用现有）
  redis:     命名卷 knowledgebase-redisdata  （沿用现有）
  qdrant:    A=./vector_data/qdrant bind mount ；B=named volume（需批准）
  web:       同镜像 → gunicorn（见下），静态资源明确交付
  worker:    同镜像 → celery worker -Q kb_worker_v2 --concurrency=N
  scheduler: celery beat（**仅在确有必要时**独立启用，见下）
```

### 6.1 原件挂载必须**区分 web 与 worker**（修订）

**"原件不可变" ≠ "原件目录不能新增"**。上传必须能落新原件，因此：

| 服务 | 挂载 | 约束 |
|---|---|---|
| **web** | `./media/raw:/app/media/raw`（**可写**） | 只允许**内容寻址新增**与**原子创建**；**禁止覆写既有原件** |
| **worker** | `./media/raw:/app/media/raw:ro` | **只读**（解析原件，不改原件） |

这条不是"约定"，而是**代码已具备**的保证（`apps/knowledge/storage.py`）：
- 路径固定为 `<raw>/<sha256 前两位>/<sha256>`，与文件名无关 → **不会按名字覆盖别人**；
- 先写 `*.part`（同文件系统），写完 `os.replace` → **原子替换**，无半截文件；
- 已存在同摘要则**复用**（`reused=True`）→ 天然去重，不重复落盘。

> 因此 §6 早期版本"原件一律 `:ro`"的说法是**错的**：那样会让上传直接失败。

### 6.2 镜像能否跑 web（修订，已核实）

- `COPY apps/` **已包含** `apps/webui/templates`、`apps/webui/static` → **模板与静态源文件没有遗漏**。
- **但"只覆盖 command 就能跑 web"不成立**，至少还缺/需确认：
  1. **`STATIC_ROOT=<项目根>/staticfiles` 不在镜像内**，需在构建期 `collectstatic`（或在 compose 里挂载交付），否则静态资源 404；
  2. **未安装 whitenoise**，所以"由 Django 自己发静态文件"需要额外方案：加 whitenoise，或用 gunicorn 之外的前端代理/静态卷；
  3. 镜像自带 **worker 的 `CMD` 与 `HEALTHCHECK`**（`celery inspect ping`）→ web 服务必须**同时覆盖 command 与 healthcheck**（beat 早已踩过此坑）；
  4. 运行用户是 uid 10001，`STATIC_ROOT` 的写权限需在构建期处理。
- **生产不使用 `runserver`**：依赖里已有 **`gunicorn==26.2.0`**（WSGI，配 `config/wsgi.py`）与 **`uvicorn==0.54.0`**（可用 gunicorn 的 uvicorn worker 跑 `config/asgi.py`）。**具体用 WSGI 还是 ASGI、worker 数、超时**需按实测确定，不在本文拍板。
- **静态资源优先评估"轻量进程内"方案**（如 WhiteNoise 这类进程内静态服务），目的是**少一个常驻容器**；若评估不成立，再退回到前端代理或静态卷。**选型必须按官方文档确认**后再实施，不在本文预先拍定。

### 6.3 scheduler（修订）

- 现状：beat 容器的健康检查是 **`test -f /var/lib/kb/celerybeat-schedule`** —— 只证明"调度文件存在"，**不证明调度在推进**。
- 因此：**需在验收阶段补真实判据**（心跳时间戳推进 / 最近一次调度结果），文件存在只能作为"启动成功"的弱判据。
- **独立 scheduler 容器只在确有真实周期任务需要时启用**（当前已有真实任务 `knowledge.sweep_pending`）；若无周期任务，不应为了"看起来完整"而常驻一个 beat。

### 6.4 必须落实的接线

| 项 | 要求 |
|---|---|
| 容器内不读 `.env` | 全部服务 `KB_SKIP_DOTENV=1`（`.env` 用 `override=True`，否则被覆盖回 127.0.0.1） |
| DB | `KB_PG_HOST=postgres`、`KB_PG_PORT=5432` |
| Redis | 缓存用 `KB_REDIS_URL`；**Celery 用 `KB_CELERY_BROKER_URL` 指向专用 DB**；**先查该 DB 是否已被占用**（`LLEN`/键扫描）再选用，**不盲用示例 `/15`**；**web（生产者）／worker（消费者）／scheduler 三方必须同一套 broker/backend/队列配置**；`/0` 的 193 条**永不被消费** |
| Qdrant | `KB_QDRANT_URL=http://qdrant:6333`、`KB_QDRANT_MODE=server` |
| 原件 | web 可写（内容寻址）／worker 只读；`RAW_STORE_DIR=/app/media/raw` |
| beat 状态目录 | 需对 uid 10001 可写（现有做法：宿主绑定 `deploy/worker/beat-state`） |
| 模型 | 仅经 `KB_*` 模型接口访问 AS711；`KB_MODEL_CALLS_DISABLED` 按环境设 |
| 健康检查 | 每服务用**自己的**真实判据（PG `pg_isready`、Redis `ping`、Qdrant `/collections`、worker `inspect ping`、web HTTP、scheduler 心跳推进） |

---

## 7. 可靠性：并发上限 / 超时 / 连接复用 / 小批嵌入 / 背压（修订）

**先纠正一个混淆**：**熔断 ≠ 限流**。现有 `CircuitBreaker`（`apps/knowledge/resilience.py`）只在连续失败后**短路**，它**不限制并发、不做排队**。需要分别落实：

| 维度 | 现状（已核实） | 需补 |
|---|---|---|
| **AS711 接口级并发上限** | **无显式上限** | 每类接口设并发闸门（信号量/令牌），避免把 AS711 打满；具体数值**待实测** |
| **超时** | 探针路径有 `timeout` 参数（如 `probe_dimension(timeout=…)`） | 明确接入/读取/总计超时，并区分"可重试"与"立刻失败" |
| **连接复用** | 未核实 | 复用 HTTP 连接池（keep-alive），减少握手开销 |
| **小批嵌入** | 未核实批次策略 | 分批提交，批次大小**按实测**调，避免单请求过大 |
| **队列背压** | 有专用队列 + 租约；`--concurrency` 默认 4 | 明确"入队速率 > 消费速率"时的行为（拒绝/排队/降级），不能让队列无限增长 |
| **重试/幂等** | `RETRYABLE_STATUS` 白名单、`RetryExhausted`；内容寻址 + `(library, sha256)` 幂等 ingest + `VectorCollection` 唯一约束 | 跨进程重试靠 Celery；重复投递应安全（已具备幂等基础） |
| **崩溃恢复** | **`acks_late`/`reject_on_worker_lost` 未接线**（grep 无命中）；`process_ingested_file` 捕获 `PipelineError` 后返回 `ok=false`，**不触发 Celery retry** | 容器重启会丢在途任务 → 需接线并做**杀 worker 子进程**的隔离验收 |
| **熔断口径** | `BREAKERS` 是**进程内**（web/worker 各一份，非全局；指标已标 `scope: process`） | 若要全局口径需外置存储；否则**明确标注为进程内** |

**并发不要当成性能承诺**：`--concurrency=4` 只是**默认值**。worker 应**先小规模起步**（例如 1–2），按实测吞吐/错误率/AS711 负载再上调；本文不给"能力上限"的数字。

---

## 8. 验收口径（修订）

**阶段部署（过渡，方案 A）**：可以把已知失败**记录在案**（例如 `kb-9-g1` 查询 500），用于说明"当前处于过渡态"。

**最终全功能交付**：**必须没有该 500**。验收至少包含：

1. `docker compose ps` 全服务 healthy（各用自己的判据）。
2. web：`GET /api/v1/health/ready/` 且向量检查为 ok。
3. PG：`pg_isready` + `django_migrations` 一致。
4. Redis：`ping` PONG；**DB0 `LLEN celery` 仍为 193**（证明旧队列未被消费）。
5. Qdrant：对**所有在用信息库**逐一核对**当前活动代次**（集合名、`points_count`、维度），与基线对照。**不做"必须永远存在那 9 个旧集合名"的死断言**——用新代次修复后集合名会变（例如 `kb-9-g1` → `kb-9-g2`），此时按"**该库的活动代次**"判定通过。
6. **逐库真实向量查询**：对每个在用库的**活动代次**做一次真实查询并记录结果；最终交付要求全部成功（点数为 0 的库需单独说明其"空库"状态）。
7. **点 payload 校验**：抽查命中点的 payload 与预期资料对应（含库归属）。
8. **重启一致性**：只重启 Qdrant → 复跑第 5–7 项，活动代次／点数／查询结果与重启前一致。
9. **有效任务并发与恢复**：并发投递 N 个任务，验证实际并发执行、租约互斥、以及**杀 worker 后任务能恢复**（依赖 §7 的 `acks_late` 接线）。
10. 端到端：小文件上传 → 落 `media/raw`（内容寻址新增，不改既有原件）→ worker 消费 → 检索命中。
11. 权限与审计：无权用户检索不到未授权库；审计事件齐全。
12. 删除：三次确认流程，且源记录/归属/字节不变。
13. 回归：全量测试保持 0 失败。

---

## 9. 工作量（**两部分分开**，均为估算）

### 甲、部署改造工作量（本文讨论的范围）

| 项 | 估算 |
|---|---|
| 集合基线（只读，落 JSON） | **0.5 h** |
| 统一 Compose：加 web/worker/scheduler，服务名寻址，专用 Redis DB，`KB_SKIP_DOTENV=1` | **2–4 h** |
| web 容器化：command（gunicorn）+ **healthcheck 覆盖** + `collectstatic` + 静态交付方案（whitenoise 或前端代理/静态卷）+ 权限 | **2–4 h** |
| 原件挂载区分（web 可写内容寻址 / worker 只读）+ 上传回归 | **1–2 h** |
| scheduler：真实推进判据（心跳/调度结果） | **1–2 h** |
| 验收（§8 的 13 项，含重启一致性与端到端） | **3–4 h** |
| **甲 小计** | **9.5–16.5 h ≈ 初估 1–2 个工作日**（**仅部署改造范围**；**不表示全系统完成**） |

存储方案 B（如获批）：需**另加数据准备与验证**（把现有索引搬进命名卷 + 逐文件校验 + 回退演练）→ **另加 1–2 h**（或选择以新代次重建，工作计入乙）。

> 甲的范围仅限"把现有系统在 Docker Compose 下统一跑起来并验证"；**乙才是功能完成度**。两者不可互相折算。

### 乙、完整系统剩余工作量（**不属于本文范围**，单独列出以便看清全局）

| 项 | 初步工作量 |
|---|---|
| `kb-9-g1` 等损坏索引重建（新代次，调用 AS711） | **0.5–2 h**（取决于模型速度） |
| 接线 `acks_late`/`reject_on_worker_lost` + 崩溃恢复验收（**崩溃恢复工时只在此行计入**） | **2–4 h** |
| AS711 并发闸门 / 超时 / 连接复用 / 小批嵌入 / 背压 | **4–8 h** |
| 看门狗 / 定时巡检的真实实现 | **4–8 h** |
| 前端渲染运行指标与探测延迟 | **2–3 h** |
| 重扫执行链路（复用既有重建能力 + 确认语义） | **3–6 h** |
| 独立审查清单剩余条目（派生文档来源校验与短事务、`search_knowledge` 结构化参数、`restart_stalled_task` 语义、`add_to_library`、`export_document` 下载、`create_library level`） | **1–3 天（初步）** —— 逐项待排程 |
| 熔断跨进程口径（若需要全局） | **2–6 h（初步）** |
| **乙 合计** | **尚未复核，不作为交付日期；待剩余功能逐项排程后提供** |

> 上表逐项均为**初步工作量**，仅供估算参考。**去重说明**：崩溃恢复**只在"接线 `acks_late`/`reject_on_worker_lost` + 崩溃恢复验收"这一行计入一次**；已从"审查清单剩余条目"中移除该项（其 Linux worker 崩溃恢复属于**同一次验收**，不重复计工时）。

> 所有数字均为**估算**，不是承诺；阶段甲的不确定性主要来自 Windows 上容器与宿主的路径/权限差异；阶段乙需**逐项排程后**才谈工期。

---

## 10. 已证实 vs 待验证

**已证实（本轮有命令证据）**
- 容器内 Qdrant 存储文件系统是 **9p**（bind 自 `D:\...\vector_data\qdrant`）。
- **`kb-9-g1` 查询返回 500**；`kb-7-g1`(0 点)、`kb-11-g1`(2 点) 正常。
- **Docker 命名卷是 ext4**（`ext4 /dev/sdd`）。
- **当前 PG 镜像没有 pgvector**（仅 `pg_trgm`/`unaccent`）。
- 现有向量 9 个集合 / 1865 文件 / **实占约 11 MB**。
- Web 目前在 Windows（8000）。
- **`acks_late` 未接线**。
- 原件写入是**内容寻址 + `.part` + `os.replace` 原子 + 复用**（`apps/knowledge/storage.py`）。
- 镜像 `COPY apps/` 已含 `apps/webui/templates` 与 `apps/webui/static`；`gunicorn`/`uvicorn` 在依赖内；`STATIC_ROOT` 不在镜像内且无 whitenoise。

**待验证（不当结论用）**
- 换到 ext4 后**其余 8 个集合**是否全部正常（`kb-9-g1` 已坏，属例外）。
- **重启持久性**是否确实改善（从未在 ext4 上验证）。
- "每次 500 都由 9p 造成"——**未证明**；官方文档只是高度吻合。
- 统一 compose 下 web 的资源占用、静态交付方式与启动顺序。
- 多容器下租约是否覆盖完备。
- §9 的两份工作量估算是否准确（甲、乙均未复核排程）。

---

## 11. 需要你决定的**唯一**一件事

**存储方案的约束取舍**：
- **A**：保持 `./vector_data/qdrant` 原地 → 满足"不搬动数据"，**仅作过渡**，不合格为最终交付；
- **B**：改用 Docker named volume → **面向可靠交付**，但需你同意**调整"向量数据必须落在项目根"这条约束**（数据改由 Docker 管理）。

**边界**：副本与冷备**默认保留**；`kb-9-g1` 等损坏索引的**重建属于既有系统目标内的工作**（需调用 AS711 接口），纳入 §9 乙推进，**不另设永久审批流程**。

> ⚠️ **本文的"默认保留／纳入推进"只表示"不需要为此单独请示"，不表示本轮开始执行。**
> **一切改动都在方案审阅通过之后才动手**；本轮（及审阅前）**不改任何代码、配置、服务与数据**。

---

## 12. 约束与前提（审阅补充）

1. **不引入 Kubernetes，不重写检索引擎**：优先**复用现有镜像与代码**（`kb-worker:local`、`docker-compose.yml`、既有 Qdrant/PG/Redis 与 `apps/knowledge/*`）。**不引入编排平台、不替换检索引擎**。
   - **静态资源优先评估"轻量进程内"方案**（目标：**少一个常驻容器**）；若评估不成立，再退回前端代理或静态卷。**选型实施前必须按官方文档确认**。
2. **新队列 / 新 Redis DB 必须先查占用**：确认目标 DB 未被其他用途占用后再选用，**不盲用示例 `/15`**；**web（生产者）／worker（消费者）／scheduler 三方必须使用同一套 broker/backend/队列配置**；**旧 DB0 不动、不消费、不清空**。
3. **部署保持数据基线与独立回退**：
   - 部署前先落**数据基线**（各在用库的活动代次／点数／维度／payload 抽样）；回退路径**独立可执行**（不依赖新栈）。
   - **用新代次修复时允许集合名改变**（如 `kb-9-g1` → `kb-9-g2`）：最终验收比较的是**所有在用库的"活动代次／点数／向量／payload"**，**不做"必须永远存在那 9 个旧名字"的死断言**。
   - **旧原件哈希必须保持不变**（`media/raw` 既有 blob 的 sha256 与字节不得变化；新增走内容寻址新增）。
