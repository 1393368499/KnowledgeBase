# 功能 / 操作矩阵（逐项验收表）

> 本表是**唯一的功能验收口径**：每个原型页面控件、每个 AI 意图一行，
> 逐项注明实现位置、API、权限、以及**已有哪一类证据**。
>
> **状态只有三种**：
> - ✅ **已实现并有证据**（写明证据类型）
> - ⚠️ **部分实现**（写明缺什么）
> - ❌ **待做**（不得以"原型占位/有意禁用"当作完成）
>
> ⚠️ **重要口径纠正（审查要求）**：表中的 ✅ **不等于独立验收通过**。
> 仅有源码/单测证据的条目，只能说明"实现存在且自测覆盖"，
> **最终验收以审查方的独立全量回归与浏览器复验为准**。
> 没有真实模型证据的 AI 能力、没有并发/恢复证据的路径，都不得标为"验收完成"。
>
> 证据列取值：`实现`(代码路径) / `API`(端到端接口) / `权限`(服务端核验) /
> `单测`(pytest) / `集成`(跨组件) / `真模型`(AS711 真实调用) / `页面`(浏览器复验) /
> `并发`(隔离并发与恢复)
>
> 生成依据：`apps/planner/operations.py` 的 `all_specs()`（脚本
> `scripts/dump_intents.py` 直接导出，非人工誊抄）。

---

## 0. 硬约束（全表适用）

| 约束 | 落实机制 | 证据 |
|---|---|---|
| 源资料不可修改 | DB 触发器守卫（补集规则：`CONTROLLED_MUTABLE_FIELDS` 之外全部锁定）+ 迁移 0012/0013 | 单测：改 `amount_wan` 被拒；`existing_columns` 跨 SQLite/PG |
| 删除需三次独立确认 | `DESTRUCTIVE_CONFIRMATIONS = 3`；第 3 次必须**原样**输入目标名；一次性凭证 + 执行窗口 | 集成 + 真模型：多轮链路 12/12（`artifacts/phase1-multiturn-delete.json`） |
| 不得启停/修改 AS711 其他服务 | 代码中**只有**只读探测路径，无任何 start/stop 接口 | 实现（`KnowledgeModelServiceView` 仅 GET） |
| 原型布局严格保持 | `kb-proto.css` 为原型 `<style>` 逐字复制，未改；首页仅新增右侧文档栏（默认收起） | 页面：8 页浏览器复验通过（审查方记录） |

---

## 1. 首页（AI 会话）

| 控件 / 功能 | 状态 | 实现 | API | 权限 | 证据 |
|---|---|---|---|---|---|
| 欢迎态（5 统计 / 6 快捷句 / 4 能力卡 / 资料库区 / 底部说明） | ✅ | `kb-assistant.js welcomeHtml()` | `GET /knowledge/stats/` | 登录 | 页面；计数为服务端真实值 |
| 固定底部 composer + Enter 发送 | ✅ | `composerHtml` 结构 | `POST /planner/chat/` | 登录 | 页面 |
| 发送防重（发送中禁用按钮，`finally` 恢复） | ✅ | `kb-assistant.js send()` | — | — | 实现 |
| 真实资料问答（RAG，含来源引用） | ✅ | `services.reply_to_message` → `build_context_report` | `POST /planner/chat/` | `library:read` | **真模型**：引用「36 个月 / 赵启明」，未串「12 / 孙念」（`artifacts/phase1-run3.log`） |
| 多轮上下文（"刚才那个"有指代） | ✅ | `services.executed_history_summary` | 同上 | 服务端按当前权限重过滤 | 单测 ×5 + **真模型**：同会话第 2 轮正确提出 `delete_file` |
| 检索降级如实提示（"部分结果"） | ✅ | 降级状态进返回体 + `assistant_message.payload`，`renderMessage` 持久渲染 | 同上 | — | 单测：范围说明送达 + 历史保留 degraded |
| 操作卡（待确认 / 三独立确认 / 执行窗口） | ✅ | `renderOperationCard` + `KB.controlled` | `/operations/propose·confirm·execute·resume` | 每次确认都核验 | 集成：8 并发确认 1×200+7×400（互斥，符合预期） |
| 手动提案（工具栏） | ✅ | `openTools` + `openParamForm` | `POST /operations/propose/` | 服务端 | 页面 |
| 参数表单按 `catalog.params` 生成中文输入 + 类型校验 | ✅ | `openParamForm` | `GET /planner/catalog/` | — | 实现（24 意图的真实参数类型） |
| 待办操作（未绑定会话的提案持久展示） | ✅ | `loadPendingOps` | `GET /planner/operations/` | 本人 | 实现 |
| 来源选择（勾选 → 指定来源撰写） | ✅ | `searchSources`（服务端 `status=done` + 分页 + 加载更多） | `GET /knowledge/files/` | `library:read` | **真模型**：指定来源撰写 `sources=[29]`，仅含所选资料事实 |
| 上传（选库确认，来源 `session`） | ✅ | `uploadFiles` → `KB.choice` | `POST /knowledge/files/upload/` | `library:write` | 集成：上传 202 + 真实 BGE 解析嵌入 |
| 右侧文档栏（默认收起 / 编辑 / 预览 / MD+HTML 下载） | ✅ | `doc-pane` + `kb-home.css`（新增样式，未改原型 CSS） | `GET/PUT /planner/documents/`、`download/?export_format=` | 仅本人（非本人 404） | **真模型** + 集成：撰写、保存、下载 md/html 通过 |
| 会话历史切换 / 新会话 / 清 URL | ✅ | `kb-nav.js renderSessions(activeSession)` | `GET /planner/conversations/` | 本人 | 集成 |
| 环境徽标（准确状态，不暗示在线） | ✅ | `renderEnvBadge` | `GET /planner/guide/` | — | 实现：`模型服务未检测` / `模型调用已禁用` |
| 工具弹窗内"清理与重试" | ✅ | `loadJobs` | `GET/POST /planner/deletions/` | `system:operate` | 集成：`complete=false` 可重试 |

## 2. 文件管理

| 控件 / 功能 | 状态 | 实现 | API | 权限 | 证据 |
|---|---|---|---|---|---|
| 5 统计卡（全部/已完成/处理中/待处理/失败） | ✅ | `loadStats` | `GET /knowledge/stats/` | 登录 | `processing` 为真实 RUNNING 数 |
| 10 列表格 + 分页（`page_size=10`） | ✅ | `query()` | `GET /knowledge/files/` | 可读库 | 单测 ×3（页大小/翻页不重复/全量可达） |
| 筛选：关键词（**按文件名**）、信息库、类型、状态、会话来源 | ✅ | `query()` | 同上（服务端过滤） | 可读库 | 单测：`mode=filename` |
| 「来自 AI 会话」按钮带**真实**份数 | ✅ | `loadStats` → `session_files` | 同上 | — | 实现（精确 `source="session"`） |
| 上传（单文件 / 目录扫描，**来源固定 manual/scan**） | ✅ | `uploadFiles` | `POST …/upload/` | `library:write` | 集成；目录保留完整相对路径 |
| 每行 解析 / 重新解析（已完成、处理中 `disabled` + 说明） | ✅ | `renderRows` | `POST …/process/` | `library:write` | 集成 |
| 「全部解析」按当前筛选**全分页**取，取不全即取消并如实报告 | ✅ | `fetchAllFiltered`（沿 `next` 循环；失败/超页上限抛错取消） | 同上 | `library:write` | 实现 + 单测 |
| 预览（真实分段） / 详情弹窗 | ✅ | `openPreview` / `openDetail` | `GET …/<id>/`、`…/chunks/` | 可读库 | 集成 |
| 删除（走受控提案，三次确认） | ✅ | `KB.controlled({intent:'delete_file'})` | `…/propose·confirm·execute` | `library:admin` | **真模型** 12/12 + 集成 |
| 「重置数据库」 | ✅ **已实现为「重置信息库索引」（受控重建，不是清库）** | 信息库页每库「重置索引」按钮 → `KB.controlled({intent:'reset_library_index'})`；文件管理页原有的「重置数据库」说明弹窗**保留安全禁用**口径（那里入口从不直接清库） | `…/propose·confirm·execute` | `system:operate` | **真模型** 17/17 + **真实 UI 15/15**（`artifacts/acceptance/reset-library-index-ui.json`：错名被服务端挡下且不发 `/execute/`；按确认卡库名原样输入后执行 200，实测只删除 1 行失效登记）。语义=**逐份按只读原件强制重跑**分段与向量；业务记录与原件一行不动；**只有全部重建成功**才清理不再生效的旧代次，失败如实报 `PARTIAL` 且**不清理任何旧代次** |

## 3. 搜索

| 控件 / 功能 | 状态 | 实现 | API | 权限 | 证据 |
|---|---|---|---|---|---|
| 关键词 + **3 模式**（文件名+内容 / 只搜文件名 / 只搜正文） | ✅ | `runSearch` | `GET /knowledge/files/`（服务端 `mode`） | 可读库 | 单测 |
| 部门 / 客户 / 资料性质 / 金额下限（**服务端**过滤） | ✅ | `structuredQuery` | 同上 + `/files/facets/`（服务端聚合计数） | 可读库 | 单测（含 `amount_min` NaN/inf → 400） |
| 空条件结果区**空白**（不列全部） | ✅ | `hasAnyCondition` → `renderBlank` | — | — | 实现 |
| 信息库下拉 | ➖ **按需求移除** | 界面不显示（"除首页右文档栏外不能加控件"）；后端 `libraries` 过滤接口保留、URL 条件支持 | `GET /knowledge/libraries/` 保留 | — | 单测：界面无该控件、接口仍可反解 |
| 混合检索（稠密+关键词+重排） | ✅ | `hybridSearch` → `POST /knowledge/search/` | — | 可读库 | **真模型**：BGE/rerank 真实调用 |
| 结构化条件**先收敛候选再检索**（不取 top50 前端过滤） | ✅ | `SearchRequestSerializer` 收条件 → `SearchView` 全量收敛 `eligible_file_ids` → dense/sparse/DB/重排前四处受限 | `POST /knowledge/search/` | 可读库 | 单测 ×6（>200 不截断 / 越权 / 无候选不外呼 / 重排前已筛 / None no-op / 顺序断言） |
| 空条件**绝不调用模型** | ✅ | 无候选立即空结果 | — | — | 单测 |
| 结果卡 `.chunk` 正文摘要 + `.meta`（匹配度/预览/准确/不准确） | ✅ | `resultCard`（匹配度仅当服务端真给分数） | 同上 | — | 实现 |
| 「预览」真正打开分段 | ✅ | `previewFile` | `GET …/chunks/` | 可读库 | 页面 |
| **「准确 / 不准确」反馈** | ✅ | `POST /knowledge/search-feedback/`（`SearchFeedback` 模型）：可点、**可改判**（同一"用户+查询词+资料+分片"唯一约束，重提交=改判而不是堆矛盾记录）、落审计；`GET` 只列自己的反馈并带净分 | `POST /knowledge/search-feedback/` | 本人 | `tests/test_search_feedback.py`、`tests/test_feedback_weight.py` |
| 语义检索降级如实标注 | ✅ | `report.degraded` → 明确写「本次为降级结果」 | 同上 | — | 实现 |

## 4. Agent 控制台

| 控件 / 功能 | 状态 | 实现 | API | 权限 | 证据 |
|---|---|---|---|---|---|
| 6 状态卡（解析看门狗/索引队列/资源调控/API熔断器/双阶段补扫/结果校验） | ✅ **6/6 有判定来源** | 索引队列=待处理+处理中；资源调控=真实队列深度（DB14）；API熔断器=真实熔断快照（**口径：进程内**）；解析看门狗/结果校验=最近一次巡检留痕（`PatrolRun`，**没跑过就灰点**）；双阶段补扫=实时候选查询 | `GET /knowledge/stats/`、`GET /knowledge/runtime-metrics/` | 指标=系统管理员；其余本人 | 实现 + `tests/test_patrol_and_integrity.py`、`tests/test_webui_agent_cards.py` |
| 5 指标（停滞任务/重启次数/熔断失败数/任务成功率/待处理反馈） | ✅ **5/5 有真实来源** | 停滞任务=租约过期/为空的 RUNNING 实时数；重启次数=本用户**已执行**的 `restart_stalled_task` 条数（审计记录里数出来）；熔断失败数=真实快照；任务成功率=已结束操作；待处理反馈=净负分资料份数 | `GET /knowledge/runtime-metrics/`、`GET /planner/operations/`（`summary.restart_stalled_executed`） | 系统管理员 / 本人 | 实现 |
| 事件时间线 | ✅ | 最近 8 条操作 | 同上 | 本人 | 页面 |
| 「立即巡检」 | ✅ | `run_patrol`：立刻跑一次与周期任务**同一实现**的只读巡检（重试用尽/停滞/向量不完整/原件缺失）并留痕，界面读数随即刷新 | `POST /operations/propose/` | `system:operate` | 实现（`manage.py kbpatrol` 同一实现） |
| 「重启停滞任务」 | ✅ | `restart_stalled_task`：判定来源=租约为空/已过期的 `RUNNING`（与 `integrity.stalled_running_queryset()` 同口径），逐份**真的** `process_file`，按实际结果分 processed/failed/lease_busy/incomplete | 同上 | `system:operate` | `tests/test_restart_stalled_task.py` |
| 「重置熔断器」 | ✅ | `reset_breaker`（已登记）；熔断状态已作为「API熔断器」卡与「熔断失败数」指标暴露 | 同上 | `system:operate` | 实现 |
| 「查看学习规则」（原型叫「触发一次学习」） | ✅ | **本系统不做训练**，所以按钮改成实话：只读意图 `feedback_learning_report`，返回有界加权口径（`MAX_ADJUST`=0.15、只影响排序）与当前反馈计数——**不假装已经"学习"过** | `POST /operations/propose/`（`feedback_learning_report`） | `system:operate` | `artifacts/acceptance/verify-agent-learn-and-doc-delete.py`（16/16）、`tests/test_webui_agent_cards.py` |
| 「重扫未处理反馈」 | ✅ | `reparse_failed`：对**净负分**资料逐份 `retry_file`（复用同一只读原件重建分段/向量）；`include_failed=true` 时额外重试解析失败资料 | 同上 | `system:operate` | `tests/test_reparse_failed.py`、`tests/test_patrol_and_integrity.py` |
| 检索反馈（批注）表 | ✅ | 接 `GET /knowledge/search-feedback/`：只列**自己**的反馈，带资料名、判定中文名与**净分**（净分与「重扫未处理反馈」同一实现，且只对当前可读的资料给出）；**5 列齐全**，第 5 列就是原型那列「分诊」：`POST /knowledge/search-feedback/<pk>/handle/` 写 `handled_at/handled_by` + 一条 `search_feedback_handled` 审计事件。**语义刻意收窄：只留痕，不改 verdict、不改加权、不改「净分 / 待处理反馈 / 重扫未处理反馈」口径**；重复处理幂等（保留第一次留痕），别人的反馈 404（系统管理员可处理全部） | `GET /knowledge/search-feedback/`、`POST /knowledge/search-feedback/<pk>/handle/` | 本人 / 系统管理员 | `tests/test_search_feedback_triage.py`（7 项）、`tests/test_search_feedback_list.py`、`artifacts/acceptance/verify-feedback-triage.py`（14/14） |

## 5. 系统配置

| 控件 / 功能 | 状态 | 实现 | API | 权限 | 证据 |
|---|---|---|---|---|---|
| 远程模型服务表（**恰好 4 行**：当前生效向量 + 重排 + OCR + LLM） | ✅ | `loadSettings` | `GET /knowledge/models/`、`GET /planner/guide/`（`llm`/`database_engine`） | 管理员（模型配置）；只读 | 实现 |
| 状态列 = 已配置/未配置 + **「未检测」** | ✅ | 不探测就不给绿灯 | — | — | 实现 |
| 数据库行显示**真实引擎** | ✅ | `guide.environment.database_engine` | 同上 | — | 实现（实测 `postgresql`） |
| 「测试连通」**按需探测 + 显示延迟** | ❌ **待做** | 当前只弹提示、不发请求。`GET /knowledge/models/?probe=1` 已存在（管理员）但**不返回延迟**，前端也未接 | ⚠️ 部分 | 管理员 | 见 §9 |
| 「全部切换为远程」+ 本地模型三按钮 | ⚠️ **范围已澄清，待实现** | `disabled` + title | 应走受控提案 | ❌ 见 T11 |

> **模型控制语义（用户 2026-10-15 明确澄清，不再是"待澄清"）**
>
> 「本地模型」= **AS711 上的模型**，不是"在 Windows 本机部署模型"。
>
> 因此启动/切换能力**必须**满足（实现前的硬约束）：
> 1. **只允许操作本系统登记的目标模型**（已登记的 AS711 目标 profile）；
> 2. 保留**确认 / 审计 / 权限**三件套（谁在何时启动了哪个目标，全程留痕）；
> 3. **禁止**全局 `docker restart`、`killall`、任意 shell 参数拼接 ——
>    不接受任何用户可控的自由命令；
> 4. **其他服务一律不得动**（不得启停/修改 AS711 上未被本系统登记的服务）；
> 5. 目标**已在运行时必须诚实返回 `already_running`**，并给出**真实探测**结果；
>    **不得**把 health 提示伪装成"启动成功"；
> 6. **没有目标管理配置时**必须明确报"配置缺失"，
>    **不得无条件返回成功**（fail-closed）；
> 7. 区分 **AS711 目标 profile** 与 **远程接口路由**：
>    未配置的路线**不可**用同一路径换个标签伪装；
> 8. **维度必须实测**：新 profile 必须**先建新代次 → 重嵌入 → 验证 → 原子激活 → 可回退**，
>    绝不允许把 BGE-1024 冒充 2048。
| 基础设施 **6 行**（数据库/向量索引/存储/任务队列/向量服务/GPU 集群） | ✅ | `infraRow` | `guide.environment` | 管理员 | 实现（GPU 集群如实"未配置"） |

## 6. 信息库

| 控件 / 功能 | 状态 | 实现 | API | 权限 | 证据 |
|---|---|---|---|---|---|
| 4 统计（信息库 / 会话资料 / 已处理 / 待确认） | ✅ | `loadKnowledge` | `/knowledge/stats/`、`/planner/operations/` | 可读库 | 「待确认」= 尚未确认的添加/上传类操作（**非** pending 解析） |
| 每库卡片：tag 用真实 `file_count`；前 6 份**按库单独抓** | ✅ | `perLibrary`（`Promise.all`） | `GET /knowledge/files/?library=&page_size=6` | 可读库 | 实现（避免全局第一页漏库误报 0） |
| 「去文件管理看全部」 | ✅ | `data-lib-link` | 页面跳转 | — | 页面 |

## 7. 添加记录

| 控件 / 功能 | 状态 | 实现 | API | 权限 | 证据 |
|---|---|---|---|---|---|
| 服务端 `source=session` 精确过滤 | ✅ | `loadTasks` | `GET /knowledge/files/` | 可读库 | 单测 |
| 7 列表头（**空态也保留**）+ 数量在分页 footer + 完整分页 | ✅ | `loadTasks(page)` | 同上（`page_size=10`） | 可读库 | 实现 + 单测（分页可达） |
| 性质列 = `classification` | ✅ | 同上 | 同上 | — | 实现 |
| 预览按钮真正打开分段弹窗 | ✅ | `openTaskPreview` | `GET …/chunks/` | 可读库 | 页面 |

## 8. 使用说明

| 控件 / 功能 | 状态 | 实现 | API | 证据 |
|---|---|---|---|---|
| 独立成页（原型 `viewDocs` 结构：`card.paper` + `h3`） | ✅ | `kb-pages.js loadDocs` | `GET /planner/guide/` | 页面 |
| `**加粗**` 安全渲染（先转义再替换） | ✅ | `renderInline` | — | 实现 |
| 步骤文案与**实际界面**一致（第 3 步讲模式/部门性质金额；第 2 步"重新解析"） | ✅ | `apps/planner/views.py` guide steps | 同上 | 实现 |

---

## 9. 待做清单（❌ / ⚠️ 汇总，**不得**以"原型占位"结项）

| 编号 | 事项 | 现状 | 需要做什么 |
|---|---|---|---|
| T1 | 检索反馈「准确/不准确」持久化 | ✅ **已完成** | `SearchFeedback` 模型 + 迁移 + `POST/GET /knowledge/search-feedback/` + 前端按钮已就位；`GET` 只列自己的反馈并带资料名/判定中文名/净分（仅当前可读的资料给净分）；不修改源资料 |
| T2 | Agent 反馈处理 / 重扫 | ✅ **已完成** | `reparse_failed` 现在对**净负分**资料逐份 `retry_file`（与 `list_feedback_targets` 同一口径），并按实际结果分类报账；`include_failed=true` 时额外重试解析失败资料 |
| T3 | 基于反馈的学习规则 | ✅ **已完成（口径：有界加权，不做训练）** | 检索侧加权早就实装：`apps/knowledge/feedback.py` 把净分归一化后夹到 **±`MAX_ADJUST`=0.15**，由 `apps/knowledge/views.py` 在**重排之后、返回之前**统一 `apply_feedback_weight()`，报告里写明 `feedback_weighting`；界面入口是本轮补的：Agent 控制台「查看学习规则」= 受控**只读**意图 `feedback_learning_report`（`apps/planner/operations.py` + `services.py` 的 `_dispatch` 分支），返回当前生效的文件级权重与「本系统不做训练」的口径。**没有** `FeedbackRule` 表：规则是按反馈实时聚合的，因此天然可审计（反馈行本身留痕）、可撤销（删/改反馈判定即刻生效）、不改源文档、不更新模型 |
| T4 | 真实运行指标：停滞任务 / 重启次数 / 熔断失败数 / 待处理反馈 | ✅ **已完成** | 停滞任务=租约过期/为空的 `RUNNING` 实时数；重启次数=`/planner/operations/` 的 `summary.restart_stalled_executed`（已执行条数）；熔断失败数=真实快照；待处理反馈=净负分份数 |
| T5 | 6 张状态卡（看门狗 / 资源调控 / 熔断 / 双阶段补扫 / 结果校验） | ✅ **已完成** | 看门狗/结果校验=最近一次巡检留痕（`PatrolRun`，没跑过就灰点）；双阶段补扫=实时候选查询；其余=真实快照/队列深度 |
| T6 | 系统配置「测试连通」按需探测 + 延迟 | ✅ **已完成** | `?probe=1` 按**每个目标**返回 `latency_ms` 与结果，前端按钮各自调用；仅按需、无后台轮询、不启停服务 |
| T7 | 真实巡检（「立即巡检」） | ✅ **已完成** | `run_patrol` 复用 `knowledge.patrol`（四项检查：重试用尽/停滞/向量不完整/原件缺失），结果留痕；`manage.py kbpatrol` 是同一实现。**停滞判据已修**：`RUNNING` 用 `parse_lease_started_at`（此前误读不存在的 `parse_started_at` → 永远回落到 `created_at`，把刚重跑的旧资料误报停滞），`PENDING` 用 `created_at`；阈值统一取自 `apps/knowledge/integrity.py:STALE_AFTER_MINUTES` |
| T8 | 「重置数据库」的整库重置 | ✅ **已完成（受控重建口径）** | 新增受控提案意图 `reset_library_index`（`apps/planner/operations.py`）：三次独立确认 + 末次**准确输入库名** + 一次性凭证 + `ARMED` 执行窗口；语义是**重建**（逐份 `retry_file(force=True)`），不是清库：不删业务记录、不擦原件，只有全部成功才清理旧代次。专项真模型用例 17/17（`tests/test_reset_library_index.py`），含真实流水线写 Qdrant 后重建的对照；**真实 UI 浏览器验收 15/15**（`artifacts/acceptance/reset-library-index-ui.json`）。**边界**：生产库当前 0 份资料，所以 UI 这轮重建的是空库，带资料的重建由上述真实流水线用例证明 |
| T9 | 存储修复：Qdrant 数据迁到真实 ext4 | 仍 bind 在 Windows 目录（已确证） | 见 `docs/storage-migration-plan.md` 与 `scripts/phase2_storage.ps1` |
| T10 | Windows Celery 真正并发 worker | ✅ **已完成（改为 Linux 容器 worker）** | 交付环境由 `deploy/docker-compose.unified.yml` 的 `worker` 服务承担：`celery -A knowledgebase worker -Q kb_worker_v2 --concurrency=2 --loglevel=INFO`（专用队列，不碰共享 broker 的其它队列），`scheduler` 服务跑 `celery -A knowledgebase beat`；可靠性开关 `task_acks_late=True` / `task_reject_on_worker_lost=True` 已接，崩溃恢复边界见 `docs/deployment-and-operations.md` 与 `artifacts/acceptance/worker-crash-recovery.json` |
| T11 | **AS711 目标模型启动/切换**（"本地模型"三按钮） | `disabled`；语义已澄清但**未实现** | 实现受控提案：仅限**已登记目标 profile**；确认+审计+权限；`already_running` 诚实返回并真实探测；配置缺失 fail-closed；**禁止**全局 docker restart/killall/任意 shell 参数；不动其他服务 |
| T12 | 新 profile 的维度实测与代次切换 | 未实现 | 新 profile 必须**新代次重嵌入 → 验证维度 → 原子激活 → 可回退**；禁止把 BGE-1024 当 2048 |
| T13 | 读取并逐条订正 `docs/independent-open-issues-2026-10-15.md` | ⚠️ **已读并逐条处理中** | 已修 #4（`reparse_failed` 名实不符）与 #6（`restart_stalled_task` 假报成功）；其余条目按该文档逐条对齐，不以占位或无条件拒绝计完成 |

---

## 10. 证据索引

| 证据 | 文件 |
|---|---|
| 真实 RAG 引用事实 | `artifacts/phase1-run3.log` |
| 多轮「只读核对 → 删除提案」12/12 | `artifacts/phase1-multiturn-delete.json` |
| 指定来源撰写（`sources=[29]`） | `artifacts/phase1-close-v2.json` |
| 独立全量回归（SQLite / PG+真实Qdrant+broker） | `ui-sqlite.xml` / `ui-postgres.xml`（审查方） |
| 界面复验 | `docs/ui-acceptance-2026-10-14.md`（审查方） |
| 跨库隔离 / 多轮历史安全 / 空答复 / eligible 作用域 / 分页金额 单测 | `tests/test_search_isolation_and_history.py`、`test_degraded_propagation.py`、`test_llm_empty_reply.py`、`test_llm_failure_api.py`、`test_search_eligible_scope.py`、`test_files_pagination_and_amount.py` |

---

## 11. 非模型缺口修复记录（2026-10-15 · 用户要求"除模型以外"的缺口全部处理）

两份只读审计（前端交互 / 后端 API）给出的清单已按下表逐条处置；每行都指向**可复跑的用例或证据文件**，
没有一项以"原型占位/有意禁用"结项。

| # | 缺口（审计编号） | 处置 | 证据 |
|---|---|---|---|
| 1 | 拒绝原因不持久化：模型不给正文 + 提案全被拒 → 刷新后只剩空气泡（条目 57） | assistant 消息现在在提案之后创建，payload 增 `rejected_proposals`（intent + reason）；`reply_empty` 死字段已删（`llm.py` 对空答复直接 `raise LLMError`，该状态到不了建消息处） | `tests/test_p3_p5_regressions.py::test_rejected_proposal_reason_is_persisted_for_refresh` |
| 2 | 模型不可用时前端没有任何留痕（503 只回响应） | `views.py` 的 `except LLMError` 与 `ServiceUnavailable` 一致：落一条 `Message.Role.SYSTEM`「助手不可用：原因」；前端把 SYSTEM 消息渲染为 `notice-bar`（**不再**伪装成带 AI 头像的回答） | `tests/test_llm_failure_api.py::test_empty_reply_persists_system_note_so_refresh_explains` |
| 3 | 「触发一次学习」按钮点了没反应（T3 的 UI 入口） | 新增**只读**受控意图 `feedback_learning_report` + 按钮改名「查看学习规则」；返回真实文件级权重与"不做训练"口径 | `tests/test_webui_agent_cards.py::test_learn_action_is_a_real_readonly_report` |
| 4 | 巡检停滞判据取错字段（`parse_started_at` 不存在） | `knowledgebase/celery.py` 按状态取值：`RUNNING`→`parse_lease_started_at`、`PENDING`→`created_at`；阈值单一来源 | `tests/test_patrol.py`（把旧写法还原后两条用例都会 FAIL） |
| 5 | `executing` 是单行道死胡同（进程被杀后待办永久"执行中"） | `OperationRecord.executing_is_stale()`（30 分钟）+ `abandon` 放行僵死操作 + 周期任务 `converge_stale_executing_operations()` CAS 收敛为 FAILED 并记审计；**只改状态，不重放动作** | `tests/test_operation_abandon.py`（23 passed，含"活的不能放弃/僵死可放弃/收敛只打僵死者"） |
| 6 | `archived` 是只读死状态（界面渲染、后端无写入方） | 新增受控意图 `archive_file`（`library:admin`）：移出检索 = 真删该文件的向量点 + 状态转 `ARCHIVED`，**保留记录、分段与原件**；再次归档幂等；向量删点失败如实报 partial | `tests/test_archive_file.py`（12 passed，含真实 Qdrant 点被删除的用例） |
| 7 | 派生文档无法删除（D5） | `delete_derived_document()` + `DerivedDocumentView.delete` + 助手页删除按钮；来源资料与原件不受影响 | `tests/test_derived_document_delete.py` |
| 8 | 代次切换/回退、索引状态、审计事件在页面上零入口（B2/E3） | Agent 控制台新增「索引代次」卡（含逐行「设为生效」与「回退到上一代」）与「审计链」卡；切换一律走**受控操作**，不直连 PUT | `tests/test_agent_console_reachability.py` |
| 9 | 陈旧向量登记行只能靠整库重置顺带清理（F1） | 新命令 `manage.py kbvacuum`：只删"登记在、Qdrant 里不存在"的行，默认 dry-run，Qdrant 不可达则 fail-closed 一个字不改；**存在但未登记的集合只报告绝不删** | `tests/test_maintenance_commands.py` |
| 10 | 一次性凭证（ActionTicket/DeleteGrant）无清理入口 | 新命令 `manage.py kbcleanup`（默认 dry-run，`--retention-days`）：明确不动审计事件、操作记录、删除账本 | 同上 |
| 11 | 原件丢失只能看见不能修（F2） | 重传同一份文件即修复：内容寻址把丢失字节写回同一只读路径（`stored.reused=false` → `processing.blob_restored=true`）；若该资料此前**处理失败**，本次重传会重新排队处理；PENDING 不重复排队（`sweep_pending` 管） | `tests/test_upload_api.py::test_reshipping_a_failed_file_restores_a_lost_blob_and_requeues` |
| 12 | `requires_confirmation=False` 与 `True` 等价（A2） | `submit_search_feedback` 改为 `requires_confirmation=True`（要写库）；`run_patrol` **保持** False（只读）；`propose_operation` 的确认数表达式简化，并新增登记表不变量测试 | `tests/test_operation_registry.py` |
| 13 | `DeletionJob.all_steps_ok()` 不含 blob 步骤（D3） | `required = {"database","vectors"}`，**有 blob 步骤时才算它**（`_cleanup_blob` 按设计始终 ok，因此不影响既有删除的 COMPLETED） | `tests/test_operation_registry.py` / 删除相关用例 |
| 14 | beat/scheduler 自己死了没人发现（A3） | `patrol_snapshot()` 增 `overdue` 判决（阈值 `WATCHDOG_STALE_MINUTES=45`，beat 周期 900s 的连续两次 + 余量）；**没有心跳时是"无法判断"（None），不是"正常"**；Agent 控制台在过期时显示危险提示 | `tests/test_patrol_and_integrity.py`（过期/正常/未知三条） |
| 15 | 窄屏（≤960px）完全没有导航（前端审计，阻塞项） | `base.html` 顶栏加 ☰ + 遮罩 + 当前用户与「退出登录」；`KB.initSidebar()` 真实实现（点击/Escape/遮罩/宽度复位），样式加在 `kb-preview.css`（**不动**原型 `kb-proto.css`） | `artifacts/acceptance/verify-responsive-nav.py` |
| 16 | 搜索页文案承诺"归属客户"筛选但没有控件 | `search.html` 增 `#customer` 下拉（facets 已返回 customers），重置按钮一并清空 | `artifacts/acceptance/verify-ui-honesty-batch.py`（选中后**真的**随检索请求发给后端） |
| 17 | 上传无进度、按钮不禁用、失败静默丢弃原因 | 逐份上传：禁用按钮/输入 + 上传互斥（`<label>` 目录选择拦不住 `disabled`）、每份 toast 进度、失败原因**留在页面上**（`#files-upload-note`），重复内容单独计数并转达服务端说明 | `artifacts/acceptance/verify-files-upload-and-row-actions.py` |
| 18 | 信息库/会话/添加记录三处"接口失败画成空态" | 三处都区分"加载失败（HTTP xxx，请刷新重试）"与"真的还没有" | `verify-ui-honesty-batch.py`（路由注入 500：信息库页 + 会话栏都如实报错） |
| 19 | 删除信息库成功回执丢了库名（`name` 落到 `window.name`） | 按钮带 `data-lib-name`，回执取 `body.name || data-lib-name || code` | `verify-ui-honesty-batch.py`（每个删除按钮都带非空库名） |
| 20 | 非管理员打开「系统配置」必然 403，却显示"未配置" | 页面顶部如实提示"403 / 需要系统管理员；下面的『未配置』不代表服务真的没配置" | `verify-ui-honesty-batch.py`（路由注入 403） |
| 21 | 文件页「重置数据库」是 `disabled` 死按钮（说明弹窗永远打不开） | 去掉 `disabled`（保留危险色与 title），弹窗只做说明、不发请求 | `verify-files-upload-and-row-actions.py`（弹窗可开 + 全程零写请求） |
| 22 | 使用说明页 guide 请求失败静默（只剩硬编码章节） | 失败时页面顶部如实说明"服务端步骤没读到"，并声明下面只是内置说明 | `verify-ui-honesty-batch.py`（路由注入 403） |
| 23 | Agent 页反馈表第 5 列是**空表头 + 空单元格**（原型里有「标记已处理」`knowledge-base-prototype.html:1561`/`:2117`） | 新增 `SearchFeedback.handled_at/handled_by`（迁移 `0016`）+ `POST /knowledge/search-feedback/<pk>/handle/`；第 5 列补上「标记已处理」→「已处理 + 时间」。**语义刻意收窄：只留痕**（不改 `verdict`、不改加权、不改「净分 / 待处理反馈 / 重扫未处理反馈」口径），重复处理幂等（保留第一次留痕），别人的反馈 404、系统管理员可处理全部 | `tests/test_search_feedback_triage.py`（7 项）、`artifacts/acceptance/verify-feedback-triage.py`（14/14，含"处理结果"列标记前后逐字一致） |
