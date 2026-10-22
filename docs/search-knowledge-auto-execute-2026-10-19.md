# 「搜索知识库」改为免确认执行（2026-10-19）

## 一句话

检索是只读操作，不该让人点两次确认才看到结果：`search_knowledge` 现在是**提案即执行**，卡片直接显示"已完成 + 检索结果"；写/删类操作照旧必须人工确认。

## 改了什么

| 位置 | 改动 |
| --- | --- |
| `apps/planner/operations.py` | `OperationSpec` 新增 `auto_execute: bool = False`（默认关，逐个意图显式打开）；只有 `search_knowledge` 设为 `True`，label 后注明"只读，提案即执行"。`to_dict()` 同步带上该字段 |
| `apps/planner/services.py` | `propose_operation()` 在返回前判断 `spec.auto_execute`，命中就调用新助手 `_auto_execute_proposal()`：用刚签发的票据走一次 `confirm_operation()`（系统代点，核销 CONFIRM_STEP_1）→ 立刻 `execute_operation()`，提案响应里直接带 `status="executed"` 与 `result` |
| `scripts/dump_intents.py` | 输出增加 `"auto"` 列，便于核对哪些意图免确认 |
| `artifacts/_verify_library_scope.py`、`artifacts/_verify_scope_api.py` | 验证脚本适配：提案已是 `executed` 时直接用其结果，不再调确认/执行接口 |

## 为什么这样实现（而不是绕开状态机）

- 走完 `confirm_operation` → `execute_operation` 原流程，等于把"人点确认"换成"系统代点"，**权限复核、票据核销、CAS、审计记录、`library_scope` 范围收敛全部照旧**——"不用人点"不等于"不记账"。
- 只读是放开前提：`is_mutation(spec) == False` 的断言写在测试里，将来若有人给写操作打开 `auto_execute`，测试会红。
- 只有被用户明确要求的意图才打开（当前仅 `search_knowledge`）；`list_files`、`read_file` 等仍保持原有确认行为，避免一次改动扩散到未讨论的范围。

## 用户会看到什么

- 问"谁出差了？"：不再出现"待确认"卡片，卡片直接是"已执行 · 共 N 条 + 结果列表"，正文模型可以基于结果继续回答。
- 检索后端出错时**不伪造成功**：提案带 `rejected=true` 与错误原因，操作记录落 `FAILED`（卡片状态"失败"），会话里如实提示。
- 对已执行的检索再调 `/confirm/` 或 `/execute/`：如实返回"操作已结束"，不会重复检索。

## 验证（离线）

- 新增 `tests/test_search_knowledge_auto_execute.py`（10 条）：spec 白名单、只读断言、提案即 executed 且 `result` 落库、响应里 `confirm_token` 为空、会话里不留 `proposed` 卡片、confirm/execute 返回"已结束"、写操作（`archive_file`）仍停在 `proposed`、聊天里 tool_call 走的也是免确认路径、检索失败如实报错。
- 既有回归 `tests/test_planner_library_scope.py`（17 条）同步调整：检索类改为断言"提案即 executed"；越界库在提案阶段就报错并落 `FAILED`。
- 全量 pytest 退出码 0；ruff 对改动文件无新增告警（`scripts/dump_intents.py` 的 E402 是既有写法，`django.setup()` 之后 import 属脚本约定）。

## 现场验证（2026-10-19 10:26，镜像 `kb-app:delivery` = `5375dd5c0f0b`）

构建 `deploy/build-app.ps1`（基座校验通过）→ `docker compose up -d web worker scheduler`，migrate 退出码 0、三个容器 healthy、`/api/v1/health/ready/` 200。

| 验证 | 结果 |
| --- | --- |
| 服务层 `propose_operation(intent=search_knowledge)` | `status=executed`、`auto_executed=True`、`confirm_token=""`、落库 `executed`/confirmations=1/有 result；不勾范围时检索了 4 个可读库、count=3 |
| 重复 `confirm_operation` / `execute_operation` | 分别返回"操作已结束（executed），不能继续确认"、"操作状态是 executed，必须先完成确认才能执行" |
| 写意图 `archive_file`（有可写资料） | 仍为 `proposed`，没有被误执行 |
| HTTP `POST /api/v1/planner/operations/propose/` | 201，响应 `status=executed`、`auto_executed=True` |
| **真实模型端到端** `POST /api/v1/planner/chat/`（勾「开发验收资料库」问"谁出差了？"） | 201；模型发起 `search_knowledge` 工具调用 → 提案 `status=executed`、`auto_executed=True`、`rejected=null`；会话详情卡片为 `executed`/confirmations=1（用户侧不再出现"待确认"） |
| 对照：勾「测试库」问同一句 | 模型直接用背景上下文答出"陶鑫宇"，不产生提案（原有行为不变） |

验证脚本：`artifacts/_verify_auto_execute.py`（服务层 + HTTP 提案）、`artifacts/_verify_chat_auto_execute.py`（真实模型聊天链路）。

## 回退

`git` 无关地回退：把 `apps/planner/operations.py` 里 `search_knowledge` 的 `auto_execute=True` 去掉即可恢复"必须人工确认"；已执行的检索记录不受影响。

