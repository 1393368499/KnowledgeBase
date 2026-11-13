# 第二项（P2）完成记录

> 本文由实现方（AI）编写，供验收方（Codex / 用户）逐条核对。
> 执行目录：`D:/AProjectScript/KnowledgeBase`；所有命令可直接复制。
> 口径：**实测**写实测值；**未做**就写未做；**待恢复**单独列出，绝不写成已完成。

---

## 0. 一句话结论

P2 的**离线可验证部分全部完成**：真实文件按内容寻址落盘（只读、不可覆盖）、
按格式真解析、真实向量索引（Qdrant）与混合检索、信息库下拉筛选贯穿权限、
浏览器界面（session 登录）可跑通"建库 → 首上传 → 处理 → 检索"。
**真实 AS711 模型验证待服务恢复**（当前断开，按用户指示不探测），
设备前提与命令已列在 §7。

---

## 1. 交付清单

### 1.1 依赖与启动

| 文件 | 说明 |
|---|---|
| `requirements.txt` | **全部固定版本**，`pip install -r requirements.txt` 即可；`pip check` 干净 |
| `README.md` | 从零启动、compose、迁移、管理命令、浏览器入口、测试命令 |
| `knowledgebase/test_settings.py` | 测试专用配置：默认 SQLite（离线确定），`--kb-db=postgres` 用独立命名库 |
| `knowledgebase/install_check_settings.py` | "从零安装"校验专用（强制 SQLite，不被 .env 覆盖） |
| `knowledgebase/celery.py` | Celery app + 入库任务 + 派发函数（queue/inline/auto 三种口径） |

### 1.2 存储与解析

| 文件 | 说明 |
|---|---|
| `apps/knowledge/storage.py` | 内容寻址（`data/media/raw/<sha2>/<sha256>`）、写 `.part` → 校验 → 原子改名 → 只读；已存在则复用，内容不符抛 `BlobCorrupt`（**绝不覆盖**） |
| `apps/knowledge/parsers.py` | PDF（文本层 + 扫描页 OCR）、DOCX、XLSX、TXT、MD、CSV；不支持格式显式报错；扫描页超上限**报错中止**（不静默丢页）；空工作簿显式报错 |
| `apps/knowledge/ocr.py` | AS711 PaddleOCR-VL 客户端（OpenAI 兼容 `/chat/completions`，图片 base64） |
| `apps/knowledge/rerank.py` | AS711 bge-reranker-large 客户端（`/rerank`） |
| `apps/knowledge/resilience.py` | 超时 / 重试上限 / **熔断**（半开只放一个探测）/ `ServiceUnavailable` 明确不可用状态 |
| `apps/knowledge/httpclient.py` | 统一外呼客户端（`trust_env=False`，宿主畸形 `NO_PROXY` 不再打挂外呼） |
| `apps/knowledge/testing_adapter.py` | **显式**离线测试适配器：默认关闭；只允许写 `__offline_test` 后缀 collection；payload 带 `is_test_adapter` |

### 1.3 向量与检索

| 文件 | 说明 |
|---|---|
| `apps/knowledge/vectorindex.py` | generation 化 collection（`<prefix>-<库id>-g<n>`）、真实 upsert、稠密/关键词检索、RRF 融合、**严格单调发号**、真实 points coverage 校验、原子切换与回退 |
| `apps/knowledge/pipeline.py` | 流水线：解析 → 分段（**事务原子 + 按内容幂等**）→ 嵌入 → 写索引；**数据库 CAS 租约**（跨 worker 互斥、可过期接管、续租不复活过期 token）；库索引初建/重建；混合检索 + **数据库关键词兜底** |
| `apps/knowledge/views.py` | 上传（202 队列口径 + 自动初建索引）、重试、检索（信息库下拉）、重建 API、索引统计、模型服务只读探测 |

### 1.4 浏览器界面（`apps/webui/`）

| 文件 | 说明 |
|---|---|
| `views.py` / `urls.py` | session 登录、文件管理页、检索页 |
| `templates/webui/*.html` | 继承 `knowledge-base-prototype.html` 的灰白极简风格（同一套 CSS 变量/边框/圆角/字号） |
| `static/webui/kb-common.js` | 会话感知 fetch（401 跳登录）、信息库下拉（只列可读库）、状态标签 |
| `static/webui/kb-files.js` | 上传**单文件/整文件夹**、状态与重试、来源只读、接口自检 |
| `static/webui/kb-search.js` | 查询 + **信息库下拉筛选**、命中详情、降级提示 |

### 1.5 管理命令

| 命令 | 用途 |
|---|---|
| `manage.py guards [--ensure] [--json]` | 不可篡改触发器自检/补建 |
| `manage.py kbprobe [--probe]` | 模型服务**只读**探测（默认不发请求，`--probe` 才调用） |
| `manage.py kbcreateuser --username ... --password ... [--staff] [--library code --role reader]` | 建验收账号（口令不落日志） |
| `manage.py kbprocess [--file N] [--retry-failed]` | 进程内推进待处理资料（无 worker 时的推进方式；只捞租约空闲的） |
| `manage.py kbrebuild --library code [--all]` | 索引初建/重建（新代次 → 全量重建 → 校验 → 原子激活） |
| `manage.py kbcheckinstall` | 在**全新临时库**上跑完整迁移链，验证"从零安装"与触发器就位 |

---

## 2. 验收命令与实测输出

### 2.1 静态检查与依赖

```
.venv\Scripts\python.exe manage.py check
.venv\Scripts\python.exe -m pip check
.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
```
实测：
```
System check identified no issues (0 silenced).
No broken requirements found.
No changes detected
```

### 2.2 从零安装（真实缺陷回归）

```
.venv\Scripts\python.exe manage.py kbcheckinstall
```
实测：
```
从零安装迁移链跑通
  临时库：C:\Users\...\kb-install-check-xxxx\fresh.sqlite3
  真实后端（connection.vendor）：sqlite
  不可篡改触发器就位：True
```
> 这条命令是**为修一个真实缺陷**加的：迁移曾用"运行时模型"的列去建触发器，
> 导致全新 SQLite 库在后续 `ALTER TABLE` 时报
> `no such column: OLD.<未来的列>`。现在 0002 冻结历史列、0007 在列齐全后按新列刷新。

### 2.3 单元 + 集成测试

```
.venv\Scripts\python.exe -m pytest tests --kb-db=sqlite
.venv\Scripts\python.exe -m pytest tests --kb-db=postgres
```
实测（两个后端一致）：
```
200 passed, 3 skipped, 1 warning
```
跳过的 3 条是 `real_as711` / `real_qdrant` / `real_broker` 标记
（需要真实模型服务 / 真实 Qdrant server / 真实 Redis broker）。
`real_broker` 用 `--kb-broker` 启用（Redis 在跑时）。

`--kb-db=postgres` 用**独立命名测试库**（`kb_pytest_run`，Django 再加 `test_` 前缀），
不与其它会话争用 `test_kb`；`--kb-db=postgres` 时若连不上会**直接报错**，
不会静默降级成 SQLite 还声称在 Postgres 上验证过。

### 2.4 离线测试禁止真实外呼

`tests/conftest.py` 有 autouse 守卫：非 `real_as711` 用例的任何未 Mock 外呼
会立刻 `AssertionError`，避免"漏 Mock 导致测试卡网等超时"。

### 2.5 首上传全链路（真实 multipart + 自动初建索引）

`tests/test_first_upload_flow.py` + `tests/test_upload_api.py` 覆盖：

- 全新库（`active_generation=0`、无任何 collection）→ 首上传 → 系统**自动初建第一代**
  → 解析 → 向量写入 → `parse_status=done`、`vector_status=indexed`；
- 首上传后**检索命中**（`report.collections` 里就是该库的真实 collection）；
- 第二次上传**复用**已有代次（`initialized=False`，不多建 collection）；
- 队列模式 + 无 worker：索引照样先初建，资料如实停在 `pending`；
- 失败资料：状态 `failed` + 原因，**不会卡在 running**，也不产生半成品分段；
- 上传走的是真实 multipart（`SimpleUploadedFile`，带文件名），
  首次上传记录**创建时**就有 `raw_path`/`raw_size_bytes`。

---

## 3. 本次修复的关键正确性问题（逐条）

| 问题 | 修法 |
|---|---|
| 上传后 `raw_path` 缺失，process 接口直接拒绝 | `services.ingest_file` 创建记录时即写 `raw_path`/`raw_size_bytes` |
| 查询自己按 `collection_name(library, generation)` 拼名字（查错 collection → 永远 0 命中） | 改为按**库真实指针 / 已激活登记行**取目标，并按各 collection 登记的 profile 维度分别取查询向量 |
| 全新库首上传无处可写（"首传前重建空库"死循环） | 上传接口自动 `ensure_library_index`；并提供 `POST /api/v1/knowledge/rebuild/` 与 `kbrebuild` |
| 激活校验只看 file 上的标记/指针（同文件可有多代，单指针证明不了完整性；回退必被当前标志挡死） | 改为**以目标 collection 真实 points 核对**：稳定 `point_id_for(file, ordinal)` 分批 retrieve，逐段比对 `file_id/ordinal/text/content_sha256/library_id/model/model_version`；空库、未解析资料、写了一半、模型不符一律拒绝 |
| 每次 prepare 复用"未激活空壳"或失败后重用编号 | **严格单调只增**：`max(next_generation, 登记最大编号, active) + 1`；回退/失败都不回收；`(library,name)` 唯一约束兜底 |
| PUT 可传 `generation=99 + collection=g1` 造假 | 代次以登记名解析出的事实为准，不一致直接 400 |
| 分段逐条写、无事务、只按数量判等 | 一个事务内 `bulk_create`；重试逐段核对 `ordinal/text/page` |
| 失败/异常卡 RUNNING | `process_file` 兜住**所有**异常 → FAILED + 审计留痕 |
| 租约"先查后写"TOCTOU、可被过期旧 worker 覆盖 | 状态写入改成**一条 CAS UPDATE**（`WHERE pk AND lease_token AND expires_at>now AND parse_status=旧值`），命中 1 行才算成功；`renew_lease` 带 `expires_at>now`，**不复活过期 token** |
| `kbprocess` 抢正在跑的 RUNNING | 只捞"无租约或租约已过期"，并区分"因租约跳过" |
| eager 让"队列"变成请求内同步执行 | `CELERY_TASK_ALWAYS_EAGER` **默认 False**；queue 模式显式拒绝 eager；派发成功也统一 **202 + task_id** |
| broker 断线阻塞请求 | `CELERY_BROKER_TRANSPORT_OPTIONS` 短超时 + 有限重试 |
| 关键词检索在离线/未嵌入时不可用 | 新增数据库关键词兜底（查**已授权的 DocumentChunk**），与向量路 RRF 融合；Qdrant 运行时断线同样兜底并如实标注降级 |
| 扫描 PDF 超上限静默丢页 | 超 `KB_OCR_MAX_PAGES` 直接报错中止 |
| 空 XLSX `blocks[0]` 越界 | 显式 `ParseError` |
| OCR 客户端"没成功过就永不调用" | `available` = 已配置即可发起**首次调用**，成功才 `mark_proven` |
| Qdrant 客户端改写进程级 `os.environ` | 实测 `QdrantClient(..., trust_env=False)` 可用，已删除全局环境修改 |
| **rerank / ocr 完全没接韧性层**（裸 httpx 调用，服务掉线时原始网络异常直接冒到接口） | 抽出统一的 `resilience.post_json`（超时 + 重试上限 + 熔断 + 明确不可用），embedding / rerank / ocr 三个客户端共用；重排不可用时检索**仍成功**并在报告里标 `rerank.unavailable` + 降级原因 |
| 重试分类"标记覆盖状态码"（把 `retryable=False` 的类身上带 503 判成不可重试） | `_is_retryable` 改为**状态码优先**：`http_status`/`response.status_code` 在 `RETRYABLE_STATUS` 就先重试，标记只兜底 |
| 首上传需要"先重建空库"（循环依赖） | 上传接口自动 `ensure_library_index` 建第一代；`POST /api/v1/knowledge/rebuild/` 与 `kbrebuild` 命令提供显式路径；空库激活仍被拒绝（有资料才允许走 rebuild 激活） |

---

## 4. 测试与证据的诚实口径

| 项 | 口径 |
|---|---|
| 单元/集成测试 | **实测**：187 passed / 2 skipped，SQLite 与独立 PG 测试库各跑一遍 |
| 向量写入 | 离线用**显式测试适配器**（确定性假向量）+ **真实 qdrant-client 本地模式**；payload 带 `is_test_adapter=true` 可一眼识别 |
| 关键词检索 | **实测**：数据库 `icontains` 兜底真命中；Qdrant `MatchText` 为 whole-token（短中文查询命中不到长 token 是分词边界，不是"模式不支持"） |
| 语义检索质量 | **未验证**——需要真实 AS711（当前断开） |
| 重排效果 | **未验证**——需要真实 AS711 rerank |
| OCR 识别质量 | **未验证**——需要真实 AS711 OCR |
| 并发容量 | **未测**，留给 P6；本文不写任何未测的并发数字 |

---

## 5. 明确未做 / 待恢复

| 项 | 状态 |
|---|---|
| 真实 AS711 嵌入 / 重排 / OCR 端到端 | **待服务恢复**（命令见 §7） |
| 真实 Qdrant server（docker）往返 | 见 `--kb-qdrant` 标记用例；服务在跑时可执行 |
| **离线队列往返**（eager：任务被派发→执行→状态落库） | **已完成**：`tests/test_queue_roundtrip.py`（6 条） |
| **重复派发只处理一次** | **已完成**：第二次走"已处理，跳过"，`parse_attempts` 不变 |
| **租约互斥 / 过期接管 / 旧 owner 不能覆盖** | **已完成**：`LeaseNotAcquired`、`LeaseLost`、续租不复活过期 token 均有断言 |
| **真实 Redis broker 往返** | 用例已写（`@pytest.mark.real_broker`），**待 Redis 在跑时执行**：`pytest -m real_broker --kb-broker` |
| P3 首页 AI 全部操作 | 未开始 |
| P4 三次确认删除（第三次输名称 + 服务端防绕过 + 事务级精确目标授权） | 未开始（设计见 `docs/architecture-review.md` D3.1） |
| P5 全库检索写新文档 + 右侧预览/编辑/下载 | 未开始 |
| P6 首页使用说明 + 并发压测（实测数字） | 未开始 |

---

## 6. 关键配置口径

| 变量 | 默认 | 说明 |
|---|---|---|
| `KB_UPLOAD_PROCESS_MODE` | `auto` | `inline`（请求内处理）/ `queue`（入队 202）/ `auto`（能派发就派发，否则如实"待处理"） |
| `CELERY_TASK_ALWAYS_EAGER` | **`0`（False）** | 生产默认 False；开启会让"队列"退化成请求内同步执行 |
| `KB_TEST_ADAPTER` | 空（关闭） | 仅 `offline` 时启用确定性假向量，且只允许写 `__offline_test` collection |
| `KB_PROCESS_LEASE_SECONDS` | `900` | 处理租约时长；长任务会续租（不复活已过期 token） |
| `KB_SEARCH_USE_RERANK` | `1` | 关闭时只用 RRF 融合结果；重排不可用时**检索仍成功**并标注 |
| `KB_RERANK_RETRIES` / `KB_OCR_RETRIES` | `1` | 重排/OCR 的重试次数（总尝试 = 次数 + 1），同样受熔断保护 |
| `KB_OCR_MAX_PAGES` | `20` | 单份 PDF 最多 OCR 页数；超过**报错中止**（不丢页） |
| `KB_SERVICE_FAILURE_THRESHOLD` / `KB_SERVICE_COOLDOWN_SECONDS` | `3` / `60` | 外呼熔断阈值与冷却（embedding / rerank / ocr 各自一个断路器） |

> `.env.example` 与本地 `.env` 都已确认：`CELERY_TASK_ALWAYS_EAGER=0`。
> 本地 `.env` 另设 `KB_UPLOAD_PROCESS_MODE=inline` 以便浏览器验收时立刻看到处理结果；
> 要验证队列口径就改成 `queue`，上传会返回 202 + task_id，再用 `manage.py kbprocess` 推进。

---

## 7. AS711 恢复后的真实验证命令（待执行）

```powershell
# 1) 只读探测（不会启停任何服务）
.venv\Scripts\python.exe manage.py kbprobe --probe --profile bge1024

# 2) 真实模型端到端（打上 real_as711 标记的用例）
.venv\Scripts\python.exe -m pytest tests -m real_as711 -q

# 3) 真实 Qdrant server 往返（容器在跑时）
.venv\Scripts\python.exe -m pytest tests -m real_qdrant -q --kb-qdrant

# 4) 真实上传 → 解析 → 嵌入 → 写入 → 过滤检索
#    把 KB_TEST_ADAPTER 留空（用真实嵌入），POST /api/v1/knowledge/files/upload/
```

恢复后需要补的记录：实测维度、真实检索命中、重排是否生效、OCR 识别样例。

---

## 8. 浏览器验收步骤（现在就能做）

```powershell
docker compose up -d
.venv\Scripts\python.exe manage.py migrate
.venv\Scripts\python.exe manage.py kbcreateuser --username demo --password demo-pass-123 --staff
.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000
```

1. 打开 <http://127.0.0.1:8000/app/login/> 用 demo 登录（**session 登录，不存 Basic 口令**）；
2. `/app/files/`：
   - 先建一个信息库（Django admin 或 API），在"目标信息库"里选中；
   - 选**单个文件**或**整个文件夹**上传 → 观察状态列（待处理/处理中/已完成/失败）；
   - 失败项点"重试处理"；"来源"列只读；
   - 底部"接口自检"可展开统计/库列表/索引统计/模型服务的原始 JSON。
3. `/app/search/`：
   - 信息库下拉**只列出你有权限的库**；多选=在这些库里搜；
   - 输入关键词检索，页面会显示"稠密/向量关键字/数据库关键词/融合"各自命中数与
     是否降级（AS711 断开时会显示"语义检索不可用"，但关键字仍能命中）。

### 8.1 本节已经实际跑过（不是"应该能跑通"）

用 Playwright 无头 Chromium 真实点击跑了一遍，脚本逻辑与结论如下
（截图：[文件管理](screenshots/p2-files.png)、[检索](screenshots/p2-search.png)）：

| 检查 | 实测结果 |
|---|---|
| 未登录访问 `/app/files/` | 302 → `/app/login/?next=/app/files/` |
| session 登录 | 登录后落到 `/app/files/`，页面无 `WWW-Authenticate`（不是 Basic） |
| 上传目标库下拉 | `['', 'contract-archive', 'settlement-archive']`（JS 调 API 填充） |
| 已入库资料表格 | 7 列表头齐全，含**只读的"来源"列**与"重试处理"按钮 |
| 接口自检 | `GET /api/v1/knowledge/stats/` → HTTP 200，原始 JSON 直接显示 |
| 检索页信息库下拉 | `['contract-archive', 'settlement-archive']` |
| 真实检索 | 返回 1 条命中；报告显示"稠密 0 / 向量关键字 0 / **数据库关键词 1** / 融合 1" |
| 降级如实标注 | 报告里写明"语义检索不可用：连接超时"与"重排服务不可用"，**没有**把关键词命中说成语义命中 |
| 浏览器控制台 | 无错误（CSRF 头已按 session 认证要求带上） |

**浏览器验收时发现并已修掉的两个真实缺陷**（这就是"必须真跑一遍"的价值）：

1. 写请求 403：DRF 的 SessionAuthentication 对 POST 强制 CSRF 校验，
   前端 fetch 没带 `X-CSRFToken` → 所有写操作（检索/重试）都失败。已修。
2. `RerankClient` / `OcrClient` 引用了 `self.profile.name`，而
   `RerankProfile` / `OcrProfile` **都没有** `name` 字段 → 真实重排/OCR 调用会
   抛 `AttributeError`。异常被上层兜住，只表现为"重排没生效"，很难发现。
   已修（改用 `service_name`），并补了 `tests/test_model_clients.py`（8 条）
   用 MockTransport 走完整真实分支，防止回归。

**顺带修掉的两个体验/性能问题**（同一次实跑中量出来的）：

3. 检索用超时默认 60 秒 → 模型服务断开时一次检索要等 35 秒（浏览器一直"检索中"）。
   现在检索超时 10 秒、连接超时 3 秒，并且**在发起外呼前先查熔断状态**：
   熔断中直接走关键字，实测第一次 15.1 秒、之后 0.8 秒（`dense_skipped_by_breaker=true`）。
4. 入库超时与检索超时**分开**：入库是后台任务，允许 `KB_EMBED_INDEX_TIMEOUT_SECONDS=60`。
