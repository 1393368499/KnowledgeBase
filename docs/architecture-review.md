# 知识库真实系统 · 架构与阶段计划（评审用）

> 本文档由实现方（AI）编写，供 Codex 逐项审查。
> 所有结论都来自**实测**：AS711 模型服务的探测数据全部是本次真实调用得到，未编造。
> 文件路径均相对项目根目录 `D:/AProjectScript/KnowledgeBase`。

---

## 0. 文档索引

| 文件 | 内容 |
|---|---|
| `docs/architecture-review.md` | 本文：架构、决策记录、阶段拆分、验收方式 |
| `docs/phase-1-completion.md` | 第一项完成记录：交付清单、命令、实测输出、未做项 |
| `docs/codex-review.md` | 审查方（Codex）独立验证记录与待修项 |
| `docker-compose.yml` | 项目隔离的 Postgres / Redis / Qdrant |
| `.env.example` | 环境变量样例（含本地 compose 的默认端口） |

---

## 1. 需求 ↔ 设计对照

| # | 用户需求 | 设计落点 | 阶段 |
|---|---|---|---|
| 1 | Python Django | Django 6.1 + DRF，ASGI 部署 | P1 |
| 2 | 向量数据必须在项目根目录 | `knowledgebase/paths.py` 强制解析到 `<项目根>/data/vector_data`，settings 启动即校验；Qdrant server 用 bind mount（`docker-compose.yml`），本地嵌入模式说明并发限制 | P1（目录与校验 + 容器挂载）/ P2（真正写入） |
| 3 | 受支持技术、面向大并发、必须实测容量 | ASGI + PostgreSQL + Redis + Celery 分层；项目隔离 compose 起真实 Postgres/Redis/Qdrant；容量数字标注"实测/预估"两类，绝不虚报 | P1（骨架 + 真实组件连通）/ P6（压测） |
| 4 | 用 AS711 上的 4 个模型 | 全部实测确认可用（见 §3），只读探测，未改动任何服务 | P1（连通性探测）/ P2 起真正调用 |
| 5 | 搜索加信息库下拉筛选 | 搜索 API 以 `library` 为结构化筛选参数之一，与关键词/部门/客户/金额并列 | P2 |
| 6 | 首页系统使用说明 | 前端首页 + `/api/v1/guide/` 落地 | P6（前端）/ P1（只写文档） |
| 7 | AI 对话可执行全部操作但不能改已入库信息 | 双层强制：`ingested_file` 表结构层面无"可编辑正文"入口 + `governance` 写入守卫 + DRF 权限类；不依赖提示词 | P1（底层与权限）/ P3（对话执行） |
| 8 | 删除需三次独立弹窗、第 3 次输名称、服务端逐步校验、一次性凭证防重放 | `OperationRecord` 状态机 + `ActionTicket`（绑定对象+会话+过期+单次消费） | P1（模型与不变式）/ P4（完整流程） |
| 9 | AI 基于全库写新文档、右侧预览编辑下载、不覆盖来源 | 派生文档独立表 `DerivedDocument`，来源只读引用；草稿区与入库区物理分离 | P1（模型约定）/ P5 |
| 10 | 允许追加需求 | 模块边界 + 决策记录 + 扩展点清单 | 全阶段 |

---

## 2. 运行时架构

```
                       ┌───────────────────────────────┐
   浏览器 (前端壳)  ──►  │  ASGI (uvicorn / daphne)      │
                       │  knowledgebase.asgi                  │
                       └───────────┬───────────────────┘
                                   │
          ┌────────────────────────┼─────────────────────────┐
          ▼                        ▼                         ▼
   governance            Django apps                 knowledgebase.paths
   （中间件+守卫）   knowledge / planner / audit   （路径强校验）
          │                        │                         │
          │              ┌─────────┴─────────┐               │
          │              ▼                   ▼               ▼
          │        PostgreSQL          Redis / Celery   vector_data/（根目录）
          │        （权威数据）        （缓存/队列）      ├─ qdrant/（server 模式）
          │            ▲                                 └─ embedded/（本地模式）
          │            │ 触发器 DDL（不可篡改第四层）          ▲
          │            └── docker compose（隔离端口 5xxxx）   │ bind mount
          │                                            qdrant/qdrant:v1.15.5
          └──────────────────────── 审计链（append-only，不可改删） ────┘

      外部模型服务（AS711，只读调用）
      10001 Qwen3.6-35B-A3B-FP8 │ 8000 bge-large-zh-v1.5（实测 1024 维，active）
       8002 bge-reranker-large  │ 8003 PaddleOCR-VL-1.5
      目标 2048 维模型：已预配置但**未激活**（见 D8）
```

### 2.1 分层与职责

| 层 | 位置 | 职责 |
|---|---|---|
| 路径与运行目录 | `knowledgebase/paths.py` | 保证 `vector_data/` 落在项目根；提供 collection 子目录；启动即校验 |
| 不可篡改底座 | `governance/`（`immutable.py` + `registry.py` + `middleware.py`） | 登记受保护资源；ORM 层拦截改删；HTTP 层拦截改删；登记变更事件 |
| 领域数据 | `apps/knowledge` | 信息库、成员、已入库资料、解析分段、向量集合登记 |
| 会话与操作 | `apps/planner` | 会话、消息、AI 提议、操作记录、一次性凭证 |
| 审计 | `apps/audit` | append-only 事件表 + SHA-256 哈希链 |

### 2.2 关键决策记录（Decision Records）

**D1 · 数据库：PostgreSQL 为主，SQLite 仅本地/测试回退**
开发机上已用**项目隔离的 Docker Compose** 跑起真实 PostgreSQL（见 D7），所以"本机没装 Postgres"不再是阻塞项：`DB_ENGINE=postgres` 连容器，`sqlite` 用 `db/kb.sqlite3`（单测默认）。
**生产必须 PostgreSQL**：审计表行级触发器、`select_for_update` 并发语义、连接池都依赖它。SQLite 仅用于本地起步与单测。
settings 里加了硬口径：**非 DEBUG 环境连着 SQLite 直接拒绝启动**（`ImproperlyConfigured`），而不是"跑起来了但不可用"。

**D2 · 向量库：本地目录 + 双模式**
- 生产：`qdrant/qdrant` 容器，`./data/vector_data/qdrant:/qdrant/storage`，路径固定在项目根。
- 本地：`qdrant-client` 嵌入模式（`QdrantClient(path=...)`）。
**本地嵌入模式的并发限制必须写明**：嵌入模式底层是本地文件锁，同一时刻**只允许一个进程**持有；多 worker（多 uvicorn worker / 多 Celery worker）会抢锁报错。故本地模式仅限单进程开发，任何多进程/多实例部署都必须切到 Qdrant server。

**D3 · 不可篡改在四个层次同时强制（不靠提示词）**
1. 数据模型：已入库资料不存储"可编辑正文"，只有解析出的 `DocumentChunk`（派生、只读）；
2. ORM 层：`ImmutableQuerySet` 封死 `update/delete/bulk_update/bulk_create/update_or_create`，实例 `save/delete` 对登记模型抛 `ImmutableRecordError`——包括 `_base_manager` 这条路；
3. 数据库层（**PostgreSQL 与 SQLite 各写一份 `CREATE TRIGGER`**）：`kb_guard_<table>_update` / `_delete` 触发器直接 `RAISE EXCEPTION` / `RAISE(ABORT)`。`kb_ingested_file` 的 UPDATE 只锁"非受控列"（受控列来自 `CONTROLLED_MUTABLE_FIELDS` 这个唯一真源，所以登记表放行的字段触发器一定也放行，不会漂移）；`kb_document_chunk` 与审计表一律禁止 UPDATE/DELETE。
4. HTTP 层：`ImmutabilityGuardMiddleware` 对受保护路径的 `PUT/PATCH/DELETE` 直接 403（`code=immutable_resource`）并写 `immutable.write_blocked` 审计事件。

四层的单元测试各自独立，任意一层被绕过，其余三层仍会拦。

**D3.1 · 受控删除通道（唯一合法例外）**
删库/删资料**不能**通过"DROP TRIGGER"或"全局禁用触发器"来实现——那会给并发请求开一个改数据的窗口。唯一入口是 `governance.registry.privileged_channel()`（进程内 contextvar），它要求：
- `action` 必须来自白名单 `PARTIAL_DELETE_ACTIONS`（不能当通用后门）；
- 必须给出 ≥4 个字的人类可读理由；
- 每次打开都生成一次性 token，并**由服务层写审计事件**。

P4 的删除流程将把这个通道绑定到"三次确认 + 一次性凭证（对象 id + actor + 会话 + 过期 + 防重放）"，且只放行 DELETE，UPDATE 与审计守卫全程有效。`governance/triggers.py` 的 `drop_guards`/`create_guards` 只是**迁移与测试工具**，不会暴露成 AI 可调用的 operation。

**D3.2 · 审计链并发：固定链头锁**
`select_for_update` 锁不住"还不存在的行"：空表或并发第一条会让两个写入方都拿到 GENESIS_HASH，链分叉。所以用只有一行的 `kb_audit_chain_head`（`AuditChainHead`，`SINGLETON_ID=1`）当固定锁对象，`get_or_create` + `select_for_update` 让所有追加方排队。`verify_chain()` 逐条重算，能定位分叉点。

**D4 · 删除凭证：一次性、绑定对象与会话、可过期、防重放**
`ActionTicket` 存 `token_digest`（不存明文）、`resource_key`、`conversation_key`、`scope`（confirm1/confirm2/delete）、`expires_at`、`consumed_at`、`superseded_by`。
消费走 `id + digest + scope + 未过期 + 未消费` 的条件更新，命中行数必须为 1 才放行——并发重放只能有一路成功。
对象名比对（第 3 次输入的名称）在服务端做，且失败会累加 `attempts`，超限即作废。

**D5 · 审计链防篡改**
每条事件 `prev_hash` + `entry_hash`，`entry_hash = sha256(prev_hash ∥ 规范化字段 ∥ payload)`。终端提供 `verify_chain()` 逐条重算；Append-only 通过 ORM 守卫 + PostgreSQL 触发器双保险。

**D6 · 中文口径沿用原型**
`信息库`（=信息库）、`资料`（=文件）、`目录`（=目录路径）等词与 `knowledge-base-prototype.html` 一致；原型文件保留不动，作为 UI 事实基准。

**D7 · 本地基础设施：项目隔离的 compose，不碰宿主机既有服务**
`docker-compose.yml` 固定 `name: knowledgebase`，容器/网络/卷都带该前缀；宿主机端口全部走 5xxxx 段（Postgres 55432 / Redis 56379 / Qdrant HTTP 56333、gRPC 56334），且只绑 `127.0.0.1`。
Postgres、Redis 的数据放命名卷（`knowledgebase-pgdata` / `knowledgebase-redisdata`，都是本机开发数据）；**Qdrant 用 bind mount `./data/vector_data/qdrant:/qdrant/storage`**——因为需求硬性要求向量数据落在项目根目录内，容器重建也不该丢。
镜像版本固定（不写 `latest`）：`postgres:17.6-alpine3.22`、`redis:7.4-alpine`、`qdrant/qdrant:v1.15.5`；三者都有健康检查，`docker compose ps` 直接看得到 `healthy`。
环境变量两套前缀都认：`KB_PG_*` / `KB_REDIS_*` / `KB_QDRANT_*`（项目自己的名字，compose 也用它做容器插值）优先，`POSTGRES_*` / `REDIS_URL` / `QDRANT_URL` 作为兼容回退；`.env` 由 `load_dotenv(..., override=True)` 加载，优先级最高。

**D8 · 向量维度：维度是"配置 + 实测"的事实，不是可以声明的数字（用户 m08374 / m08687）**
- 维度、模型名、版本、端点全部配置化（`knowledgebase/settings.py` 的 `MODEL_SERVICES` + `apps/knowledge/embedding.py` 的 profile 注册表）。
- **active profile 明确固定为 `bge1024`**（bge-large-zh-v1.5，实测 1024 维）。
- 目标 2048 维预配置为 `bge2048` 但**不激活**：任何嵌入请求在没有实测证据前一律被拒（`ProfileNotReady`），`ready` 只能由 `probe_dimension()` 的实测结果置位。
- **绝不允许**补零、截断，或把 1024 维模型说成 2048 维。服务返回维度 ≠ profile 声明维度时抛 `DimensionMismatch`，**在使用前校验**，拒绝写入、不污染索引；探测报告里同时带上 `expected` / `actual` 便于定位模型版本。
- 换模型必须走"新 collection → 全量重嵌入 → 原子切换 → 旧索引保留可回退"（`apps/knowledge/vectorindex.py`，P2 起真正执行；P1 已把状态机与拒绝路径跑通）。

---

## 3. AS711 实测基线（本次真实探测）

服务器：`172.16.100.114`（别名 `as711`），Ubuntu 6.8，Hygon C86-4G × 32 核，内存 251 GB，`/data` 3.5 TB。
加速卡为**国产 TPU（sophgo/vllm-tpu 镜像）**，`nvidia-smi` 不存在；4 个 vLLM 容器均 `Up`，分别映射到不同端口。

| 端点 | 模型 | `/v1/models` | 实测结果 |
|---|---|---|---|
| `:10001` | Qwen3.6-35B-A3B-FP8 | ✅ | `max_model_len=262144`；关闭思考后 206 token / 6.05 s ≈ **34 tok/s**（单路）；支持 OpenAI `tools` 工具调用，返回规范 `tool_calls`；**默认开启思考，必须传 `chat_template_kwargs.enable_thinking=false`** 否则短输出会被思考吃光 |
| `:8000` | bge-large-zh-v1.5 | ✅ | `/v1/embeddings` 可用，**1024 维**，21 token 中文句 485 ms |
| `:8002` | bge-reranker-large | ✅ | `/v1/rerank` 可用，`relevance_score` 判别力正常（0.8984 / 0.0009 / 0.0005），3 文档 103 ms |
| `:8003` | PaddleOCR-VL-1.5 | ✅ | `/v1/chat/completions` 走 `image_url` base64，实测识别出图中英文字符串，0.5 s；容器参数 `--max-num-seqs 16` |

四个端口从本机（开发机）**均可达**（TCP 10001/8000/8002/8003 全通）。
> 全程只读：只做了 `GET /v1/models`、`GET /health`、`docker ps`、`docker inspect` 与少量推理调用，**未停止、未修改任何服务**。
> 连接资料只在内存中使用，不写入仓库；仓库内仅出现主机与端口，**不含任何口令**（见 `docs` 与 `.gitignore`）。

**容量口径（诚实版）**
- 已实测：单路生成 ≈ 34 tok/s；嵌入 ≈ 0.5 s/句；重排 ≈ 0.1 s/3 文档；OCR ≈ 0.5 s/图。
- 未实测（后续压测才能给数）：并发吞吐、PostgreSQL 写入 TPS、Celery 队列吞吐、检索 QPS。
- 已知硬上限：OCR 容器 `--max-num-seqs 16`；Qwen 走 TP-1 单容器，生成侧是**共享瓶颈**，不能按"32 核 × N"线性放大。
- 结论：本系统在"大并发"上的诚实表述是——**请求接入层可横向扩展（ASGI 无状态），推理层受 AS711 单容器能力限制**；压测（P6）会把真实曲线写进文档，不在本文预填数字。

---

## 4. 数据模型总览（P1 全部落地，表名为实测输出）

> 下表 `表` 一列来自 `apps.get_app_config(...).get_models()` 的真实输出，不是手写的。

| 模型 | 表 | 关键字段 | 不变式 |
|---|---|---|---|
| `KnowledgeLibrary` | `kb_knowledge_library` | code, name, level(internal/external), description, department, customer, vector_collection, is_active | code 全局唯一 |
| `LibraryMember` | `kb_library_member` | library, user, role(reader/writer/admin), department | (library,user) 唯一 |
| `IngestedFile` | `kb_ingested_file` | library, original_name, content_sha256, size_bytes, parse_status, chunk_count, customer, department, classification, vector_point_id | **(library,content_sha256) 唯一 → 去重**；无正文可写字段；非受控列被数据库触发器锁死 |
| `DocumentChunk` | `kb_document_chunk` | file, ordinal, text, permission_code, token_estimate | (file,ordinal) 唯一；append-only，随 file 级联删除 |
| `VectorCollection` | `kb_vector_collection` | name, dimension, embedding_model, model_version, profile, library, storage_mode, is_active | name 唯一；`is_active` 表达"当前生效的索引" |
| `Conversation` | `planner_conversation` | user, title, source | — |
| `Message` | `planner_message` | conversation, role, content, intent, payload | — |
| `OperationRecord` | `planner_operationrecord` | intent, risk, status(proposed…armed/executed/rejected/expired/failed), params, target_ref, confirmations, required_confirmations | 提议 ≠ 执行 |
| `ActionTicket` | `planner_actionticket` | token_hash(unique), scope, intent, object_ref, consumed_at, consumed_count, expires_at | 单次消费、防重放；库里只存摘要 |
| `DerivedDocument` | `planner_deriveddocument` | title, topic, content_markdown, status, sources(m2m), libraries(m2m) | 只引用来源，不改来源 |
| `AuditEvent` | `audit_auditevent` | action, actor, resource_kind/key, payload, prev_hash, entry_hash | **append-only**，改删即报错；ORM + 数据库触发器双保险 |
| `AuditChainHead` | `kb_audit_chain_head` | id(=1), last_event | 单行固定链头，给审计链追加当锁 |

---

## 5. 追加需求的扩展点

- 新操作：在 `apps/planner/operations.py` 注册 intent（名称、参数 schema、风险级别、是否需凭证、是否需要第 3 次名称确认），API 与凭证流程自动适配。
- 新检索器：`apps/knowledge/retrieval/` 下新增 provider，注册到策略表即可参与混合检索。
- 新数据源：`api` 适配层（`apps/knowledge/sources/`）实现 `list/parse` 两个方法。
- 新前端页面：沿用原型 `#/`、`#/files`… 路由，逐步换成真实 API 调用。

---

## 6. 阶段拆分与验收

| 阶段 | 内容 | 验收方式 | 状态 |
|---|---|---|---|
| **P1** | Django 骨架、配置、数据模型、权限与不可篡改底层（ORM + 数据库触发器 + HTTP 三层）、向量维度 profile 体系、基础 API、单元测试、docs | `pytest` 全绿（SQLite 与真实 PostgreSQL 各跑一遍）；`check`/`migrate` 无 issue；健康检查与资料列表返回真实 JSON | **本次交付** |
| P2 | vector_data 落地、Qdrant 双模式、解析（pdf/docx/xlsx/md）+ PaddleOCR、嵌入、重排、混合检索、搜索信息库下拉筛选 | 真实上传→解析→入库→检索命中；检索延迟实测 | 待评审后开工 |
| P3 | AI 对话执行全部操作（工具调用 + 后端强制权限），改动被拒的实测证据 | 用"改已入库资料"的指令验证后端硬拒 | 待 |
| P4 | 三次弹窗删除流程 + 凭证逐步校验 + 重放/过期/并发攻击测试 | 攻击测试用例（重放、换对象、超时、并发双发）全被拒 | 待 |
| P5 | 全库检索写新文档、右侧预览/编辑/下载、来源只读 | 派生文档不覆盖来源的验证 | 待 |
| P6 | 并发压测与真实容量数字、缓存/队列调优、前端壳接真实 API、系统使用说明页 | 压测报告写进 docs | 待 |
| P7 | 设计文档与扩展指南收口 | 文档评审 | 待 |

---

## 7. 第一项（P1）验收指引

见 `docs/phase-1-completion.md`，其中有可直接复制的命令与本次实测输出。
