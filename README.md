# 知识库系统（真实后端）

## 当前部署方式（唯一入口）

```bash
docker compose up -d          # 在**仓库根**执行；根 compose 是统一入口（include 引用 deploy/docker-compose.unified.yml）
```

一套 Compose 起全栈：**web（gunicorn，宿主 8800）· migrate（一次性迁移）· worker · scheduler ·
PostgreSQL · Redis · Qdrant**，服务间用服务名寻址。

**换机器 / 首次部署**（顺序有意义，逐条原因见 [部署与运维](docs/deployment-and-operations.md) §1.1）：

```bash
bash deploy/preflight.sh                      # 检查 .env、建 external 卷、绑定目录交给 uid 10001
bash deploy/build-app.sh                      # 两步建镜像（Windows：powershell -File deploy/build-app.ps1）
docker compose up -d                          # migrate 服务先成功跑完迁移，应用服务才会启动
docker compose exec web python manage.py kbcreateuser --username X --password Y --staff
curl -sS http://127.0.0.1:8800/api/v1/health/ready/       # 期望 200 且 checks.migrations == "ok"
```

预检会统一准备 `media/raw`、`media/templates` 与 `deploy/worker/beat-state` 的目录权限。
Linux 上不要省略这一步，否则原件或模板上传可能因 uid 10001 没有写权限而失败。

| 数据 | 位置 |
|---|---|
| **向量数据** | **Docker 命名卷** `knowledgebase-qdrantdata-delivery-20261015`（由 Docker 管理，**不在项目根**） |
| 原件（不可修改） | `media/raw`（web 可写=内容寻址新增；worker 只读） |
| PG / Redis | 既有命名卷 `knowledgebase-pgdata` / `knowledgebase-redisdata`（原样复用） |
| Celery | 专用队列 `kb_worker_v2` + 专用 Redis DB（`/14`）；**DB0 的历史消息不被消费** |

> **向量存储位置是 2026-10-15 用户明确授权的变更**：原「向量数据强制落在项目根 `vector_data/`」
> 的约束**已被该授权取代**。原因：旧的 `vector_data/qdrant` 是 Windows bind mount，
> Qdrant **重启后**会出现 payload 读取失败（`OutputTooSmall`）—— 对 id 9–15 共 7 个库重建新代次、
> 逐库验证通过后，仅重启 Qdrant 仍 **7/7 复现**。
> 旧目录 `vector_data/qdrant` **原样保留可回溯，但已不再被挂载**。详见 [交付状态与未解决风险](docs/delivery-status-2026-10-15.md)。

镜像构建（**一条命令可复现**，不依赖他人机器上的本地缓存）：

```powershell
pwsh -File deploy/build-app.ps1              # 有缓存基座则走交付路径，否则自动降级为标准从源码构建
pwsh -File deploy/build-app.ps1 -Standard    # 强制标准路径（完全从源码构建，需 PyPI）
```

Linux / macOS 用等价脚本：

```bash
bash deploy/build-app.sh                     # 标准路径：kb-worker:local-build → kb-app:delivery
```

> 交付路径固定了基座镜像的 **本地 image ID**（不是易变 tag，也不是 Registry digest）。
> 若本机没有该基座，脚本会自动改用标准路径按仓库 `deploy/worker/Dockerfile` 从源码构建，无需人工干预。
> 新机器上 compose 的 build 也需要一个存在的基座 tag：先跑上面的脚本，再 `export KB_BASE_IMAGE=kb-worker:local-build`。

---

> ⚠️ **以下关于 Windows bind mount / 独立 WSL(KnowledgeBase-Runtime)/`56339` 的迁移说明均为历史记录，已废弃。**
> 当前唯一支持的部署方式就是上面的 Docker Compose（命名卷）。历史迁移方案（`scripts/storage_migrate.py`、
> `scripts/phase2_storage.ps1`、独立 WSL 发行版等）**不再是部署入口**，仅作为回溯材料保留；
> **不要**再按它们操作，也不要再用 `vector_data/qdrant` 作为 Qdrant 存储。

> **当前状态（2026-10-15，独立审查）**：完整系统仍在开发与验收。真实 AS711 问答、指定来源文档生成及保存导出已获得证据；第一阶段独立回归 PG 350通过/1跳过、SQLite 338通过/13跳过。存储迁移、后台任务故障恢复、反馈学习、监控和部分 AI 操作仍待闭合。下文2026-10-14的交付数字与“唯一待补项”为历史快照，不能代表用户最新的完整目标。当前证据见 [恢复记录](docs/resume-2026-10-15.md) 和 [独立问题清单](docs/independent-open-issues-2026-10-15.md)。

Django 6.1 + DRF + PostgreSQL + Redis/Celery + Qdrant 的知识库后端，配套可浏览器验收的界面。

- 架构、决策记录、阶段拆分：`docs/architecture-review.md`
- P1 完成记录：`docs/phase-1-completion.md`
- P2 完成记录：`docs/phase-2-completion.md`（含逐条验证命令与实测输出）
- **P3–P7 交付清单与已知限制**：`docs/delivery-status.md`
- **独立验收记录（权威证据）**：`docs/acceptance-2026-10-14.md`
- **部署与运维**：`docs/deployment-and-operations.md`
- **接入 AS711 模型的手册（接模型当天按这个顺序做）**：`docs/model-onboarding-runbook.md`（一键体检：`deploy\model-readiness.ps1`）
- 审查方（Codex）记录：`docs/codex-review.md`

> **交付状态（2026-10-14）**：P1–P7 功能已实现，并已由审查方完成
> **独立功能验收与隔离并发验收**：
>
> - 独立 PG + 真实 Qdrant + 隔离真 worker：**296 passed / 1 skipped**（108.70s）
>   → `artifacts/acceptance/final-postgres.xml`（skip = AS711 实机用例）
> - SQLite 离线：**284 passed / 13 skipped**（23.43s）→ `artifacts/acceptance/final-sqlite.xml`
>   （skip 为真实基础设施与 PG 专项，**不计为通过**）
> - 隔离并发：新库首批上传 20/20、同库第二批 20/20、8 并发关键词检索 116/116；
>   8 并发确认 1×200+7×400、8 并发执行 1×200+7×409（后者拒绝是**预期**互斥，非可用率）
>
> 详见 `docs/acceptance-2026-10-14.md`（权威证据）与 `docs/delivery-status.md`。
>
> **唯一待补项：AS711 真实模型侧验收**（真实资料问答 + AI 生成文档）。
> 该链路**未完成复验**，且**不得用离线/降级结果替代**；
> 当前**不声明整个模型链路验收完成**。

---

## 1. 环境要求

| 项 | 本机实测版本 |
|---|---|
| Python | 3.13.5 |
| Docker Engine | 29.6.1 |
| Docker Compose | v5.3.0 |
| 操作系统 | Windows（命令按 PowerShell 写；Linux 下把 `.\\.venv\\Scripts\\python.exe` 换成 `./.venv/bin/python`） |

---

## 2. 从零启动（可直接复制）

### 2.1 建虚拟环境并装依赖

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip check
```

`requirements.txt` 里**所有版本都写死**；`pip check` 必须输出 `No broken requirements found.`。

### 2.2 起本地基础设施（项目隔离，不碰本机既有服务）

```powershell
docker compose up -d
docker compose ps
```

应当看到三个 `healthy`：`knowledgebase-postgres`（`127.0.0.1:55432`）、
`knowledgebase-redis`（`127.0.0.1:56379`）、`knowledgebase-qdrant`（`127.0.0.1:56333`）。
Qdrant 数据落在 **Docker 命名卷** `knowledgebase-qdrantdata-delivery-20261015`（由 Docker 管理，不在项目根）。
> 变更缘由（2026-10-15，用户批准）：原先的 `vector_data/qdrant` bind mount 在 Qdrant **重启后**会出现
> payload 读取失败（`OutputTooSmall`）；对 id 9–15 共 7 个库重建新代次并逐库验证通过后，仅重启 Qdrant 仍 **7/7 复现**，
> 因此改用命名卷并从原件重建。旧目录未删除、未拷贝。证据见 `docs/delivery-status-2026-10-15.md`。

### 2.3 配置环境变量

```powershell
Copy-Item .env.example .env
```

`.env` **不进仓库**。样例已按 compose 默认端口填好；口令只放 `.env`，代码与文档中不出现真实口令。

非 DEBUG 环境连 SQLite 会直接拒绝启动（SQLite 撑不住并发写），这是故意的。

### 2.4 迁移并自检

```powershell
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py guards            # 不可篡改触发器是否就位
.\.venv\Scripts\python.exe manage.py kbcheckinstall    # 在全新临时库上验证"从零安装"
```

### 2.5 建验收账号并起服务

```powershell
.\.venv\Scripts\python.exe manage.py kbcreateuser --username demo --password demo-pass-123 --staff
.\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8800
```

（生产口径用 ASGI：`.\.venv\Scripts\python.exe -m uvicorn config.asgi:application --host 127.0.0.1 --port 8800`）

> **宿主端口一律用 8800，不要用 8000。** Windows 的 WinNAT/Hyper-V 会动态预留成段的 TCP 端口，
> 落在预留段里的端口宿主**根本 bind 不了**（`WinError 10013`，docker 会报
> `ports are not available ... forbidden by its access permissions`，且换 `0.0.0.0`/`127.0.0.2`/`::1`
> 都没用）。实测 7984–8083 这一段吃掉过 8000。先自查：
> `netsh int ipv4 show excludedportrange protocol=tcp`。容器内始终是 8000，只有宿主映射可换。

### 2.6 浏览器验收入口

| 页面 | URL |
|---|---|
| 登录（**session 登录，浏览器不保存 Basic 口令**） | <http://127.0.0.1:8800/app/login/> |
| **助手首页**（AI 会话、操作提案确认、派生文档右侧预览/编辑/下载、清理与重试、使用说明） | <http://127.0.0.1:8800/app/assistant/> |
| 文件管理（上传单文件/**整文件夹**、状态、重试、来源只读） | <http://127.0.0.1:8800/app/files/> |
| 检索（**信息库下拉**） | <http://127.0.0.1:8800/app/search/> |
| 健康检查 | `/api/v1/health/live/`、`/api/v1/health/ready/` |

界面只是壳：所有数据都走 REST API，所以"界面能用"等价于"API 能用"。
会话/操作/清理/派生文档接口在 `/api/v1/planner/`：

| 接口 | 用途 |
|---|---|
| `GET /planner/guide/` | 使用说明 + 助手可用性（首页"使用说明"用它） |
| `GET /planner/catalog/` | 能力目录（与"我的操作记录"路径分开，避免遮蔽） |
| `POST /planner/chat/` | 会话：AI **只提案，不执行** |
| `POST /planner/operations/propose/` | 手动提案（页面点选操作） |
| `POST /planner/operations/<id>/confirm/` | 逐次确认；删除类第 3 次必须**准确输入目标名称** |
| `POST /planner/operations/<id>/execute/` | 执行；只接受已完成确认的操作 |
| `POST /planner/operations/<id>/abandon/` | **放弃待办**（首页工具栏「待办事项」的删除按钮）：置为「已放弃」、清掉执行窗口，**不执行任何改动**，操作记录按审计保留 |
| `POST /planner/operations/<id>/resume/` | 刷新后恢复当前步骤（旧凭证作废、重新签发） |
| `GET/POST /planner/deletions/` | 清理任务查询 / 重试（受权限保护） |
| `GET/POST/PUT /planner/documents/` | 派生文档：新建（写新文档）/ 列表 / 编辑 |
| `GET /planner/documents/<id>/download/?export_format=md\|html` | 导出真实 `.md` 或独立 `.html`（HTML 为**真实渲染**，与右侧预览同规则） |

> 参数是 `export_format`，**不是** `format`——后者是 DRF 的 renderer 协商参数，
> 会在进入视图前被拦掉（曾导致下载 404）。

### 2.7 模型服务（AS711）状态

| 事实 | 状态 |
|---|---|
| 四个模型**简单调用** | ✅ 曾成功（embedding 1024 维 / rerank 排序正确 / Qwen 正常回复 / OCR 正确识别文本图） |
| **真实 RAG + AI 生成文档** | ⚠️ **待补**：修复两 system 的 400 后曾出现 `EngineCore` HTTP 500；服务恢复窗口内**单次** RAG 形状请求已返回 200 且回答引用了来源，但**完整链路未跑完**（验收期间 AS711 再次断开） |

服务部署在 AS711 的 Docker 里（3 个 compose 项目：`deploy/qwen3.5`、`deploy/bge`、
`deploy/paddleocr`），且 **`RestartPolicy=no`** → **物理重启后不会自启**，
表现为"主机在线、端口拒绝连接"。需人工按原 compose 方式拉起。

切到真实模型：`KB_MODEL_CALLS_DISABLED=0` → `manage.py kbprobe --probe` 确认就绪
→ 再走真实上传/检索/会话。**不要把离线/降级输出当成真实模型结果。**

### 2.8 跑测试

```powershell
# 默认：SQLite，完全离线（不需要容器、不需要 AS711）
.\.venv\Scripts\python.exe -m pytest

# 强制用真实 PostgreSQL（独立命名测试库 kb_pytest_run），连不上会直接报错
.\.venv\Scripts\python.exe -m pytest --kb-db=postgres

# 真实 Qdrant server 用例（需要容器在跑）
.\.venv\Scripts\python.exe -m pytest -m real_qdrant --kb-qdrant

# 真实 AS711 用例（服务恢复后；当前断开，默认跳过）
.\.venv\Scripts\python.exe -m pytest -m real_as711
```

离线测试**禁止任何未 Mock 的外呼**（漏 Mock 会立刻 AssertionError，不会卡网等超时）。
要跑真实模型链路必须先设 `KB_TEST_ADAPTER=""`，否则会用确定性测试适配器。

---

## 3. 常用运维命令

```powershell
docker compose stop                 # 停服务（保留数据）
docker compose down                 # 删容器（保留卷与 vector_data）
docker compose down -v              # 连 Postgres/Redis 卷一起删（Qdrant 目录需手动删）

# 管理命令
.\.venv\Scripts\python.exe manage.py guards [--ensure] [--json]
.\.venv\Scripts\python.exe manage.py kbprobe [--probe] [--profile bge1024]
.\.venv\Scripts\python.exe manage.py kbcreateuser --username demo --password **** --staff
.\.venv\Scripts\python.exe manage.py kbprocess [--file N] [--retry-failed]
.\.venv\Scripts\python.exe manage.py kbrebuild --library <库code> [--all]
.\.venv\Scripts\python.exe manage.py kbcheckinstall
```

---

## 4. 处理模式与队列

| `KB_UPLOAD_PROCESS_MODE` | 行为 |
|---|---|
| `inline` | 请求内跑完整流水线（本地验收方便；远端模型掉线会阻塞请求） |
| `queue` | 落盘后立刻返回 **202 + task_id**，由 Celery worker 处理 |
| `auto`（默认） | 能派发就派发；派发不出去就**如实返回"待处理"**（不假装完成） |

- `CELERY_TASK_ALWAYS_EAGER` 生产默认 **False**；开启会让"队列"退化成请求内同步执行。
- 没有 worker 时用 `manage.py kbprocess` 在进程内推进；它只处理**租约空闲**的资料。
- 同一份资料由**数据库 CAS 租约**保证不会被两条流水线同时处理；租约过期可被接管。

---

## 5. 关键配置（详见 `docs/phase-2-completion.md` §6）

| 变量 | 默认 | 说明 |
|---|---|---|
| `KB_TEST_ADAPTER` | 空（关闭） | 仅 `offline` 时启用确定性假向量，且只允许写 `__offline_test` collection |
| `KB_PROCESS_LEASE_SECONDS` | `900` | 处理租约时长（长任务续租；不复活过期 token） |
| `KB_SEARCH_USE_RERANK` | `1` | 关闭时只用 RRF 融合结果 |
| `KB_OCR_MAX_PAGES` | `20` | 单份 PDF 最多 OCR 页数，超过报错中止（不静默丢页） |
| `KB_SERVICE_FAILURE_THRESHOLD` / `KB_SERVICE_COOLDOWN_SECONDS` | `3` / `60` | 外呼熔断 |
| `KB_UPLOAD_MAX_BYTES` | `200MB` | 上传上限 |

---

## 6. 目录结构

```
config/             Django 配置、ASGI/WSGI、路径强校验、健康检查、Celery app
governance/         不可篡改底座：登记表、ORM 守卫、HTTP 中间件、数据库触发器
apps/audit/         append-only 审计事件 + SHA-256 哈希链 + 固定链头
apps/knowledge/     信息库、已入库资料、解析分段、存储/解析/嵌入/检索/向量索引
apps/planner/       会话、消息、操作记录、一次性凭证、派生文档、意图登记表
apps/webui/         浏览器验收界面（登录、文件管理、检索；继承原型灰白风格）
vector_data/        ★ 向量数据（qdrant server 模式 / embedded 本地模式）
media/raw/          ★ 已入库资料的原始字节（内容寻址、只读）
db/                 运行期目录（SQLite 文件）
docs/               架构、阶段完成记录、审查记录
tests/              pytest 测试包（含离线与真实依赖标记）
```

---

## 测试资源自动清理（2026-10-15）

**默认行为：测试与压测产生的资源会被自动回收，不需要人工收拾。**

为什么要做：历史上测试/压测直接在主实例上建集合又不清理，累积出
191 个集合（其中 125 个 `kb-lt-*`、约 42 GiB）。这些历史集合**不在**自动删除
范围内 —— 需要人工决策后才能动。

### 机制

* **每次 run 独占前缀**：`kb-<run_uuid>-`（集合名 = 前缀 + `<库id>-g<代次>`）。
  清理**只**按这个前缀精确匹配（**带尾连字符**，不会误中另一个更长 UUID）；
  绝不按 `kb-*` / `kb-lt-*` 模糊扫删。
* **先登记后创建**：`declare`（意图）→ 创建 → `confirm`（创建成功）；
  清理**只**删本 run 清单里 `created` 且 endpoint/identity 都匹配的资源。
* **跨进程锁判活**：run 创建时独占持有 `run.lock`；判活靠锁而不是心跳
  （长测试不 beat 不会被误判为死；被杀进程留下的新鲜心跳也不会被误判为活）。
  **活跃 run 一律拒绝清理**，`--force` 也不能绕过；CLI 同样不能绕过。
* **删除前审计**：前缀清点会把"即将删除的精确集合名 + 目标 endpoint"先写进
  本 run 清单，再执行删除；并协调"已 declare 但死在 confirm 前、实际已建出来"
  的资源（补登记后删除，不静默跳过）。
* **边界与链接**：所有删除先解析**真实绝对路径**并校验落在本 run 目录内，
  逐项拒绝链接/junction；只读（不可变）原件会先清只读位再删（Windows 上
  直接 `rmtree` 只读文件会 `WinError 5`）。
* **保留小报告**：`artifacts/test-runs/<run_uuid>/` 下的 `manifest.json`、
  `cleanup-report.json`、`heartbeat`、`run.lock` 会保留（小型审计）；
  只清 `raw/`、`vector_data/` 等大数据与本 run 的向量集合。

### 入口

| 场景 | 命令 |
|---|---|
| 测试会话 | `pytest`（会话级 finally 自动清理；清理失败会让 session **失败**） |
| 遗留 run 恢复 | `.\.venv\Scripts\python.exe scripts/run_resources.py list` / `recover-all` |
| 压测（独占 DB/服务/raw/集合全生命周期） | `.\.venv\Scripts\python.exe scripts/loadtest_runner.py --dry-run` 然后 `--execute` |
| 真实"故意失败仍清理"演练 | `.\.venv\Scripts\python.exe scripts/cleanup_failure_drill.py` |
| 完整验收步骤 | [`docs/cleanup-acceptance-steps.md`](docs/cleanup-acceptance-steps.md) |

**默认只读**：`loadtest_runner.py` 与存储迁移脚本默认 `--dry-run` / 只读，
必须显式 `--execute` 才会做变更；清理失败一律**非零退出并列出残留项**，
不会只报"向量已删"就算全清。
