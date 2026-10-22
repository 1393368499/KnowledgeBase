# 「本次检索」勾选的库不生效 —— 缺陷说明与修复（2026-10-19）

> 已构建并部署：`kb-app:delivery` = `9fbe2041a958`（2026-10-19 10:14），
> `migrate` 已应用 `planner.0008_operationrecord_library_scope`，web/worker/scheduler 均 healthy。

用户反馈（原话）：

> 出差的资料我放在测试库中，为什么我选中其他库后，还是能给我搜出来？

**结论：这不是向量库过滤失效，而是"用户勾选的检索范围"只作用在背景上下文检索，
模型发起的工具调用（`search_knowledge` 等）完全绕过了它。** 已修复：范围现在
从"用户勾选"一路绑定到"执行层"，并在确认卡上回显。

---

## 1. 现场证据

会话「谁出差了?」（DB：`planner_conversation.id=29`，user_id=17）：

- 用户消息（`planner_message.id=69`）payload：`{"library_codes": ["codex-acceptance"]}`
  —— 即输入栏勾的是**开发验收资料库**。
- 助手消息（`planner_message.id=70`）payload：
  - `requested_codes = ["codex-acceptance"]`
  - `searched_libraries = [{id: 9, code: "codex-acceptance", name: "开发验收资料库"}]`
    —— **背景检索确实被限定住了**；
  - `tool_calls = [{name: "search_knowledge", arguments: {"query": "出差"}}]`
    —— **工具调用里没有 library 参数**。
- 执行卡片结果 3 条：`陶鑫宇提交的出差申请202610132300000331266.pdf`、
  `海口东-陵水.pdf`、`陵水-海口东.pdf`，全部 `library_id=16`（`contract-test` 测试库）。

库表实况（`kb_knowledge_library`）：9 `codex-acceptance`（开发验收资料库，
`kb-9-g3`）、16 `contract-test`（测试库，`kb-16-g1`）；上列 3 份资料
（`kb_ingested_file` id 40/41/42）确实只在库 16，`parse_status=done`、
`vector_status=indexed`。

**所以数据没问题、权限也没问题：是"勾了库"这一信息在工具调用执行时丢了。**

## 2. 根因（两条独立的遗漏）

1. `services.reply_to_message()` 只把 `library_codes` 交给
   `build_context_report()`（背景上下文检索），而把 `tool_calls` 落成提案时
   **没有把这个范围传给 `propose_operation()`**，`OperationRecord` 上也没有字段
   可存 → 执行时无从知道用户勾了什么。
2. 执行层（`_dispatch`）里 `search_knowledge` 只认模型自己给的 `params["library"]`；
   模型不给（常态）就退回"**全部可读活跃库**"。于是测试库的资料被抓了出来。

崩溃机理之外还发现一个"看起来修了其实没修"的坑：`OperationRecord` 是跨回合的，
用户在卡片上点「执行」可能晚几分钟甚至刷新页面之后 —— 所以范围**必须落库**，
不能只在一次请求的内存里传递。

## 3. 修复

### 3.1 数据与接口

| 位置 | 改动 |
| --- | --- |
| `apps/planner/models.py` | `OperationRecord.library_scope = JSONField(default=list, blank=True)`：提案时冻结"这一问勾了哪些库"，执行时只认它。 |
| `apps/planner/migrations/0008_operationrecord_library_scope.py` | 新增字段（`makemigrations --check` 无差异）。 |
| `apps/planner/services.py` | 新增范围工具：`_library_scope_codes()`（去空白/去重/保序）、`_scope_libraries()`、`_scope_names()`、`_scope_unusable_error()`、`_scoped_libraries_or_raise()`。 |
| `services.propose_operation(..., library_scope=...)` | 落库并回显；`reply_to_message()` 调用时传入本次 `library_codes`。 |
| `services.reply_to_message()` | system 提示新增 `[本次检索范围]` 段：把库名+code 告诉模型，明确"只在这些库内检索/列资料/查看/写文档；不够就说明，不要换个库再查"。 |

### 3.2 执行层：范围硬生效（不再是"建议"）

- `search_knowledge`：范围非空时检索集合 = **范围内可用库**；模型显式点名的
  `library` 若在范围外 → `PlannerError("信息库 X 不在本次检索范围内（本次范围：…）")`；
  结果里新增 `library_codes`（实际检索了哪些库，卡片可核对）。
- `list_files`：结果集只保留范围内的库；显式 `library` 越界同样拒绝。
- `read_file`：资料不在范围内 → 拒绝，并说明是哪份资料。
- `compose_document`：范围非空时，默认依据 = 范围内的库（不再"全库"）；
  `sources` 里出现范围外的库/资料 → 拒绝。
- 范围内的库**部分**不可用（被删/停用/权限被收回）时：只搜仍可用的部分；
  **全部**不可用 → 拒绝执行并说明"本次检索范围的信息库已不可用…请把「本次检索」改为全部库"，
  **绝不静默回落到全库**（静默回落正是原 bug 的形态）。
- 范围为空（没勾库）时行为与以前完全一致。

### 3.3 界面

- `GET /api/v1/planner/operations/` 的每条操作新增 `library_scope`（code）
  与 `library_scope_labels`（库名，库被删则回落 code）。
- `apps/webui/static/webui/kb-assistant.js` 的确认卡在「目标」下显示
  `检索范围：开发验收资料库` —— 执行前后都能看见"为什么只/也搜了这些库"。

## 4. 验证

新增回归 `tests/test_planner_library_scope.py`（16 条），覆盖：

- 勾选范围落库并随提案返回；未勾库时仍不限制（兼容底线）；
- 范围内检索只调用范围内库（`knowledge_search` 参数被断言）；
- 显式越界库/资料被拒绝且**拒绝发生在检索之前**；
- 停用库 → 拒绝（不回落全库）；部分失效 → 只用可用部分；
- `list_files` / `read_file` / `compose_document` 同样收敛；
- 接口回显 `library_scope` + 库名；system 提示里出现 `[本次检索范围]`。

结果：新用例 16 passed；全量 `pytest` 退出码 0。

## 5. 现场复验（部署后，2026-10-19 10:09）

用真实链路（真实 AS711 模型 + 真实解析/向量库 + 真实工具执行）跑了三组，脚本见
`artifacts/_verify_library_scope.py`（容器内 `docker compose exec -T -e PYTHONPATH=/app web python /tmp/_verify_library_scope.py`）：

| 用例 | 勾选 | 助手检索回执 `searched_libraries` | 工具执行结果 |
| --- | --- | --- | --- |
| A | 开发验收资料库 `codex-acceptance` | 只有库 9 | 模型提案 `search_knowledge(query=出差, library=codex-acceptance)`，`count=0`、`library_codes=['codex-acceptance']`，**没有**测试库资料 |
| B | 测试库 `contract-test` | 只有库 16 | 直接据上下文答"出差的是陶鑫宇"，无越界 |
| C | 不勾（全部库） | 库 7/8/9/16 | 与旧行为一致 |

结论：**A 没有越界**（`library_codes` 不含测试库、结果里无测试库资料），
B/C 说明范围收窄与"不限定"两端都正常。三组用例创建的验证会话已从库里清理。

另外用 DRF 视图跑了浏览器真正走的那条 HTTP 链路（`artifacts/_verify_scope_api.py`，
`POST /api/v1/planner/chat/` → 确认卡接口 → 确认 → 执行）：

- `POST /chat/` 返回 201，提案 `library_scope=['codex-acceptance']`；
- `GET /conversations/<id>/` 的卡片回执：`library_scope=['codex-acceptance']`、
  `library_scope_labels=['开发验收资料库']`；
- 确认/执行均 200，执行结果 `{"count": 0, "library_codes": ["codex-acceptance"]}`，无越界。

## 6. 回退与边界

- 本次不涉及数据迁移回退风险（只加一个带默认值的 JSON 字段）。
- 想临时回到旧行为（不推荐）：把范围留空即"不限定"；没有开关可以"忽略已勾选范围"，
  这是刻意的 —— 用户勾了就该生效。
- 边界：范围只约束**工具调用与撰写**；模型在正文里凭已有上下文作答不受影响。
