# 回答用正文、卡片只用于待确认（2026-10-19）

> 需求原话（用户 m01034）：**"回答不要用卡片，卡片只用在确认操作上，确认完毕后就消失。"**
>
> 触发场景（截图 m01033，10:28/10:29 的同一会话）：先问了一次「谁出差了」，
> 助手**没有正文**，只在消息下方挂了一张「搜索知识库」卡（已完成 / 共 0 条）；
> 用户再问一次才拿到正文。卡片既没帮上忙，还让"回答"看起来像"操作结果"。

---

## 1. 改了什么

### 1.1 检索结果回填给模型，让回答变成正文

文件：`apps/planner/services.py`（`reply_to_message` + 两个新助手函数）

`search_knowledge` 在上一次改动后是"提案即执行"（见
`docs/search-knowledge-auto-execute-2026-10-19.md` 的机制说明，本文只讲回答形态）。
现在执行结果不再当作"卡片的展示内容"，而是**喂回模型**：

- 每轮工具调用后，把模型这一轮的调用还原成 OpenAI 协议消息
  （`_assistant_tool_call_message()` → `assistant` + `tool_calls`）；
- 把**已自动执行**的只读工具结果压成纯文本
  （`_tool_result_content()`：命中条数、检索的库、每条的文件名/页码/正文片段），
  以 `role: "tool"` + `tool_call_id` 回填；
- 模型下一轮据此写正文 —— 用户看到的就是一段正常回答。

三条边界：

1. **写操作不自动执行**：`archive_file`、`delete_file`、`compose_document` 等
   仍只落成待确认卡片；既然没有"已执行结果"可回填，循环立刻停下，不做多余推理。
2. **轮数上限**：一轮提问最多 `KB_LLM_MAX_TOOL_ROUNDS`（默认 3）轮工具调用，
   避免模型反复检索把一次对话拖成死循环。
3. **兜底一定给正文**：轮数用尽时模型仍只调工具、没有正文，则**关掉 tools
   再问一次**（追加一条 user 提示"请直接基于这些结果用正文回答，不要再调用工具"）。
   多花一次调用换来"问题一定有回答"，否则用户只会看到
   "这一轮没有正文回答"。

### 1.2 会话接口只返回"还没结束"的操作

文件：`apps/planner/views.py`（`ConversationView`）

会话详情现在只返回 `proposed / confirmed_1 / confirmed_2 / armed` 四种状态；
`executed / rejected / expired / failed` 不再出现在会话里。
于是「确认 → 执行」「放弃」「过期」之后，刷新或重渲染都不会再冒出那张卡。

**没有删除任何审计**：`planner_operationrecord` 记录原样保留（「我的操作记录」里可查）。
"卡片消失"只影响会话区的展示。

### 1.3 前端：执行/放弃后把卡片从会话里撤掉

文件：`apps/webui/static/webui/kb-assistant.js`

- `executeCard()`：执行成功后 `card.remove()`（旧行为是把卡改成"已完成 + 结果列表"
  并继续留在会话里）；结果的去向照旧 —— 生成的文档直接在右侧文档栏打开。
- `abandonCard()`：会话内的卡直接移除；工具栏待办列表仍走整块重载（服务端不再返回该条）。
- `emptyReplyHtml()`：文案从"待确认的卡片在本条消息下方，确认后才会执行"改为
  "检索类操作已直接执行；写操作会在下方生成待确认卡片"——不再承诺不存在的东西。

### 1.4 配置：轮数预算必须真的从环境变量读

文件：`knowledgebase/settings.py`、`.env.example`

新增 `KB_LLM_MAX_TOOL_ROUNDS`（默认 3）并**在 settings 里真正定义**。
此前代码只有 `getattr(settings, "KB_LLM_MAX_TOOL_ROUNDS", 3)` 兜底，
环境变量设了也不生效 —— 与 `KB_LLM_TEMPERATURE` 是同一类缺陷。

---

## 2. 没有放宽的约束

- 写/删除类操作**仍必须人工确认**，任何情况下都不代用户执行；
- 权限复核、一次性凭证核销、CAS（`ARMED → EXECUTING`）、审计落库全部照旧；
- 「本次检索范围」（用户勾选的信息库）仍随提案落库并在执行层硬收敛，
  回填给模型的检索结果也在该范围内。

---

## 3. 验证

### 3.1 离线

- 新增 `tests/test_reply_tool_loop_and_card_lifecycle.py`（11 条）：
  - 检索结果确实以 `tool` 消息回填、模型第二轮拿到文件名/正文，最终 `content` 是正文；
  - 轮数上限生效（`KB_LLM_MAX_TOOL_ROUNDS=1/2` 参数化），最后一轮 `payload` 里没有 `tools`；
  - 写操作只问模型一次、只落待确认卡（不自动执行、不多轮）；
  - 会话接口隐藏已执行/已放弃的卡、保留待确认卡；
  - 工具结果文本包含"命中条数/文件名/页码/正文"。
- 全量 `pytest` exit code 0（改动后、以及补 `KB_LLM_MAX_TOOL_ROUNDS` 后各跑一次）。
- `ruff check` 只报 `knowledgebase/settings.py:379` 的既有 F401（与本次改动无关）。

### 3.2 镜像与部署

- `powershell -NoProfile -File deploy/build-app.ps1` → `kb-app:delivery` =
  `7dc391187236`（2026-10-19 10:43:19，基座 `kb-worker:phase2-review-20261015`
  本地 ID 校验通过）；
- `docker compose up -d web worker scheduler`：migrate 无待应用迁移，三个容器 healthy；
- 容器内 `KB_LLM_MAX_TOOL_ROUNDS=3`。

### 3.3 现场（真实 AS711 模型 + 真实资料，容器内 APIClient）

| 场景 | 结果 |
|---|---|
| 勾「测试库(contract-test)」问"谁出差了？" | HTTP 201；正文"根据检索到的资料，出差的是**陶鑫宇**（随孙总出差陵水城建集团）"；0 提案、0 卡片 |
| 勾「开发验收资料库(codex-acceptance)」（库里没有出差资料） | 模型调用 `search_knowledge` → 提案 `auto_executed=true`、`count=0`、`library_codes=['codex-acceptance']`；会话内 0 张卡片；模型用**正文**回答"在「开发验收资料库」中未检索到关于'出差'的相关信息…"；`tool_calls` 已写入消息 payload |
| `artifacts/_verify_card_lifecycle.py` | 检索卡不出现；`list_files` 待确认卡出现；放弃后卡片消失；审计记录仍在 → 失败：无 |

两次对话都真实打到 AS711 `/v1/chat/completions`（第二次带 `role: "tool"` 消息），
均 HTTP 200，模型服务未崩溃（本系统仍全程 greedy）。

验证脚本：`artifacts/_verify_chat_auto_execute.py`（`KB_VERIFY_SCOPE` /
`KB_VERIFY_QUESTION` 可覆盖）、`artifacts/_verify_card_lifecycle.py`。
验证产生的临时会话已清理（只留用户自己的会话 53）。

---

## 4. 回退与已知边界

- **完全回退**：仓库没有 git，按函数定位还原四处 ——
  `apps/planner/services.py` 的 `reply_to_message` 内 `dialogue` 循环
  （连同 `_assistant_tool_call_message` / `_tool_result_content`）、
  `apps/planner/views.py` 的 `ConversationView` 状态过滤、
  `kb-assistant.js` 的 `executeCard` / `abandonCard` / `emptyReplyHtml`。
- **只关多轮**：`.env` 设 `KB_LLM_MAX_TOOL_ROUNDS=1` 再 `docker compose up -d`
  （不再多轮，但仍会执行一次检索并逼出正文）。
- **已知边界**：模型如果在最后一轮仍拒写正文，界面会显示
  "这一轮没有正文回答，只发起了操作：…（检索类操作已直接执行）"，
  而不是编造一段回答 —— 宁缺勿假。
