# 第一项（P1）完成记录

> 本文由实现方（AI）编写，供验收方（Codex / 用户）逐步核对。
> 所有数字都来自本机真实执行，命令可直接复制。执行目录：`D:/AProjectScript/KnowledgeBase`。
> 口径：**实测**写实测值，**未做**就写未做。这一节里没有任何预估数字。

---

## 1. 交付清单

### 1.1 应用与配置

| 文件 | 作用 |
|---|---|
| `manage.py` | Django 管理入口 |
| `config/settings.py` | 全部配置；环境变量优先（`.env`，`load_dotenv(override=True)`）；非 DEBUG + SQLite 直接拒绝启动 |
| `config/paths.py` | `vector_data/` 强制落在项目根；越界抛 `StoragePathError`；启动即创建并校验 db/media/vector_data |
| `config/urls.py` / `config/health_urls.py` | `api/v1/` 总路由；`/live/` 直接 200，`/ready/` 真执行 `SELECT 1` 并检查 vector_data |
| `config/asgi.py` / `config/wsgi.py` | ASGI 部署入口（需求 3：面向大并发） |
| `config/exceptions.py` | 统一 API 异常结构 |

### 1.2 三个业务 app

| app | 内容 |
|---|---|
| `apps/knowledge` | 信息库 / 成员 / 已入库资料 / 解析分段 / 向量集合登记；`services.py`（ingest、add_chunks、sha256）、`permissions.py`（库级角色）、`serializers.py`、`views.py`、`urls.py` |
| `apps/planner` | 会话 / 消息 / 操作记录 / 一次性凭证 / 派生文档；`operations.py` 登记 20 个意图（含风险级别与是否需要三次确认） |
| `apps/audit` | append-only 审计事件 + SHA-256 哈希链 + 固定链头 `AuditChainHead` |

### 1.3 不可篡改底座（`governance/`）

| 文件 | 层次 |
|---|---|
| `governance/registry.py` | 登记表 + `ImmutableManager`/`ImmutableQuerySet`（封死 update/delete/bulk_update/bulk_create/update_or_create）+ `privileged_channel()` 受控特权通道 |
| `governance/immutable.py` | `ImmutableRecordError` / `GuardedOperationError` / `immutable_endpoint` 装饰器 |
| `governance/middleware.py` | HTTP 层：受保护路径的 PUT/PATCH/DELETE → 403 + 审计事件 |
| `governance/triggers.py` | 数据库层：SQLite 与 PostgreSQL 各一份 `CREATE TRIGGER`，`kb_guard_*` 前缀，`guards_present()` 自检 |

### 1.4 向量维度 profile 体系（用户 m08374 / m08687 新增要求）

| 文件 | 内容 |
|---|---|
| `apps/knowledge/embedding.py` | `ProfileInfo` 注册表、`DEFAULT_PROFILE="bge1024"`、`probe_dimension()`（就绪的唯一来源）、`DimensionMismatch`、`ProfileNotReady` |
| `apps/knowledge/vectorindex.py` | `collection_name()`、`build_collection()`、`activate_collection()`、`switch_profile()`、`rollback_to()`、`BatchEmbedder` |

### 1.5 本地基础设施（用户 m08687：Docker 已真实可用）

| 文件 | 内容 |
|---|---|
| `docker-compose.yml` | 项目隔离的 Postgres 17.6 / Redis 7.4 / Qdrant v1.15.5，端口 5xxxx 段、只绑 127.0.0.1、三者带健康检查 |
| `.env.example` | 环境变量样例（与 compose 默认值对齐） |

### 1.6 迁移与文档

- 迁移：`apps/audit/migrations/0001…0003`、`apps/knowledge/migrations/0001…0003`（含 `0002_immutability_triggers`）、`apps/planner/migrations/0001`。
- 文档：`docs/architecture-review.md`（架构 + 决策记录 D1–D8 + 阶段拆分）、`docs/phase-1-completion.md`（本文）、`docs/codex-review.md`（审查方记录）。

---

## 2. 验收命令与实测输出

### 2.1 静态检查

```
.venv\Scripts\python.exe manage.py check
```
实测输出：
```
System check identified no issues (0 silenced).
```

### 2.2 起本地基础设施（项目隔离）

```
docker compose up -d
docker compose ps
```
实测输出（三行都是 healthy）：
```
knowledgebase-postgres  Up (healthy)  127.0.0.1:55432->5432/tcp
knowledgebase-qdrant    Up (healthy)  127.0.0.1:56333->6333/tcp, 127.0.0.1:56334->6334/tcp
knowledgebase-redis     Up (healthy)  127.0.0.1:56379->6379/tcp
```
连通性实测：`pg_isready` → `accepting connections`；`redis-cli ping` → `PONG`；`GET http://127.0.0.1:56333/collections` → `{"result":{"collections":[]},"status":"ok"}`。
镜像版本：`postgres:17.6-alpine3.22`、`redis:7.4-alpine`、`qdrant/qdrant:v1.15.5`（都固定版本，不用 `latest`）。

### 2.3 迁移到真实 PostgreSQL

`.env` 里 `DB_ENGINE=postgres`、`KB_PG_PORT=55432`，然后：
```
.venv\Scripts\python.exe manage.py migrate --no-input
```
实测输出（节选）：`contenttypes/auth/admin/audit.0001-0003/sessions/planner/knowledge.0001-0003` 全部 `OK`，其中 `knowledge.0002_immutability_triggers ... OK`。
迁移后 `showmigrations --plan` 无任何 `[ ]`（全部已应用）。

### 2.4 单元测试

```
.venv\Scripts\python.exe -m pytest -q
```
实测：**94 passed**。

| 测试文件 | 条数 | 覆盖 |
|---|---|---|
| `tests/test_api_basics.py` | 34 | 权限（匿名/普通/staff）、库可见性、筛选、详情越权 404、上传追加与去重、中间件 403 + 审计事件、集合只读、操作登记表 |
| `tests/test_audit_chain.py` | 8 | 哈希链、篡改/删除可被发现、固定链头并发 |
| `tests/test_immutability_orm.py` | 30 | 四层守卫逐层验证（登记表、ImmutableManager 类型、受控字段对齐、save/delete/queryset/bulk、触发器、状态流转、去重、ingest 幂等） |
| `tests/test_vector_profiles.py` | 22 | profile 注册、bge1024 激活、bge2048 未就绪即拒绝、维度不符拒绝且不污染索引、collection 命名/切换/回退、只读装饰器 |
| **合计** | **94** | — |

**同一套测试在真实 PostgreSQL 上也跑通**（同一命令，`DB_ENGINE=postgres`）：94 passed。

### 2.5 真实 HTTP 冒烟（ASGI/WSGI 起服务 + 真实数据库）

`.env` 指向 compose 的 PostgreSQL，起 `runserver` 后实测：

| 请求 | 实测响应 |
|---|---|
| `GET /api/v1/health/live/` | `200` `{"status":"ok"}` |
| `GET /api/v1/health/ready/` | `200` `{"status":"ok","checks":{"database":"ok","vector_data":"ok"}}` |
| `GET /api/v1/knowledge/libraries/`（匿名） | `403`（默认权限 `IsAuthenticated` 生效，不是 500） |

### 2.6 数据库触发器实测（PostgreSQL）

迁移 `knowledge.0002_immutability_triggers` 后，`pg_trigger` 里实测存在 6 条：
```
kb_guard_kb_ingested_file_update    kb_guard_kb_ingested_file_delete
kb_guard_kb_document_chunk_update   kb_guard_kb_document_chunk_delete
kb_guard_audit_auditevent_update    kb_guard_audit_auditevent_delete
```
（审计表用的是 Django 默认表名 `audit_auditevent`；触发器表名从 `AuditEvent._meta.db_table` 取，不写死字符串。）

---

## 3. 向量维度口径（本次新增的重点）

| 项 | 值 | 依据 |
|---|---|---|
| 当前 active profile | `bge1024`（`bge-large-zh-v1.5`，1024 维） | AS711 `:8000` 实测返回 1024 维 |
| 目标 profile | `bge2048`，**未激活** | 模型尚未部署；`ready` 只能由实测置位 |
| 不补零、不截断、不声称 | 已实现为硬拒绝：`DimensionMismatch(expected, actual)`，在使用前校验 | 单测 `test_probe_reports_not_ready_on_dimension_mismatch`、`test_dimension_mismatch_does_not_pollute_index` |
| 换模型流程 | 新 collection（带 model_version + dimension）→ 全量重嵌入 → 原子切换 → 旧 collection 保留可回退 | `apps/knowledge/vectorindex.py`；P1 已把状态机与拒绝路径跑通，真正写入是 P2 |

**明确没做的事**：P1 **没有**写入任何真实向量。`build_collection()` / `BatchEmbedder` 是 P2 的落点；P1 只保证"维度不对就进不去"这条不变式。

---

## 4. 未做项（避免误解为已完成）

| 项 | 计划阶段 | 说明 |
|---|---|---|
| 真正把向量写进 Qdrant | P2 | P1 只登记 collection 元数据并跑通切换状态机 |
| 解析（pdf/docx/xlsx/md）与 PaddleOCR 调用 | P2 | P1 只保留解析状态字段与流转 |
| 上传落盘原始字节 + 哈希校验 | P2 | P1 的 upload 只写元数据；`docs/codex-review.md` 已记录此限制 |
| 搜索（关键词 + 重排 + 信息库下拉） | P2 | P1 只有结构化筛选参数位 |
| AI 对话执行全部操作 | P3 | P1 已登记 20 个意图与风险级别 |
| 三次确认删除的完整流程 | P4 | P1 已有 `OperationRecord` 状态机、`ActionTicket` 模型与 `privileged_channel` 通道 |
| 派生文档生成/预览/下载 | P5 | P1 有 `DerivedDocument` 模型约定 |
| 并发压测与真实容量数字 | P6 | 见 `docs/architecture-review.md` §3 的诚实口径 |
| 前端壳接真实 API | P6 | `knowledge-base-prototype.html` 保持不动，作为 UI 事实基准 |

---

## 5. 需要验收方拍板的两点

1. **D8 的"未就绪即拒绝"** 是否满足"先兼容 1024、2048 预配置不激活"的要求（用户 m08687）。
2. **D3.1 的受控删除通道** 设计（`privileged_channel` + 白名单 + 必留审计）是否满足 `docs/codex-review.md` 里"生产请求路径严禁 DROP TRIGGER"的要求；完整三次确认流程在 P4 落地。
