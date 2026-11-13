# 交付清单与已知限制

> **当前完整目标状态（2026-10-15）**：开发与独立验收进行中，尚未交付全部功能。第一阶段PG350通过/1跳过、SQLite338通过/13跳过，真实AS711问答和浏览器指定来源文档生成/保存/导出已有证据。存储迁移、后台并发恢复、反馈学习、监控、模型操作及其余边界仍待完成。以 [恢复记录](resume-2026-10-15.md)、[独立问题清单](independent-open-issues-2026-10-15.md) 为当前证据；下文2026-10-14结论保留作历史记录。

> **最新独立验收结论（2026-10-14）**：本次指定原型的界面还原已完成并通过浏览器验收。最新独立回归：PostgreSQL + 真实 Qdrant + 隔离 worker 328通过/1跳过，SQLite 316通过/13跳过；最后小修后的定向回归24通过。详见 [界面验收记录](ui-acceptance-2026-10-14.md)。后文“独立验收进行中/等待”的描述为此前快照，以此结论为准。原型占位能力并非全部实现（见第0节）；AS711 真实模型联调仍未完成。

> **新增界面要求（2026-10-14）**：用户要求八个页面原样采用
> `knowledge-base-prototype.html`（原型源文件**只读**，SHA256
> `D3C0886E…4DDE5`，mtime 未变），仅**首页**增加可收起的右侧文档栏，使用说明独立成页。
>
> **实现状态**：原型界面还原**已实现完成**——首页、文件管理、搜索、Agent 控制台、
> 系统配置、信息库、添加记录、使用说明共 8 页均按原型 DOM 落地，首页右侧文档栏
> 默认收起，工具栏为弹窗。
>
> **独立验收正在进行中**：审查方正在跑 **SQLite 全量**与
> **PostgreSQL + 真实 Qdrant + 独立 broker** 回归，输出
> `ui-sqlite.xml` / `ui-postgres.xml`。**在验收结论出来之前，本文不宣称最新界面
> 版本已通过验收。**
>
> **AS711 未测**：本轮全程模型服务不可调用，**未做任何模型探测**，
> 所有模型侧能力（真实问答、AI 撰写文档、真实重排、OCR）**均未验证**。
>
> **原型中的占位功能并非全部可用**（详见 §0）。系统只在**确有真实能力**时才
> 呈现为可用；没有对应能力的原型控件一律以**灰色"暂无状态"、"—"、
> `disabled` + 简短说明**呈现，**不伪造绿灯、不伪造成功**。因此不能把
> "界面已还原"读成"原型里出现的每个运维/反馈动作都能用"。

> 本文由实现方（AI）编写，供审查方（Codex）独立验收对照。
>
> **状态口径（2026-10-14 更新）**：P1–P7 功能已实现，并已由审查方完成
> **独立功能验收与隔离并发验收**；原始记录见
> **[docs/acceptance-2026-10-14.md](acceptance-2026-10-14.md)**（该文件由审查方维护，
> 是唯一权威证据来源）。本文只做**汇总与对照**，数字冲突时以那份为准。
>
> **仍待补（共两项）**：
> ① **本轮原型界面还原的独立验收结论**（进行中，见上方说明与 §8.0）；
> ② **AS711 真实模型侧验收**（真实资料问答 + AI 生成文档）。
> 模型服务此前已能启动并就绪过（见 §5.5 时间线），但**尚未在恢复状态下完成
> 真实 RAG / 生成链路的复验**，且验收期间模型服务再次断开；**本轮全程未探测**。
> **模型验收不得用离线/降级结果替代。**
>
> 本文中"静态检查"类内容仅说明实现方自检的覆盖范围，
> **不构成也不替代**审查方的功能验收（后者见上述验收记录）。

### 开发完成时间

| 里程碑 | 时间 |
|---|---|
| P1–P2 完成并验收 | 2026-10-13 |
| P3–P7 功能实现完成、产品代码冻结 | **2026-10-14 16:57** |
| 审查方独立功能与并发验收完成 | 2026-10-14（记录见 `docs/acceptance-2026-10-14.md`） |
| **最终独立全量回归**（PG 296/1 skip、SQLite 284/13 skip） | 2026-10-14 22:20 后 |
| 交付文档状态定稿 + 最终数字回填（本文/README/部署文档） | **2026-10-14 22:2x** |
| **原型界面还原实现完成**（8 页照原型、首页右文档栏、工具栏弹窗、搜索页撤除自加控件） | **2026-10-14 23:4x** |
| **实现冻结，等待审查方独立验收**（SQLite + PG+真实Qdrant+broker → `ui-sqlite.xml` / `ui-postgres.xml`） | **进行中** |
| **除模型外的开发工作** | **已于 2026-10-14 完成** |
| 剩余项 | ① 原型界面还原的**独立验收结论**（进行中）；② **AS711 真实模型侧验收**（外部依赖，非实现工作，本轮全程未探测） |

---

## 0. 界面还原：哪些是真能用的，哪些是**有意禁用**的

原型里有若干"运维/反馈"占位控件。本系统**没有**为它们编造数据，而是按
"有真实能力才可用"的原则处理。**下面这些不是缺陷，是刻意的诚实呈现**：

| 原型控件 | 本系统实际状态 | 原因 |
|---|---|---|
| Agent「解析看门狗 / 索引队列 / 资源调控 / API熔断器 / 双阶段补扫 / 结果校验」6 张状态卡 | **真实判定来源**（看门狗/结果校验**没跑过巡检**时才显示灰点） | 看门狗/结果校验=最近一次巡检**留痕**（`PatrolRun`）；双阶段补扫=实时候选查询；熔断器/队列=真实快照。**不拿别的能力顶替**（例如不能拿"就绪检查"冒充"结果校验"） |
| Agent 指标「停滞任务 / 重启次数 / 熔断失败数 / 待处理反馈」 | **真实读数** | 停滞=租约为空/已过期的 `RUNNING`；重启次数=本用户**已执行**的 `restart_stalled_task` 条数；熔断失败数=真实快照；待处理反馈=净负分资料份数 |
| Agent 指标「任务成功率」 | **真实计算** | 只统计**已结束**操作（已执行/失败/过期/放弃），不把待确认提案计入分母 |
| Agent 状态卡「索引队列」 | **真实** | 待处理 + 处理中份数 |
| Agent 运维动作「触发一次学习」 | 点击后**明确说明暂不可执行** | 无对应意图，**不显示成功** |
| Agent 运维动作「立即巡检 / 重启停滞任务 / 重扫未处理反馈 / 重置熔断器」 | **真实执行** | 分别接 `run_patrol` / `restart_stalled_task` / `reparse_failed` / `reset_breaker`，走"提案→确认→执行"并写审计；「立即巡检」是**只读**动作（受控流程仍会要求 1 次独立确认，确认后**不改任何资料**） |
| Agent「检索反馈（批注）」表 | **真实数据** | 接 `GET /knowledge/search-feedback/`：只列**自己**的反馈，带资料名、判定中文名与净分（净分与「重扫未处理反馈」同一实现，且只对当前可读的资料给出）。此前是**写死的空态**，现已接通 |
| 搜索「准确 / 不准确」反馈按钮 | **已启用** | `SearchFeedback` 已持久化，并用于净负分定位与加权重排 |
| 搜索「匹配度」 | 仅当检索**真的返回分数**才显示 | 不编造相似度 |
| 系统配置「全部切换为远程」、本地模型三按钮 | `disabled` + 简短 title | 本系统使用远程模型服务，不提供本地推理/批量切换 |
| 系统配置各服务「状态 / 延迟」 | **已配置 / 未配置**（已知事实）+ **"未检测"** | 页面**不主动**逐行探测（避免影响线上）；点「测试连通」才按**该行目标**（embedding / rerank / ocr / llm）真实发一次请求并回填实测延迟，无后台轮询、不启停服务 |
| 系统配置「GPU 集群」 | **未配置** | 确无该项 |
| 文件管理「重置数据库」 | `disabled` + 说明弹窗，**永不执行清库** | 清空资料属不可撤销高危动作，只走**受控删除**（三次独立确认）；批量清理在首页工具栏 |
| 各资料行「解析」 | 全行保留；**已完成/处理中 `disabled`** | 已完成无需重复解析、处理中不可并发 |

> **检索降级会如实标注**：语义检索不可用而回退到关键字时，界面明确写出
> "本次为降级结果"及原因，不伪装成完整语义检索。
>
> **离线/降级结果不等于模型验收。**

---

## 1. 阶段总览

| 阶段 | 内容 | 状态 |
|---|---|---|
| P1 | 不可篡改底座、审计哈希链、健康检查 | 已验收 |
| P2 | 文件入库、解析、向量索引、混合检索、浏览器界面 | 已验收；P3–P7 改动后已随最终回归复跑 |
| P3 | 首页 AI 会话、操作提案、权限核验、执行卡 | **已实现；非模型部分已独立验收**（浏览器只读链路）；AI 生成答案待模型验收 |
| P4 | 三次确认受控删除（事务局部授权 + 分步可重试清理） | **已实现并独立验收**（含级联删除、并发 CAS、首建竞态修复后复测） |
| P5 | 全库/勾选来源写新文档、右侧预览编辑下载 | **已实现并独立验收**（手工草稿编辑/下载、来源勾选、渲染与转义）；AI 撰写待模型验收 |
| P6 | 首页使用说明、并发侦测脚本 | **已实现**；并发验收由审查方独立完成（见 §5.9） |
| P7 | 部署与运维文档、交付清单（本文） | 已完成 |

### 1.1 独立验收结论（审查方，2026-10-14）

> 详细过程与修复前数据保留在 [acceptance-2026-10-14.md](acceptance-2026-10-14.md)。
>
> **注意：下表数字属于此前的功能版本。** 本轮原型界面还原改动之后，
> 审查方正在做**新一轮独立回归**（SQLite 全量 + PostgreSQL + 真实 Qdrant +
> 独立 broker），输出 `ui-sqlite.xml` / `ui-postgres.xml`。
> **在该结论出来之前，最新界面版本尚未取得验收结论**，本文也不据下表宣称
> 最新版本已交付。

| 维度 | **最终结果** | 结果文件 |
|---|---|---|
| SQLite 离线回归 | **284 passed / 13 skipped**，23.43s | `artifacts/acceptance/final-sqlite.xml` |
| **独立 PG + 真实 Qdrant + 隔离真 worker** | **296 passed / 1 skipped**，108.70s | `artifacts/acceptance/final-postgres.xml` |
| 隔离并发（冷/热上传、关键词检索、确认与执行互斥） | 见 §5.9 | — |
| 浏览器功能 | 见 [acceptance-2026-10-14.md](acceptance-2026-10-14.md)「浏览器已验证」+ 下列复验 | `artifacts/acceptance/final-ui-2026-10-14.jpg` |
| **AS711 真实模型（真实问答 + AI 生成文档）** | **仍待补**（不得用降级/离线结果替代） | — |

**跳过项口径**：PG 轮唯一的 skip 是 **AS711 实机用例**；
SQLite 轮的 13 skip 是**真实基础设施与 PostgreSQL 专项**，**不计为通过**。
两轮均有一条"本地 Qdrant 不支持 payload index"的提示，真实 Qdrant 专项另行通过。

**界面复验（最新代码）**：恢复既有 `confirmed_2` 删除提案时**直接进入第 3 次
独立确认弹窗**（不再从第 1 步重来）；**取消后保持 2/3、不删除源资料**；
手工非 AI 草稿再次正常打开，右侧**表格与已保存内容**显示正常。

---

## 2. P3：会话与操作提案

- `apps/planner/llm.py`：OpenAI 兼容 LLM 客户端（真实 HTTP、工具调用解析），
  接入既有韧性层（超时 / 重试上限 / 熔断 / 离线开关）。
- `apps/planner/services.py`：
  - `tool_schema()` 由 `operations.py` **自动生成**（新增能力只改登记表）；
  - AI 的 `tool_calls` **只落成 `OperationRecord(proposed)`**，代码里没有
    "模型直接执行"的路径；
  - 权限**服务端核验**，且**提案时 / 每次确认 / 执行时**各验一次；
  - 目标身份绑定**不可复用标识**（`file:<pk>:<sha256>` / `library:<pk>:<code>`），
    并在确认与执行前核验身份、展示名、（整库）资料清单未变。
- API：`/api/v1/planner/chat/`、`conversations/<pk>/`、`catalog/`、
  `operations/propose|confirm|execute|resume`、`guide/`。
- 模型不可用 → **503 + `assistant_unavailable`**，会话里留系统提示，**不伪造回答**。

**已知限制**

- ~~操作目录只覆盖部分能力~~ → 已扩到 24 项，含 `embedding_profiles`、
  `index_stats`、`rebuild_index`、`activate_generation`、`rollback_generation`
  （全部复用既有服务）。
- 会话历史上下文只取**最新 20 条**（有序取反后再正序），超长会话不会带全量历史。
- `tool_schema()` 的参数类型只映射 `int/str/list`，没有 JSON Schema 的
  `enum`/`minimum`；服务端 `_validate_params` 另行做类型/范围/非空/长度校验。

---

## 3. P4：三次确认受控删除

**已实现**

- **事务局部授权**：`DeleteGrant`（表 `kb_delete_grant`）。授权行由服务层在
  删除的**同一事务**内插入，未提交对其他事务不可见；触发器据此放行。
  **不使用** `drop_guards()` 摘触发器（那会制造全局删除窗口）。
- 触发器（`governance/triggers.py`）：
  - 资料表 DELETE：`EXISTS(未核销 + 未过期 + target_ref = OLD.id)` 才放行；
  - **分段表 DELETE：按 `OLD.file_id` 同样放行**（否则级联删除必被挡）；
  - **审计表 DELETE 仍无条件拒绝**。
- 迁移：`knowledge.0010`（资料守卫）、`knowledge.0011`（分段守卫）、
  `planner.0002`（授权表 + 清理任务表）。部署文档已写明"必须显式应用"。
- 清理拆步：`DeletionJob` 记录 `database` / `vectors` / `blob` 三步，
  `all_steps_ok` 才 `completed`，否则 `partial` 且**可重试**。
- 整库删除：同事务**锁库 → 快照全部资料 → 逐条精确授权删除**，
  再按**登记代次逐个 `delete_collection` 并核验不存在**；
  写审计事件与**审计快照**。只有全部干净才 `complete=True`。
- 向量清理遍历**该库全部登记代次**，按 payload（`file_id` [+ `content_sha256`]）
  批量删除并**核实残留为 0**（不靠 `range(1, chunk_count+1)` 猜 point_id）。
- 检索侧兜底：`_drop_tombstoned()` 在**融合后、重排前**以及**返回前**各过滤一次；
  校验 **fail-closed**（`content_sha256` / `library_id` 缺失即丢弃）。
- 目标锁 + **租约冲突校验**（正在处理的资料拒绝删除）+ 删除前后审计事件。
- 清理任务有受权限保护的 API（`/planner/deletions/`）与页面入口（助手首页"清理与重试"）。

**已知限制**

- **原始字节按设计保留**：内容寻址存储里同一份字节可能被多条记录共用
  （重复上传会复用），物理删除会破坏其它资料。因此 `blob` 步恒为
  "按设计保留"并如实记录，**不会**释放磁盘空间。
- `reset_demo_data` 只接受 `code` 以 `demo-` 开头的库，且需显式传 `library`；
  业务库必须走 `delete_library`。
- `add_to_library`（改归属）**明确拒绝**：归属属不可篡改字段。
  替代路径已写入错误信息（新库重新上传，内容寻址复用同一份只读字节）。
- 整库删除后 `KnowledgeLibrary` 行被真正删除（该表不在不可篡改登记表内），
  可追溯性依赖审计快照与审计事件。
- 级联删除、残留过滤、并发 CAS 已由审查方独立验证（见
  [acceptance-2026-10-14.md](acceptance-2026-10-14.md) 与本文 §5.9）；
  **浏览器端未实际执行过删除**（三次弹窗在第 3 步以错误名称被前端拒绝而停止），
  删除通路的证据来自隔离后端回归。
- 并发删除/执行在真实负载下的表现**只验证到"互斥正确"**（1 成功 + N 冲突），
  **未做容量结论**。

---

## 4. P5：派生文档

**已实现**

- `compose_document()` 支持两种来源：**用户勾选的具体资料**（逐个核验权限与
  完成状态）或**按主题全库检索**；检索为空时**明确拒绝**，不生成无依据内容。
- 派生文档**可编辑**（`DerivedDocumentView` **不带** `@immutable_endpoint`——
  不可变层保护的是源资料，草稿本就该能改）；写入约束是"只有 `created_by` 能改"，
  且**永不回写源资料**。
- 导出：`?export_format=md` 返回真实 `.md`；`?export_format=html` 返回**独立 HTML**，
  正文由 `apps/planner/markdown.py` **真实渲染**（与前端 `kb-markdown.js` 同一套规则，
  已实测标题/粗体/表格/代码块与右侧预览一致），响应头 `X-Document-Format` 标注实际格式。
  > 注：参数名是 `export_format`，**不是** `format`——后者是 DRF 的 renderer 协商参数，
  > 在进入视图前就会被拦掉（曾导致下载 404）。
- 前端：`kb-markdown.js` 零依赖渲染器（先转义、白名单标签），支持标题、
  粗体/斜体、行内代码、围栏代码块、有序/无序列表、引用、表格、分隔线、链接。
- 右侧面板：预览 / 编辑 / 保存 / 下载（Markdown、HTML）。

**已知限制**

- 渲染器**不是完整 CommonMark**：不支持 setext 标题、引用块嵌套、嵌套列表缩进、
  脚注、参考式链接、HTML 内嵌（后者是**故意**的：避免 XSS）。
- 前端与服务端是**两套实现、同一套规则**（JS 与 Python 无法共享代码）：
  改动渲染规则时两处都要改，否则导出与预览会漂移。
- 来源选择器一次最多列出 **50 份**资料（无分页）。
- `load_probe.py` 的 upload 模式写入 `load-test` 库时需要该库已存在。

---

## 5. P6：说明与并发

**已实现**

- `/api/v1/planner/guide/`（使用说明，与能力目录同源）+ 助手首页"使用说明"卡片。
- `scripts/load_probe.py`：环境变量认证、**真实 multipart 上传**、
  **同一合法一次性 token 并发确认**、只读操作并发执行、网络异常分类、
  成功率/吞吐/P50-P95-P99、**401/403 计为失败**、精确 hostname 仅允许本机、
  要求目标服务模型已禁用（避免间接压 AS711）。

**已知限制**

- 并发验收**由审查方用自建脚本完成**（见 §5.9），`scripts/load_probe.py`
  本身**未在验收中被采用**，因此它自身没有被实测过的运行记录。
- 旧版曾把"propose upload_file"当成上传压测，那不算真实上传；现已改为真 multipart。
- **`assert_models_disabled()` 现在是 fail-closed 的**（不再只是提示）：
  它读服务端 `/guide/` 的 `environment`，要求 `model_calls_disabled=true`
  **且** `load_test_mode=true` **且** `dispatch_disabled=true` **且** 向量前缀以
  `kb-lt-` 开头，否则**拒绝发压**。
- 承上：正式服务（含 8000 的真实模型配置）**不会被该脚本压测**——这是机制保证，
  而不是靠人记得。配套 `scripts/check_loadtest_guards.py`（27 项，含负例）验证过闸门。

---

## 5.5 AS711 联调状态（**两类事实必须分开看**）

| 项 | 状态 | 证据 |
|---|---|---|
| 四个模型**简单调用** | ✅ 曾成功 | embedding HTTP 200 / 实际 1024 维；rerank HTTP 200 `top_index=0, score=0.97265625`；Qwen3.6-35B-A3B-FP8 HTTP 200 正确回复"连接正常"；OCR HTTP 200 正确识别 3 行（`artifacts/acceptance/ocr-live.png`） |
| **真实 RAG / 生成链路** | ⚠️ **仍待补**（不得用降级/离线结果替代） | ①修掉"两条 system"后不再 400；②16:31 曾出现 AS711 HTTP 500 `EngineCore encountered an issue`，重试一次后 UI 如实 503；③**服务恢复窗口内单次 RAG 形状请求已返回 HTTP 200 并给出引用来源的回答**（见 §5.5 时间线）；④但验收期间 AS711 再次断开，**完整照护链路未跑完** |

**关于这次 500 的纪律**：这是**推理服务内部错误**，**原因未知**。
因此本文档**不做任何原因猜测**（不是 OOM、不是模型不兼容、不是配置问题——无证据），
也**没有**为它添加任何"伪造回答/静默降级"的兜底：助手就是**如实 503 +
`assistant_unavailable`**，会话里留系统提示，不编内容。

已遵循的约束：未修改/未重启 AS711 任何服务；未重复发起大批 LLM 请求。

**16:31 之后的时间线补充（2026-10-14）**

- AS711 主机经**物理重启**；四个服务以 Docker 容器方式部署在 3 个 compose 项目里
  （`deploy/qwen3.5`、`deploy/bge`、`deploy/paddleocr`），且 `RestartPolicy=no`
  → **重启后不会自启**，这就是"连接被拒绝/超时"的原因（主机在线、端口无监听）。
- 经用户授权后，按原 compose 方式启动这四个服务；四端口 `/health` 均转 200，
  `manage.py kbprobe --probe` 三项**全部就绪**，OCR 也能**正确识别出预期文字**
  （同时印证了服务端日志里 1×1 图片导致的处理器错误就是当初的假阴性来源）。
- **单次** RAG 形状请求（唯一首条 system + 资料上下文 + tools）返回 HTTP 200，
  内容为引用来源的真实回答（"根据《合同.txt》的内容，质保期为 12 个月。"）
  → 说明**两条 system 的 400 与 EngineCore 500 在服务恢复后均不再出现**。
- 但验收期间 AS711 **再次断开**，因此上述属于**恢复窗口内的单次连通性确认**，
  **不构成真实 RAG / 生成链路的完整验收**；该链路仍标注为**待补**。

**复验方式**（模型恢复后执行，仍属待办）：
1. `manage.py kbprobe --probe`（4 项，含合理尺寸 OCR 样本）；
2. 浏览器：上传一份小资料 → 处理完成 → 助手首页就该资料提问，
   预期返回**基于资料的回答**（HTTP 200），而不是 503；
3. 再走一次"生成派生文档"，预期产出真实 Markdown 草稿。

---

## 5.6 已修复的两条真实缺陷（本轮联调暴露）

1. **两条 system 消息 → AS711 HTTP 400**
   `"System message must be at the beginning."`：`reply_to_message` 与
   `compose_document` 都曾拼"系统提示 + 资料片段"两条 system。
   现在合并为**唯一首条 system**（资料区显式标注为**不可信引用数据**，
   权限仍由服务端判定、不因资料内容改变）；并在 `LlmClient.normalize_messages`
   做规范化兜底（开头连续 system 合并、非首位 system 降级为 user）。
2. **`loadJobs` 未定义 → 后续初始化全部中断**
   替换 `renderMarkdown` 段落时误删了清理任务函数，`init` 里
   `await loadJobs()` 抛 `ReferenceError`，导致其后的 `loadGuide()`
   永不执行（"使用说明"一直显示"加载中"、清理重试没有入口）。
   已恢复，并新增静态审计（见 §7）兜住"调用了但未定义"。

---

## 5.7 执行窗口（ARMED 不是永久的）——本轮补上的安全边界

**问题**（审查方指出）：确认完成后操作进入 `ARMED`，而 `execute_operation`
原本不再核验任何时效 → **ARMED 可以无限期执行**。这等于把"三次确认"
退化成"某一天确认过"：用户当初确认的目标/上下文可能在几小时后已经变了
（资料补传、库改名、价格表更新），拿旧确认执行已不是用户同意的那件事。

**修法**

- `OperationRecord` 新增 `armed_at` 与 `armed_expires_at`；
- 每次进入 `ARMED` 时记录窗口（`KB_ARMED_TTL_SECONDS`，默认与
  `KB_ACTION_TICKET_TTL_SECONDS` = 900 秒一致，避免"凭证还有效但操作已过期"错位）；
- `execute_operation` 在**独立事务**里核验窗口并**先提交** `EXPIRED` 再抛错
  （踩过的坑：把标记与 raise 放同一个 `atomic()` 里，抛错会把状态更新一起回滚，
  操作会一直停在 `ARMED`）；
- 过期后**要求重新提案**，不提供"自动续期"——续期等于把旧确认当新确认用；
- `POST /operations/<pk>/resume/` 对已过期的 ARMED 返回 **409 `armed_expired`**；
- `armed_expires_at` 为空（没记录窗口）**一律视为过期**（fail-closed）。

**回归覆盖**

- `test_armed_expires_and_execute_is_refused`：走完**三次确认** → 把窗口推到过去
  → `execute` 被拒 → 操作落到 `EXPIRED` → **资料未被删除**（过期确认不产生副作用）；
- `test_armed_without_expiry_is_fail_closed`：窗口为空也拒绝；
- `test_resume_refuses_expired_armed`：恢复接口返回 409 `armed_expired`。

（此前只有"确认时凭证过期"的用例，确实证不了"ARMED 后能否无限期执行"。）

---

## 5.8 负载测试环境的 fail-closed 准备

**服务端自述**：`GET /api/v1/planner/guide/` 新增 `environment` 字段（服务端事实）：

```json
{
  "model_calls_disabled": true,
  "load_test_mode": true,
  "dispatch_disabled": true,
  "qdrant_collection_prefix": "kb-lt-verify",
  "upload_process_mode": "queue",
  "test_adapter": "",
  "database_name": "kb_loadtest_verify"
}
```

`database_name` **只在 `load_test_mode=true` 时**返回（正式环境不回传库名）。

**压测脚本三道闸门**（`scripts/load_probe.py`，全部 fail-closed，不再是提示）：

1. 目标服务必须自述 `model_calls_disabled=true` **且** `load_test_mode=true`
   **且** `dispatch_disabled=true` **且** 前缀以 `kb-lt-` 开头，否则**拒绝发压**；
   缺 `environment` 字段（旧版本）也拒绝。
2. `upload` 只能写 `load-test-` 前缀的库，否则拒绝。
3. `execute` 只允许**只读白名单** intent，并与服务端目录的 `is_mutation`
   交叉核对——冲突时按**更严格**一方处理。

**独立负载测试 settings**：`knowledgebase/loadtest_settings.py`

- 先正常导入业务 settings（连接参数来自 `.env`），**再强制改写**模块属性，
  因此 `.env` 的 `override=True` **盖不回去**；
- 强制：模型禁用、`load_test_mode`、禁止派发、独立 Redis DB、`kb-lt-` 前缀、独立库名；
- **拒绝启动**的情形：缺参数 / 库名等于业务库 `kb` / Redis DB 等于业务 DB `0` /
  前缀不是 `kb-lt-` / 落点变成 SQLite；
- `KB_DISPATCH_DISABLED=1` 时 `dispatch_processing()` 直接返回
  `reason="dispatch_disabled"` 且**不产生 task_id**（任务不会进业务队列）。

**配套工具**

- `scripts/loadtest_env.py`：生成一套合规参数并打印可直接复制的命令；
- `scripts/check_loadtest_guards.py`：**27 项**校验（隔离落地、派发被拒、
  `/guide/` 自述、以及各闸门的**正例与负例**）。

**已准备好的测试服务**（开发方起在本机，供审查方直接压）：

| 项 | 值 |
|---|---|
| 地址 | `http://127.0.0.1:8011`（**8000 保持真实模型配置，不可用于压测**） |
| 库 | `kb_loadtest_verify` |
| 向量前缀 | `kb-lt-verify` |
| Redis | `redis://127.0.0.1:56379/14`（业务用 DB0，互不干扰） |
| 模型 | 已禁用（不会打到 AS711） |
| 派发 | 已禁止 |
| 账号 | `loadbot`（staff，口令请审查方自行重置） |
| 业务库 | `load-test-main`（`load-test-` 前缀，可被 upload 压测写入） |

> 该负载测试服务（8011）在验收完成后已**停止**；8012（双 worker）同样未在运行。
> **8000 由审查方恢复并在运行**，用于最终界面复验。需要重建 8011 时按下面命令。

重启命令（若该服务退出）：

```powershell
$env:DJANGO_SETTINGS_MODULE='knowledgebase.loadtest_settings'
$env:KB_LOADTEST_DB_NAME='kb_loadtest_verify'
$env:KB_LOADTEST_QDRANT_PREFIX='kb-lt-verify'
$env:KB_LOADTEST_BROKER_DB='14'
$env:KB_LOADTEST_LABEL='loadtest-verify'
.\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8011 --noreload
```

---

## 5.9 隔离并发验收结果（审查方独立，2026-10-14）

> 以下数字由审查方在**独立负载测试服务**上实测（PostgreSQL `kb_loadtest_verify`、
> Qdrant 前缀 `kb-lt-verify`、Redis DB14、**模型禁用**、**禁止派发**）。
> 原始记录见 [acceptance-2026-10-14.md](acceptance-2026-10-14.md)。

| 场景 | 成功 / 请求 | 时间 | P50 / P95 | 吞吐 |
|---|---:|---:|---:|---:|
| 8011 开发服务，新库**首批**上传，4 并发 | **20 / 20**（HTTP 202） | 11.22s | 0.0921 / **11.2206s** | 1.78 req/s |
| 同库**第二批**上传，4 并发 | **20 / 20**（HTTP 202） | 0.91s | 0.1056 / **0.4945s** | 21.94 req/s |
| 8012 ASGI，2 个 worker，8 并发关键词检索 | **116 / 116**（HTTP 200） | 10.16s | 0.6349 / **1.6145s** | **11.41 req/s** |
| 同一确认凭证 8 并发 | **1×200 + 7×400**（HTTP 400 = 预期拒绝） | — | — / 0.1028s | — |
| 同一已确认只读 `get_stats` 8 并发执行 | **1×200 + 7×409**（HTTP 409 = 预期拒绝） | — | — / 0.0797s | — |

三次上传/检索运行**无网络错误**。测试库 `load-test-final-a7f3` 最终 **40 份文件均留存**；
首批完成时检查**活动代次唯一且指针一致**。为关键词检索准备了 **57 份隔离原件对应的
57 个真实文本分段**，**未生成假向量**。116 次检索**全部如实标注模型禁用后的降级**。

**修复前对照**（保留，见验收记录）：同样冷启动 4 并发 20 份时
17×202 + **3×500**（成功率 85%），根因是首建竞态（指针指 g4、登记 g1 active）——
该缺陷已修。

**口径提醒（重要）**

- 上表是**本机小样本入口 + 关键词降级 + 并发互斥**的验证，
  **不是**生产容量、全文件解析速度、模型吞吐或真实 AI 生成性能结论。
- 后两行的 7 次拒绝是**预期的重复提交防护**（一次性凭证 + 执行端 CAS），
  **单个赢家才是验收条件**，不能解读成"12.5% 业务可用率"。
- 压测实例**均关闭模型调用与任务派发**；业务队列未清空、未启动消费。

---

## 6. P7：部署运维

- [docs/deployment-and-operations.md](deployment-and-operations.md)：端口/路径、启动、
  队列与 worker、运维命令、运行期开关、故障处置、**触发器刷新迁移必须应用**、
  测试命令与隔离口径（含 `KB_PG_TEST_DB_NAME` 才是真正隔离的那个变量）。
- 本文档。

---

## 7. 实现方静态自检的覆盖范围

> 下表只说明实现方**自检**做到哪一步，**不构成也不替代**审查方的功能验收。

- `manage.py check` 干净；`makemigrations --check` 无待生成迁移。
- 迁移 `knowledge.0010` / `0011`、`planner.0002` / `0003` 已应用；核对了 Postgres 里
  **函数体**确实含 `kb_delete_grant`（`kb_guard_ingested_file_delete_guard`、
  `kb_guard_chunk_delete_guard` 为真，`kb_guard_append_only_guard` 为假）。
- 所有新增/改动 Python 文件通过 AST 解析；5 个 JS 文件通过 node 解析。
- `scripts/check_markdown.js`：**前端**渲染器 12 项单元检查通过（含 XSS 剥离）。
- `apps/planner/markdown.py`：**服务端**渲染器 12 项规则检查通过（与前端同规则）。
- `scripts/check_js_symbols.js`：**JS 顶层符号审计**——找出"被调用但未定义"的
  标识符，并单独断言 `init` 里每个 `await X()` 都有定义。
  （这条是真实事故的补丁：`loadJobs` 被误删时 `new Function(src)` 仍解析通过，
  只有这个审计能静态抓住。）
- `ruff check --select F821,F811`：**0 项**（`append_event` 那类未定义名已清零；
  并借此删掉 `resilience.py` 里一个"只有 docstring、被静默覆盖"的同名死函数）。
- `scripts/check_loadtest_guards.py`：负载测试三道闸门 **27 项**（含负例）全部按预期拒绝。
- `scripts/load_probe.py`：非本机目标 / 缺认证 / 缺写开关三种情形均被拒绝。

---

## 8. 待补项（其余均已由审查方独立验证）

> 曾经的"待验证清单"绝大部分已有独立结论，见
> [acceptance-2026-10-14.md](acceptance-2026-10-14.md) 与本文 §5.9。
> 目前剩两项：

0. **原型界面还原的独立验收结论（进行中）**：本文 §0–§1 所述 8 页原型还原
   （含首页右侧文档栏、工具栏弹窗、搜索页撤除自加控件、结构化筛选服务端收敛）
   已实现并冻结，审查方正在跑 SQLite 与 PostgreSQL + 真实 Qdrant + broker
   两轮回归（`ui-sqlite.xml` / `ui-postgres.xml`）。
   **在结论出来之前不声明界面版本已验收。**

1. **AS711 真实模型侧验收**（**模型侧唯一剩余待补项**）：真实资料问答 + AI 生成文档
   的完整链路，**必须在模型可用时复验**，不得用降级/离线结果替代（见 §5.5）。
   - 已完成的只是**恢复窗口内的单次连通性确认**（HTTP 200 + 引用来源的回答）。
   - 依据审查方结论："当前**不声明整个模型链路验收完成**"。
   - **本轮全程未做任何模型探测**（AS711 断开）。
   - **含"真实新模型 / 2048 维实机迁移"**：切换模型并做真实迁移这一步需要
     AS711 上的目标模型在线，因此归入本项（模型待补），
     **不是**一个未完成的本地功能。
2. ~~换模型 / 回退后重建路径不改动当前指针：静态已复核，动态仍未验~~
   → **已有隔离回归通过**（见下表），不再列为待办。

**已有独立结论、不再列为待办的**（避免重复劳动）：

| 曾经的问题 | 独立结论 |
|---|---|
| 级联删除（授权感知分段守卫） | 隔离后端回归已通过 |
| `_drop_tombstoned` 残留过滤 / fail-closed | 回归已覆盖 |
| 整库 `delete_collection` 与不存在核验 | 回归已覆盖 |
| 确认/执行**并发唯一性** | 8 并发确认 1×200+7×400；执行 1×200+7×409（§5.9） |
| 真实 worker 往返 | **296 passed / 1 skipped**（含隔离真 worker） |
| 首建竞态 | 修复后首批上传 20/20、活动代次唯一且指针一致（§5.9） |
| 恢复流程 UI 缺陷（resume 丢进度） | 浏览器复验：`confirmed_2` **直接进第 3 弹窗**、取消不删除 |
| 最终全量回归数字 | 已回填至 §1.1 |
| **重建写目标代次不得移动任何指针** | ✅ **隔离回归通过**：`tests/test_concurrency_and_rebuild.py:215` `test_rebuild_target_write_does_not_move_any_pointer`，断言**库指针 + 资料全部三个字段**（`vector_collection` / `vector_collection_name` / `vector_generation`）均不变 |
| **原子 activate / rollback / 覆盖完整性** | ✅ **隔离回归通过**：`tests/test_vector_profiles.py`（`test_activate_generation_switches_library_and_files_atomically`、`..._refuses_incomplete_coverage`、`..._refuses_mismatched_text`、`..._leaves_only_one_active`、`test_rollback_switches_back_without_reembedding`、`test_prepare_after_rollback_does_not_reuse_history`、`test_failed_generation_number_is_not_reused` 等） |

> 以上两组均已包含在**最终全量 296 passed** 中（见 §1.1）。

### 8.1 实现方自测记录（**不替代**审查方结论）

| 运行 | 库名 | 结果 |
|---|---|---|
| 全量 + 真实 Qdrant/worker（修复后期） | `kb_dsh_alloc` | `296 passed / 1 skipped`（skip = AS711 marker） |

与审查方最终结果（§1.1）数值一致，但这只是"在实现方的隔离环境里通过"；
**验收依据以审查方独立结果与结果文件为准**。

---

## 9. 环境备注（供审查方参考）

- `.env`：`KB_MODEL_CALLS_DISABLED=0`、`KB_UPLOAD_PROCESS_MODE=auto`、
  `CELERY_TASK_ALWAYS_EAGER=0`。
  > 模型侧验收期间 AS711 会断开：此时真实链路表现为"如实 503 + 模型不可用提示"，
  > 若要让非模型链路跑得快且不空等超时，可临时置 `KB_MODEL_CALLS_DISABLED=1`。
- 业务 Redis 队列（DB0）里仍有**历史遗留的 182 条测试消息**，来自
  "测试尚未隔离默认派发"之前的运行。**故意不清空**（删别人的消息风险更大）；
  任务侧会拒绝缺少 `content_sha256`/`library_code` 绑定的旧消息。
- **当前端口状态**：**8000 由审查方恢复并在运行**（最终界面复验用）；
  **8011 / 8012 均无监听**（隔离负载测试服务已停止，需要时按 §5.8 命令重建）。

### 9.1 实现方在这套环境里留下的东西（请审查方知悉，可自行处置）

- **PG 库**：`kb_dsh_fix_verify`、`kb_dsh_sysfix`、`kb_dsh_jsfix`、`kb_dsh_race`、
  `kb_dsh_alloc`、`kb_dsh_ui3` 等**测试库**（经 `KB_PG_TEST_DB_NAME` 指定，
  Django 会加 `test_` 前缀），以及负载测试库 `kb_loadtest_verify`。
  **未动** `kb` 业务库的既有数据。
- **账号**（审查方已决定处置方式，此处仅记录）：
  - 实现方建的 `browsercheck`（staff，浏览器复验用）将与审查方的
    `codex_review` 临时验收账号**一并停用，保留记录**；
  - `loadbot`（staff，压测用）**只存在于隔离负载测试库**（`kb_loadtest_verify`），
    **不计入主库账号口径**。
- **派生文档**：1 份《浏览器复验文档》（`DerivedDocument` id=1，创建者 `browsercheck`）。
- **未做任何删除**：没有删业务资料、没有删库、没有清队列。
- 若需清理，请审查方按自己的流程处置（这些都不属于"已入库源资料"）。

### 9.2 版本状态

**产品代码与测试代码自 2026-10-14 实现冻结**：审查方已开始独立回归
（SQLite 全量 + PostgreSQL + 真实 Qdrant + 独立 broker，输出
`ui-sqlite.xml` / `ui-postgres.xml`），在给出验收结论之前，实现方
**不再改动代码**（除非审查方点名要求修某个具体问题），也**不再重复跑整套测试**。

本次文档更新**只改文档**（`docs/delivery-status.md`），未触碰任何产品/测试代码。

**冻结范围**：`apps/`、`governance/`、`templates/`、`static/`、`tests/`、迁移文件。
**未验证项**：AS711 真实模型侧（本轮全程未探测）；
原型占位运维/反馈控件**并非全部可用**——见 §0 的逐项说明。
