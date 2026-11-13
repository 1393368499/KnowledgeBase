# 交付状态与**未解决风险**（2026-10-15）

本文只记录**已发生的事实**与**仍未解决的问题**，不代替验收结论。

## 0. 用户 m11581 五项要求：逐项证据索引（2026-10-15 收尾核对）

| # | 要求 | 结论 | 真实证据（可当场复验） |
|---|---|---|---|
| 1 | 首页工具栏「待办事项」加删除（放弃）按钮 | ✅ 已完成 | 代码：`apps/webui/static/webui/kb-assistant.js:159-164`（**只对未执行完的卡片**出现「删除」）+ `:357-399` 放弃流程；服务端 `apps/planner/services.py:1090 abandon_operation` → `apps/planner/views.py:249` → `apps/planner/urls.py:50`（**只改状态、不删记录**）。测试：`tests/test_operation_abandon.py`，`junit-abandon.xml` testcases=114 / failures=0。浏览器（**当晚在当前镜像上重跑**）：`artifacts/acceptance/verify-abandon-docpane.py` → **8/8 通过**，其中「每张未结束的待办卡都有删除按钮」6 按钮/6 卡，确认框写明不会删资料且保留审计，实测点删除后卡片 6→5 且无 JS 报错；截图 `todo-docpane-2-pending-with-delete.png`。 |
| 2 | 文档侧边栏宽度改为原来 1.5 倍 | ✅ 已完成（**2026-10-19 用户先要求 ×1.5 → 855px，再依次收窄为 750px、**700px**；见 `docs/doc-pane-width-700-2026-10-19.md`**） | 代码：`apps/webui/static/webui/kb-home.css` `.doc-pane{flex:0 0 570px}`（注释写明 380px × 1.5）。浏览器实测（同上脚本、当时镜像）：**展开后渲染宽度 = 570px**、收起时 = 0px（保证「收起时几何与原型一致」）；截图 `todo-docpane-1-docs-open.png`。 |
| 3 | 盘点 AS711 模型以外的未完成功能并由易到难真正完成 | ✅ 已完成（**4 项明确不做/未验收，已在 §5.2 如实列出**） | §5.1 八行全 ✅，每行都有测试或实测：巡检留痕、反馈重扫执行链、前端静态固化、LLM 按需探测、Agent 页检索反馈表、**worker 崩溃恢复**（`artifacts/acceptance/worker-crash-recovery.json`，含修复前 59 分钟恢复缺陷的实测）、**整库重置**（`tests/test_reset_library_index.py` 17/17 + 真实 UI `reset-library-index-ui.json` 15/15）、**跨进程限流**（`cross-process-limit.json` + `cross-process-limit-runtime.json`，4 进程采样峰值=限额）。全量回归 **testcases=735 / failures=0 / errors=0 / skipped=24**（`junit-item3-final.xml`）。**边界**：AS711 当前从容器与宿主都不可达、生产库 0 份资料，所以「带资料的重建」只由真实流水线用例证明，未能在 UI 上复跑。 |
| 4 | 整理项目结构，避免后续 Docker 部署到服务器时失败 | ✅ 已完成 | 单一入口：根 `docker-compose.yml` **只用 `include`** 引用 `deploy/docker-compose.unified.yml`（文件头写明历史教训：旧根 compose 会把 Qdrant 切回坏目录），`docker compose config --quiet`（根与 `-f deploy/...` 两种写法）都 **exit 0**；`docker-compose.worker.yml` 头部标 **DEPRECATED** 并列出「直接跑会失败」的三条结构性原因；新机器一次性准备 `deploy/preflight.sh`（检查 .env / external 命名卷 / 宿主绑定目录）；显式 `COPY` 的 Dockerfile + `.dockerignore` 第二道防线（含 `.tmp*`/`_probe_*`/`media/`/`db/`，密钥与数据绝不进镜像）；当轮实建镜像 `kb-app:delivery` 并 `--force-recreate` 三容器全部 healthy（`build-app-item4.log`、`build-app.log`、`docker-hour-build-final.log`）。 |
| 5 | 全面使用仓库虚拟环境 `.venv` 跑 Python 工具链 | ✅ 已完成 | `.venv\Scripts\python.exe` = **Python 3.13.5**（实际执行路径已核）；本轮所有 pytest / 验收脚本 / 盘点脚本都用它跑；证据 `junit-item5-full.xml` **testcases=666 / failures=0 / errors=0 / skipped=24**（`pytest-item5-full.log`）与后续 `junit-item3-final.xml` 735 条。 |

## 1. 已完成并可复验（部署，方案 A 原地挂载）

| 项 | 证据 |
|---|---|
| 统一 Compose（web/worker/scheduler + 现有 PG/Redis/Qdrant） | `deploy/docker-compose.unified.yml` |
| 交付镜像含**当前源码** | `kb-app:delivery`；基础镜像 `kb-worker:phase2-review-20261015` digest `sha256:63c6532ef74939dee3bb9ceca4abfdc5639ffbcf454c3568774096ba9e4bdea8`；容器内 `whitenoise` 中间件存在 |
| 挂载来源已核（2026-10-15 晚 **重新核对**） | `docker inspect`：qdrant→**命名卷** `knowledgebase-qdrantdata-delivery-20261015` → `/qdrant/storage`（rw，**已不再**挂项目根 `data/vector_data/qdrant`，见 §2）；web rw / worker ro→项目根 `data/media/raw`；宿主与容器内**都是 276 个原件**（合计 0.96 MB） |
| web 生产形态 | PID1 = `gunicorn … --bind 0.0.0.0:8000 --workers 2 --threads 4 --timeout 300 --graceful-timeout 300`（超时覆盖模型最长超时） |
| 队列隔离 | 三方 `CELERY_BROKER_URL=redis://redis:6379/14`（同时覆盖无前缀变量）；**Redis DB0 的 193 条业务消息未被消费** |
| 宿主真可用 | `http://127.0.0.1:8800/api/v1/health/ready/` 与 `/static/webui/*.js` 均 HTTP 200（**宿主端口已由 8000 改为 8800**：8000 落进 Windows WinNAT 动态保留段 7984–8083，宿主绑定被拒 WinError 10013；容器内仍是 8000，详见 `docs/deployment-and-operations.md` §1/§5） |

## 2. Qdrant 存储：由用户批准的 **Docker 命名卷** 承接（原 bind mount 缺陷已记录在案）

**事实（用户独立实测，2026-10-15 16:13）**

1. 用现有 `kbrebuild` 对 **id 9–15 共 7 个库**重建 **g2**：13 份真实原件经 AS711 嵌入、全覆盖并成功激活。
2. 逐库 `scroll`（`with_payload=True`、`with_vector=True`）**全部 200**，并保存每库点的向量/payload SHA
   → 证据：`artifacts/acceptance/docker-hour-rebuild-before-restart.json`。
3. 仅执行 `docker restart knowledgebase-qdrant` 后：
   **7/7 新活动集合再次全部 500 `OutputTooSmall`**
   → 证据：`artifacts/acceptance/docker-hour-rebuild-after-restart.json`。

**结论（当前真实状态，2026-10-15 16:45 更新）**

- **方案 A 的 bind mount 问题已由用户批准的变更解决**：改为 **Docker 命名卷**
  `knowledgebase-qdrantdata-delivery-20261015`（由 Docker 管理，**不再强制落在项目根**）。
  用户已独立验证：**9 个库 / 22 个点，重启 Qdrant 前后逐库带 payload + 向量取值完全一致**，
  带 `library_id` 过滤的真实查询 **9/9 通过**。
- **重建方式必须准确表述**：本轮用的是既有 `kbrebuild --all`，其 **`reuse_chunks=True`**
  —— 即**复用已存不可变 `DocumentChunk` 重新嵌入向量**，
  **不是**"重新解析原件"。对 id 14/15/16 这类**缺原件**的历史记录，
  它是基于已存分段重嵌入；**不得**写成"重新解析原件成功"。
- 早期的小样本 probe（新建 1 点、两种 `on_disk_payload`、重启后 200）**只证明该次样本可读**，
  **不能**推出 `on_disk_payload=False` 可避开 payload 存储路径
  （1.15.5 的 `InRamMmap` 仍用 `MmapPayloadStorage`）。
- 命名卷下的**长期稳定性**（多轮重启/大量写入后）尚未做长周期验证；
  目前的证据是**该次重启一致性实测通过**，不构成"所有长期风险已彻底解决"。

## 3. 待评估的替代方向（**仅候选，未实施**）

- **ID + 后端回填**：Qdrant 只返回 `point id + score`（不取 payload / 不取 vector），
  PostgreSQL 按**服务端已收敛的授权候选 ID** 回填原文与元数据。
  这样可绕开 payload 读取路径，但：
  - 必须**服务端收敛权限候选**（不能把授权判断放到客户端或事后过滤）；
  - **不得**用"只返回 ID"把向量检索失败伪装成成功 —— 该方向需单独审查与等价性验证；
  - 混合检索（稠密+稀疏+RRF）依赖 payload 过滤的部分需要重新设计。
- 本文**不实施**该方向，仅登记为候选。

## 4. 本轮补完的功能（含回归）

| 项 | 说明 | 验证 |
|---|---|---|
| `create_library` 的 `level` | 登记过的 `level` 真正生效；非法值明确报错 | 回归全绿 |
| AI `search_knowledge` 结构化与权限口径 | 复用搜索 API 的服务端收敛（`department`/`customer`/`amount`）；**显式指定的库不存在或无权直接报错**（不再静默退回全库）；`permission_codes` 用用户可读口径 | 回归全绿 |
| 派生文档**严格来源校验** | `source_file_ids` 逐项校验：非数组/空数组/非数字/负数/0/浮点/布尔/对象 → **400 且不触发模型调用**；混合不存在 id → **400**（不静默丢弃）；`compose_document` 内任一来源不可用即报错（不再无声缩小范围） | `tests/test_document_source_strict.py` 10/10 |
| 迁移守卫的**环境独立性** | `assert_no_inflight_processing` 显式使用 `knowledgebase.settings` 并清除测试库变量；夹具模式不再误查部署库；提供可注入 provider | `tests/test_storage_migrate_safety.py` + `test_storage_verified_binding.py` 12/12；`tests/test_inflight_guard_semantics.py` 4/4 |
| 前端接通**真实运行指标** | Agent 页六张状态卡**全部**有真实判定来源：「API熔断器」=真实快照（含**口径：进程内**标注）、「资源调控」=真实队列深度（DB14）、「解析看门狗 / 结果校验」=最近一次巡检**留痕**（`PatrolRun`，没跑过就灰点）、「双阶段补扫」=实时候选查询；五个指标（停滞任务/重启次数/熔断失败数/任务成功率/待处理反馈）也全部来自真实查询或审计记录 | JS 符号检查通过；webui 7/7；宿主 HTTP 实测送达；`tests/test_patrol_and_integrity.py`、`tests/test_webui_agent_cards.py` |

## 5. 仍未完成（**逐项如实列出**）

> 本节是交付清单的一部分。**§5.1 是本轮及此前补齐的项（附证据）**；
> **§5.2 才是当前仍未完成/未验收的项** —— 不得在报告/界面/文档中宣称已完成。

### 5.1 此前列为未完成、已完成（附证据）

| # | 项 | 当前真实状态 | 证据 / 说明 |
|---|---|---|---|
| 1 | **看门狗（watchdog）** | ✅ **已完成（以周期巡检 + 留痕实现）** | `knowledge.patrol`（beat 每 `KB_CELERY_BEAT_PATROL_SECONDS`=900 秒）做四项真实检查：重试次数用尽 / 停滞 / 向量不完整 / 原件缺失；每次运行写一行 `PatrolRun`，界面「解析看门狗 / 结果校验」据此显示真实读数（**没跑过巡检就是灰点**）；`manage.py kbpatrol` 与「立即巡检」（意图 `run_patrol`）是**同一实现**。**剩余边界**：没有独立进程级看门狗 —— beat/worker 自身的存活由容器 healthcheck 与 `celery inspect ping` 覆盖，"谁看住 beat"不由本系统解决。 |
| 4 | **反馈重扫的"执行"链路** | ✅ **已完成** | `reparse_failed` 对**净负分**资料（与被反复判不准确的口径**同一实现** `apps/knowledge/integrity.py::net_negative_feedback_rows`）逐份 `retry_file`，复用同一只读原件重建分段与向量、**不改源字节**；按实际结果分 processed / failed / lease_busy / incomplete 报账；`include_failed=true` 时额外重试解析失败资料（尝试次数已用尽的不重复刷）。证据：`tests/test_reparse_failed.py`、`tests/test_patrol_and_integrity.py` |
| 8 | **前端静态资源固化** | ✅ **已完成** | 早期用 `docker cp` 热修的临时手段已被正常镜像构建 + `collectstatic` 取代。 |
| 9 | **LLM（大模型服务）行的按需探测** | ✅ **已接入** | 真实发一次最小 `chat/completions`（`max_tokens=8`、`temperature=0`）并返回连通性/HTTP 状态/延迟/是否空内容；`KB_MODEL_CALLS_DISABLED=1` 或未配置时如实跳过（返回 `skipped` 与原因），**不使用 embedding 顶替**，不冒充四个按钮全部相同。证据：`tests/test_llm_probe_target.py` |
| 12 | **Agent 页「检索反馈（批注）」表** | ✅ **已完成** | 该表此前是**写死的空态**（`还没有检索反馈。`），而端点其实一直存在。现在接 `GET /knowledge/search-feedback/`：只列**自己**的反馈，带资料名、判定中文名与**净分**（与「重扫未处理反馈」同一实现 `net_negative_feedback_rows`，且只对**当前可读**的资料给出净分，撤权后留空）。证据：`tests/test_search_feedback_list.py`、`tests/test_webui_agent_cards.py` |
| 6 | **真正的 worker 崩溃恢复** | ✅ **已完成（隔离实测通过）** | 用独立队列 `kb_crash_probe` 做了两个场景的隔离实测（主队列与 Redis DB0 全程不动，可复现：`scripts/crash_recovery_probe.ps1`）。**场景 A**：`SIGKILL` worker 的池子进程 → `task_reject_on_worker_lost` 立即重投，200 条全部 SUCCESS、未确认清零；**场景 B**：硬杀整个容器 → 父进程也没了，只能等 kombu `visibility_timeout`（默认 3600s，实测 4 条在飞消息还要等 **3554s≈59 分钟**、期间 4 条一直 `PENDING`）→ **已修**：`visibility_timeout` 收敛为「任务硬超时 + 300」= 2100s，并**同时补上此前完全没设的两个任务超时**（硬 1800 / 软 1440；不留超时则任务可能永不返回，任何 `visibility_timeout` 都会被"还活着的任务被重复投递"击穿）。证据：`artifacts/acceptance/worker-crash-recovery.json`、`tests/test_celery_crash_recovery_settings.py` 6/6、`docs/deployment-and-operations.md` §「崩溃恢复的真实边界」。 |
| 3 | **整库重置（controlled reset）** | ✅ **已完成（受控重建口径，不是清库）** | 新增受控提案意图 `reset_library_index`（`apps/planner/operations.py`：`permission=system:operate`、`RISK_DESTRUCTIVE`、`requires_destructive_flow=True`、`ticket_scope=reset_library_index`）：三次独立确认 + 末次**准确输入库名**（服务端逐字符比对）+ 一次性凭证 + `ARMED` 执行窗口。语义是**重建**：逐份 `retry_file(force=True)` 按只读原件重跑分段与向量，**业务记录一行不动、原件字节不擦除**；只有**全部成功**才清理不再生效的旧代次 collection 与其登记行；任一份租约在飞 / 解析失败 / 超过本轮上限 → 如实报 `PARTIAL` 且**不清理任何旧代次**（宁可留着旧代次，也不留半清空的库）。界面：信息库页每库「重置索引」按钮走同一受控流程，确认卡的影响范围全部来自后端（资料份数、已登记集合、`index_note`）。证据：`tests/test_reset_library_index.py` **17/17**（含**真实流水线**用例：真原件 → 真解析/分段 → 真 Qdrant 写入 → 重建后向量点数、分段数、原文字节全部不变）、`docs/function-operation-matrix.md` 行「重置数据库」与 T8。**真实 UI 浏览器验收 15/15**（`artifacts/acceptance/verify-reset-library-index.py` → `reset-library-index-ui.json` + 3 张截图）：末次确认输错名 → 服务端拒绝且一次 `/execute/` 都没发；按确认卡上「目标：合同档案库」原样输入 → 三次确认 → `/execute/` 200，结果体 `{'complete': True, 'rebuilt': 0, 'remaining': 0}`；执行前后实测**只**删除 1 行失效登记（`('kb-7-g1', 7, False)`），资料 0→0、分段 0→0、原件 276→276、Qdrant 3 集合点数全 0 不变。**边界（如实）**：生产库当前 0 份资料（用户 m11581 第 (3) 项验收后清空的终态），所以 UI 这轮重建的是空库；「带资料的重建」由上面的真实流水线用例与 17/17 专项用例证明；AS711 当前从容器与宿主都不可达，无法再造真实资料。全量回归（所有改动落盘后重跑）：**testcases=735 / failures=0 / errors=0 / skipped=24**（`artifacts/acceptance/junit-item3-final.xml`、`pytest-item3-final.log`）。 |
| 5 | **跨进程限流** | ✅ **已完成（Redis 排序集合信号量，真实多进程验收通过）** | 新模块 `apps/knowledge/ratelimit.py`：额度是 Redis 里的**排序集合**（`kb:model:slots:<service>`），`acquire` 是原子 Lua（先 `ZREMRANGEBYSCORE` 清掉超 TTL 的**单个**持有者，再在 `ZCARD < limit` 时 `ZADD` 自己），`release` 是 `ZREM` —— 故意不用 `INCR/DECR` 计数：那样持有者被 SIGKILL 会把额度**永久占死**，而给键加整体 TTL 又会在负载持续时把**在飞**的持有者也清零造成超发。挂点在**唯一外呼收口** `resilience.call_with_resilience()`，所以 embedding/rerank/ocr/llm 四条路径一起生效；等不到额度抛 `ServiceUnavailable`（如实"模型服务不可用"，且**不发请求**）；Redis 不可用时 **fail-open 放行**并把降级次数计入指标 `model_concurrency.fallback`（口径 `scope: redis`，与进程内熔断器的 `scope: process` 分开标注）。证据：`artifacts/acceptance/cross-process-limit.json`（4 个**独立进程** × 25 轮，Redis 采样峰值 = 限额 2，未超发、有真实拒绝、结束额度归还）、`tests/test_cross_process_rate_limit.py` 14/14。**注意**：这解决的是"并发数"（保护 AS711 上的模型服务），不是"速率/令牌桶"；两者都已明确登记。 |

### 5.2 仍未完成 / 未验收

| # | 项 | 当前真实状态 | 为什么不算完成 |
|---|---|---|---|
| 2 | **AS711 模型服务的管理/切换** | **未实现** | 只有"按需连通探测 + 实测延迟"；**不提供**启动/停止/切换本地与远程推理的能力（也不应擅自操作 AS711）。 |
| 7 | **Qdrant 重启后 payload 读取** | **已由命名卷规避，但未做长期稳定性验证** | 用户已批准命名卷并从原件重建，重启一致性已由用户独立验证（9/9 校验值相同、带 `library_id` 过滤查询 9/9 通过）。这证明**当前配置在该次实测下可用**；**不构成**"所有长期风险已彻底解决"的结论。 |
| 10 | id 14/15/16 缺原件的历史记录 | **明确拒绝伪造** | 无 `raw_path`，`add_to_library`/重建路径对其**明确报错**，不会假造原件、不宣称"重新解析原件成功"。 |
| 11 | **「立即巡检」之外的"独立看门狗进程"** | **未实现（且已明确不承诺）** | 见 §5.1 第 1 行的"剩余边界"：巡检依赖 beat 周期调度；没有任何独立进程监视 beat/worker 自身。 |

**已完成的与上表无关的能力**（避免误读）：检索反馈持久化与加权重排、`list_feedback_targets` 定位、
按需探测与实测延迟、真实运行指标（含**真实队列名**积压读取）、**真实周期巡检**（失败到上限/停滞/向量不完整/原件缺失，
只读）、**巡检留痕与「立即巡检」**、**「重启停滞任务」真实推进**（租约判定 + 逐份 `process_file` + 如实分类）、派生文档严格来源校验与短事务、`export_document` 真实下载、`add_to_library` 受控复用、
`create_library` 的 `level`、AI 检索的结构化条件与权限口径。

## 6. 非模型缺口审计与修复（2026-10-15 第二轮，用户 m04007）

> 用户问："还有哪些功能和交互逻辑没有处理的，**除了模型相关的**，保证明天连接上模型后这个系统所有功能能够使用。"
> 做法：两个**只读**审计子代理分头过一遍前端（`apps/webui`）与后端（`apps/planner`、`apps/knowledge`、管理命令），
> 逐条核实"是不是真的缺口"（**反证过的伪缺口不计入**），然后按"由易到难、每项有真实证据"逐条修。
> 完整的 22 条缺口 → 处置 → 证据表在 `docs/function-operation-matrix.md` **§11**。

### 6.1 修掉的**真实缺陷**（不是文案问题）

| 缺陷 | 真实后果 | 证据 |
|---|---|---|
| 巡检停滞判据读**不存在的字段** `parse_started_at` | `getattr` 恒为 `None` → 永远回落到 `created_at`：一份 3 小时前创建、1 分钟前才开始重跑的资料被误报"停滞" | `tests/test_patrol.py`（把旧写法还原后两条用例都 FAIL） |
| `executing` 是**单行道死胡同** | 进程在 CAS 到 EXECUTING 之后被杀 → 放弃被拒、再执行被拒、恢复视图 400 → 待办**永久**显示"执行中" | `tests/test_operation_abandon.py` 23 passed（含周期收敛 `converge_stale_executing_operations`） |
| `archived` 是只读死状态 | 状态机与界面都在渲染"已归档"，但**全仓 0 个写入方** → 该状态永远不会出现 | `tests/test_archive_file.py` 12 passed（含真实 Qdrant 删点） |
| 模型不给正文 + 提案全被拒 | 拒绝原因只存在于本次 HTTP 响应 → 刷新后只剩**空气泡**，用户不知道发生了什么 | `tests/test_p3_p5_regressions.py`、`tests/test_llm_failure_api.py` |
| 窄屏（<=960px）**完全没有导航** | `kb-proto.css` 把侧栏 `display:none`，而侧栏是唯一导航容器、顶栏没有 ☰、`_navbar.html` 从未被 include → 窄屏用户只能靠手改 URL | `artifacts/acceptance/verify-responsive-nav.py` → `responsive-nav-ui.json` |
| 上传失败**静默丢弃** | 旧实现用原始 `fetch`、只看 `response.ok` → 用户以为"已提交 5/5"，实际 4 份被拒且原因不可见 | `artifacts/acceptance/verify-files-upload-and-row-actions.py` → `files-upload-and-row-actions-ui.json` |
| 接口失败被画成"还没有数据" | 信息库/会话/系统配置三处在 401/403/500 时显示空态或"未配置"，把"读不到"伪装成"真的没有" | `artifacts/acceptance/verify-ui-honesty-batch.py` → `ui-honesty-batch.json`（**路由注入 500/403**） |
| `requires_confirmation=False` 与 `True` 等价 | 登记表说的"只读、无需确认"与真实确认次数不一致（两分支都取 1） | `tests/test_operation_registry.py` |
| **退出登录点了没反应**（POST 403） | 顶栏退出表单里**没有** `{% csrf_token %}`（token 只在 `<body>` 顶部渲染过一次）→ 每次登出都被 CSRF 拒绝，用户以为"退不出去" | `artifacts/acceptance/verify-responsive-nav.py`（修前该项 FAIL、浏览器控制台有 403；修后 19/19） |
| **页面把模板注释原样渲染给用户看** | Django 的 `{# #}` **不能跨行**，写成多行就不是注释 → 顶栏/搜索页直接显示出"〔窄屏导航入口：…原型 CSS 在 @media 里直接 .sidebar{display:none}…〕"这类内部说明 | 同上脚本的新断言「页面上没有未渲染的模板语法残留」；已把 2 处多行 `{# #}` 改成 `{% comment %}` |

### 6.2 补上的**缺失入口**（后端有、页面上零入口）

- **代次切换/回退、索引状态、审计事件**：Agent 控制台新增「索引与代次」卡（逐行「设为生效」+ 每库「回退到上一代」）与「审计（只读）」卡（最近 8 条 + 校验审计链）。切换**一律走受控提案**（`activate_generation` / `rollback_generation`），页面里**没有**直连 `PUT /api/v1/knowledge/generations/` 的字面量（有测试断言）。证据 `tests/test_agent_console_reachability.py`。
- **派生文档删除**：新增 `delete_derived_document` + `DerivedDocumentView.delete` + 助手页「删除草稿」按钮；**来源资料与原件不受影响**。证据 `tests/test_derived_document_delete.py`。
- **两条维护命令**（此前这些动作**没有任何入口**）：`manage.py kbvacuum`（只删"登记在、Qdrant 里不存在"的登记行，默认 dry-run，Qdrant 不可达则 fail-closed；存在但未登记的集合**只报告绝不删**）、`manage.py kbcleanup`（清理过期一次性凭证，**明确不动**审计事件/操作记录/删除账本）。证据 `tests/test_maintenance_commands.py`。
- **原件丢失的修复入口**：重传同一份文件即修复（内容寻址把丢失字节写回同一只读路径）；若该资料此前**处理失败**，本次重传会重新排队处理。证据 `tests/test_upload_api.py::test_reshipping_a_failed_file_restores_a_lost_blob_and_requeues`。
- **「触发一次学习」按钮**：本系统**不做训练**（反馈在检索时实时生效、有界 ±0.15），因此按钮改成实话「查看学习规则」，对应新增**只读**意图 `feedback_learning_report`。证据 `tests/test_webui_agent_cards.py`。
- **搜索页「归属客户」筛选**：页面文案一直承诺、`kb-search.js` 一直按它写（三处死守卫），但 DOM 里没有控件 → 已补 `#customer` 下拉，并验证选中后**真的随检索请求发给后端**。
- **Agent 页反馈表第 5 列「分诊」**：原型有「标记已处理」，真实界面却是空表头 + 空单元格 → 新增 `SearchFeedback.handled_at/handled_by`（迁移 `0016`）与 `POST /knowledge/search-feedback/<pk>/handle/`。**只留痕**：不改 `verdict`、不改加权、不改「净分 / 待处理反馈 / 重扫未处理反馈」口径。证据 `tests/test_search_feedback_triage.py`（7 条）+ `artifacts/acceptance/verify-feedback-triage.py`（14/14）。

### 6.3 回归

`KB_PYTEST_DB=sqlite` + 项目 `.venv`：

```
& .\.venv\Scripts\python.exe -m pytest tests -q --junit-xml=artifacts\acceptance\junit-nonmodel-gaps.xml
-> testcases=782 failures=0 errors=0 skipped=24          # 非模型缺口修复那一轮
& .\.venv\Scripts\python.exe -m pytest tests -q --junit-xml=artifacts\acceptance\junit-nonmodel-gaps-3.xml
-> tests=789 failures=0 errors=0 skipped=24              # 本轮（含反馈分诊 7 条新用例）
```

（对照上一轮基线 735 → 782 → **789**；24 个 skip 仍是环境门控：真实 Qdrant 端点、本机 Redis、AS711、Linux 权限位、符号链接。）

### 6.3.1 浏览器验收（项目 `.venv` + Playwright，跑在**重建后**的镜像上）

| 脚本 | 结果 | 证据 |
|---|---|---|
| `artifacts/acceptance/verify-responsive-nav.py` | **19/19 PASS** | `responsive-nav-ui.json`（含"退出登录真的退出"、"页面上没有未渲染的模板语法残留"） |
| `artifacts/acceptance/verify-ui-honesty-batch.py` | **11/11 PASS** | `ui-honesty-batch.json`（路由注入 403/500，看页面是否如实报错） |
| `artifacts/acceptance/verify-files-upload-and-row-actions.py` | **13/13 PASS** | `files-upload-and-row-actions-ui.json`（含受控删除收尾，环境回到 0 份资料） |
| `artifacts/acceptance/verify-agent-console.py`（既有，本轮回归） | **20/20 PASS** | `verify-agent-console.json` |
| `artifacts/acceptance/verify-abandon-docpane.py`（既有，本轮回归） | **8/8 PASS** | `verify-abandon-docpane.json` |
| `artifacts/acceptance/verify-agent-learn-and-doc-delete.py` | **16/16 PASS** | `agent-learn-and-doc-delete-ui.json`（Agent 页「查看学习规则」走受控确认后返回**真实只读**读数 `{"ok":true,"count":0,"max_adjust":0.15,…"本系统不做训练"}`，全程无受控操作之外的写请求；助手页「删除草稿」：种子草稿 → 文档栏可见 → 确认文案写明"来源资料与原始文件不会被删除" → `DELETE` 200 返回 `{"deleted":"derived_document",…"审计事件仍然保留"}` → 列表里消失）。种子与清理：`artifacts/acceptance/_seed_derived_document.py`（容器内建草稿；`cleanup` 只删本脚本标题前缀的行，本轮清掉 2 份历史遗留草稿 id 13/14）。 |
| `artifacts/acceptance/verify-feedback-triage.py` | **14/14 PASS** | `verify-feedback-triage.json`（+ 截图 `feedback-triage-1-before.png`／`-2-after.png`）：第 5 列表头 =「分诊」；点「标记已处理」→ `POST /knowledge/search-feedback/5/handle/` 返回 `{"handled":true,"already_handled":false,"handled_by":"admin","handled_at":"2026-10-15T15:04:24+00:00"}`；重载后按钮变「已处理 + 时间」；**「处理结果」列标记前后逐字一致（都是 `—`）**；同 id 再调 → `already_handled:true` 且 `handled_at` 与首次**完全相同**；未知 id → 404。留痕另由容器内读回：`_seed_feedback_row.py verify 5` → `{"handled_by":"admin","audit_events":1}`。种子/清理：`artifacts/acceptance/_seed_feedback_row.py`（只删本脚本查询词前缀的行，含回收旧 `verify-agent-console.py --fixture-row` 的固定查询词）。 |

> 四条验收脚本本身也修过 6 处**假失败/假通过**（都记在脚本注释里，避免下次重犯）：
> ① Playwright 的 glob 里 `*` **不跨 `/`**，`**/libraries*` 匹配不到 `libraries/?page_size=200` → 路由注入根本没生效（改用正则）；
> ② 注入故障必然产生浏览器 resource error，把它算成"JS 报错"是假 FAIL（改成只把**非** resource error 记为 JS 异常）；
> ③ 空态占位行也是 `<tr>`，把它数成"资料行"会得出"环境没清干净"的假结论（排除 `.empty-state`）；
> ④ 诚实提示里**故意**引用了"还没有信息库"这几个字，用子串去否定空态会命中自己（改成整段精确匹配）；
> ⑤ 文档栏**默认收起**，而 `listDocuments()` 只在展开时才跑 → 不先点「文档 ▸」就断言"草稿没出现"是假 FAIL；
> ⑥ 受控操作本身就是 POST（propose/confirm/execute），把它算成"写请求"会误报 → 只统计受控端点的**之外**的写；
> ⑦ 反馈表「问题」列渲染的是 `note`，**查询词只在 `title` 属性里** → 用查询词去匹配行会假 FAIL（改用可见的 note 标记／按钮的 `data-anno-done` id）；
> ⑧ 脚本**自己**故意打的 404 探针必然在控制台留下 "Failed to load resource: 404"，把它算成页面 JS 错误是假 FAIL（按 `msg.location` 里的探针 URL 排除）。

### 6.4 仍未做 / 需产品决策（**不含模型相关项**）

> **本轮已从下表移除**：原型「检索反馈 → 标记已处理」。它此前登记为"需产品确认口径"，本轮按**最窄**的合理口径实现了：`SearchFeedback.handled_at/handled_by`（迁移 `0016`）+ `POST /knowledge/search-feedback/<pk>/handle/` + Agent 页第 5 列按钮，**只做分诊留痕**（写 `handled_at/by` 与一条审计事件），**不改变**检索加权，也**不改变**「净分 / 待处理反馈 / 重扫未处理反馈」的口径——所以"标记已处理"不会让一份资料变准，也不会让它逃出重扫范围；重复处理幂等，别人的反馈 404、系统管理员可处理全部。证据：`tests/test_search_feedback_triage.py`（7 项，含"口径一字不变"断言）、`artifacts/acceptance/verify-feedback-triage.py`（14/14）、`docs/function-operation-matrix.md` §11 第 23 行。

| 项 | 状态 | 说明 |
|---|---|---|
| 归档**单向不可反归档** | **如实登记为边界** | `ARCHIVED` 是状态机终态（`models.py`：`ARCHIVED -> set()`），`retry_file` 也只接受 `{FAILED, RUNNING, DONE}` → 归档后无法取消。语义上"移出检索"如此，但误操作只能靠删除资料走出；如需反归档，需新增一次重解析路径（模型可用时才有意义）。 |
| 代次切换的 **CLI** | 未做 | 页面入口已补（走受控提案）。CLI 属于便利性，不是"功能不可用"。 |
| 终态 `OperationRecord` 的清理命令 | **不打算做** | 操作记录是审计证据，不是缓存（`kbcleanup` 的 docstring 已写明）。 |
| 无人引用的模板 `_navbar.html` / `_sidebar.html` | 未删 | 死模板不参与渲染；删除属清理，已登记。 |
| 添加记录页语义（写死 `source=session`） | 设计如此 | 导航已改名「会话来源」；原型是"待办任务视图"，这是刻意重构。 |

## 7. 「假设模型连上了，是否所有功能都能用」（2026-10-15 第三轮，用户 m06677 追问）

> 用户原话（m06677）：「除了模型，检查其他=功能是否全部完成，假设模型连接了，这个系统的所有功能是不是都能使用」。

### 7.1 结论

**是。** 四路模型（嵌入/重排/OCR/LLM）都已接线，且**有真实外呼成功的历史证据**（不是 mock）：
当天上午 AS711 在线时 —— `artifacts/acceptance/as711-resume-20261015.json`（`manage.py kbprobe --probe`）
→ 嵌入 `ready=true, actual_dimension=1024`（与声明一致才写库）、重排 `ready=true, top_score=0.97265625`、
OCR `ready=true`；`artifacts/acceptance/resume-independent-rag.json` → 真实 LLM 问答返回
"36个月/赵启明"并带来源引用（60.57s，usage 真实）。**那一次就是"模型接上后全功能可用"的实证**。
当前 AS711 从宿主与容器都不可达，四路能力进入**如实降级**（不是崩溃）：界面显示未连通/熔断，
检索退化关键字并标注 `degraded`，问答给出明确的失败原因。

### 7.2 本轮审计（4 路并行只读审计 + 本人复核）与交叉验证

- 后端：126 条 URL（67 业务/API + 59 admin）、54 条业务路由、32 个操作意图 —— 全部有真实执行分支
  （`apps/planner/services.py:3539` 的 `raise PlannerError("操作 … 还没有执行实现")` 不可达），无 501/桩函数。
- 前端：8 个导航页都有真实视图与接口；无"有按钮无处理函数"；无指向不存在路由的链接。
- 运维：`/health/ready` 200（`checks.migrations=ok`）；`celery inspect ping` → pong；`active_queues` 含 `kb_worker_v2`；
  redis DB14 在用、DB0 未被触碰。
- 复核时修掉一处**真实的部署漂移**：worker/scheduler 还跑着 14:46 的旧镜像（web 已是 23:01），
  已 `up -d --force-recreate migrate web worker scheduler` 统一到同一镜像；`showmigrations knowledge` 中 `0016` 已 `[X]`。
- 回归：`artifacts/acceptance/junit-nonmodel-gaps-3.xml` = **789 / 0 失败 / 0 错误 / 24 跳过**。

### 7.3 如实登记的三条"口径"问题（不影响功能可用，但会影响你判断"是不是连上了"）

| 项 | 事实 | 后果与对策 |
|---|---|---|
| 界面「测试连通」对重排/OCR 只发 `GET {base}/models` | `apps/knowledge/views.py:749`（`connectivity_only=true`），且用裸 `httpx.get`（不过 `build_http_client`，`trust_env=False` 的保护不适用） | 若 AS711 的 `:8002/:8003` 没实现 `/v1/models`，界面可能显示"未连通"而服务其实可用 → **以 `manage.py kbprobe --probe` 为权威**（真调嵌入/重排/OCR） |
| LLM 探测只证"能补全" | `apps/knowledge/views.py:790-869` 独立最小 `chat/completions`，不走 `LlmClient.chat` 的载荷/解析 | 探测通过 ≠ 问答链路全通 → 端到端冒烟必须真问一次 |
| 重排只有"服务不可用"才置 `degraded` | `apps/knowledge/pipeline.py:1558-1572`：其它错误只写 `report["rerank"]["error"]` | 结果其实已回退 RRF 融合（重排失效），但界面不一定显示"降级" → 排障时看 `report.rerank.error` |

### 7.4 交付物

- `docs/model-onboarding-runbook.md`：**接模型当天的唯一顺序**（两层网络可达性 → 配置变量表 →
  `up -d --force-recreate migrate web worker scheduler`（**必须显式带 migrate**）→ 三级真实验证
  （`kbprobe --probe` / 界面测试连通 / 端到端冒烟）→ 故障排查（熔断 60s 且**进程内**、队列错配、健康检查））。
- `deploy/model-readiness.ps1`：一键体检（宿主 + 容器可达性、`kbprobe --probe` 产物落
  `artifacts/acceptance/model-probe-<时间戳>.json`、`/api/v1/health/ready/`、worker 队列、结论与下一步）。
  **实测（2026-10-15 23:47，AS711 不可达时）**：四端点宿主与容器都不通 → 真实探测逐项如实报错
  （embedding "post 尝试 3 次仍未成功：请求超时"、rerank/OCR 同）→ `/api/v1/health/ready/` **200**
  `{"status":"ok","checks":{"database":"ok","migrations":"ok","vector_data":"ok"}}` →
  worker `active_queues` 含 `kb_worker_v2` → 结论"模型目前不可用"并给出下一步；产物
  `artifacts/acceptance/model-probe-20261015-234738.json`（合法 UTF-8 JSON）。
  注：脚本是 `.ps1` 且含中文，**必须以「带 BOM 的 UTF-8」保存**，否则 Windows PowerShell 5.1
  会按 ANSI/GBK 读取而报 `Unexpected token` 解析错误（本仓库 `write` 工具写出的是无 BOM UTF-8，
  需转换后再交付）。

