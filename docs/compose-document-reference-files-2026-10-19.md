# 「编写新文档」：先选模板，再在当前库里选参考文件（2026-10-19）

> **⚠️ 契约已于 2026-10-22 变更**：`compose_document` 的「一份都不勾 = 按主题检索全库」
> 已被取消——确认与执行都要求至少一份具体参考文件。本文 §2.2、§3、§5.1 中
> 「空选回落全库」的描述是**当日的历史记录**，不再是现行行为。
> 现行口径见 [compose-document-requires-sources-2026-10-22.md](compose-document-requires-sources-2026-10-22.md)。

## 1. 用户提出的问题

第一条需求（m01352 原话）：

> 不能基于全库，用户选择完模板后还要选择当前所在库中的参考文件

紧接的补充（m01369 原话）：

> 如果不选的话就默认全库

问题现场（m01351 截图）：待确认卡的明细是

```
目标：基于全库编写新文档
检索范围：测试库
```

卡片上只有一个「文档模板」下拉框，**没有任何选参考文件的地方**，而操作名与目标
都写着"基于全库"——用户想写一份只依据测试库里那三份出差资料的文件时，除了
把整库塞给模型，没有别的选择。

## 2. 改成了什么

### 2.1 卡片上新增「参考文件」多选

「编写新文档」的待确认卡现在有两级选择：

1. **文档模板**（原有）：不选就不用模板；
2. **参考文件**（新增）：列出**当前所在库**里已处理完成的资料，可多选。

"当前所在库"就是这一问的「本次检索范围」：

- 勾了库 → 只列这些库里的资料；
- 没勾库 → 列全部可读库的资料（此时本就没有"当前库"）。

### 2.2 勾选立刻落库，可改可清空

勾/取消会立即 `POST /api/v1/planner/operations/<id>/sources/`，把资料 id 写进
这条操作自己的 `params["sources"]`——确认可能隔几分钟才点，甚至跨一轮对话，
参考文件必须**持久化在这条待办上**，不能只存在浏览器里。

语义（与用户两句话逐条对应）：

| 用户选择 | 执行时 |
|---|---|
| 勾了 1..N 份资料 | **只使用这些资料**（精确到份；`source_file_ids` 路径） |
| 一份都不勾 | **按主题检索全库**（保留原行为，`topic` 路径） |

### 2.3 校验口径（不能悄悄放宽）

- 资料必须存在、属于当前用户可读的库、**已处理完成**（`parse_status=done`）；
- 勾了库时，资料还必须在**本次检索范围**内；
- 任何一份不满足就整条报错（400/404），而不是"悄悄少用几份"；
- `armed`（已确认、可执行）之后不允许再改（409）——用户确认过的样子必须
  就是实际执行的样子。

### 2.4 文案去掉"基于全库"

操作名从「基于全库编写新文档」改为「编写新文档（可选参考文件）」，使用说明
第 5 条同步改为"按主题检索、或基于你勾选的参考文件"。

助手"这一轮没有正文回答"的兜底气泡用的是一张前端工具名表
（`kb-assistant.js` 的 `TOOL_NAMES`），那里原来还写着「基于全库编写新文档」，
上线后实测发现气泡和卡片名字不一致，已同步改为「编写新文档（可选参考文件）」。

## 3. 改动清单

| 文件 | 改动 |
|---|---|
| `apps/planner/operations.py` | `compose_document` 标签/摘要改口径；注释写明 `sources` 留空 = 全库 |
| `apps/planner/services.py` | 新增 `set_operation_sources()`、`compose_source_candidates()`、`PENDING_EDITABLE_STATUSES`、`MAX_SOURCE_CANDIDATES`；`_validate_params` 把**可选参数的空串**归一为"没传"（见 §7） |
| `apps/planner/views.py` | 新增 `OperationSourcesView`；`_operation_dict` 增加 `source_file_ids` / `source_candidates`；使用说明文案 |
| `apps/planner/urls.py` | 新增 `planner/operations/<pk>/sources/` |
| `apps/webui/static/webui/kb-assistant.js` | `sourcePickerHtml()`、`setCardSources()`、change 委托；`TOOL_NAMES.compose_document` 改口径 |
| `apps/webui/static/webui/kb-home.css` | `.op-sources` 一组样式 |
| `tests/test_compose_document_sources.py` | 新增 12 条回归（含空串参数与气泡文案两条） |

## 4. 验证

- 新增用例 12 passed；
- 相关回归（模板/范围/免确认/多轮正文/卡片生命周期）110 passed；
- 全量 `pytest`：**exit 0**（1513 条）；
- `ruff check` 改动文件全绿；`node --check kb-assistant.js` 通过；
- 镜像内现场验证见 §5。

## 5. 现场验证（真实容器 + 真实资料 + 真实 AS711 模型）

最终上线镜像 `kb-app:delivery` = `sha256:e96b3113410b`（2026-10-19 11:21:42 构建，
已 `docker compose up -d web worker scheduler`，三容器 healthy；本页结果先在
`sha256:f39f816c4b3a` 上取得，最终镜像上 UI 复验通过）。

服务层脚本 `artifacts/_verify_compose_sources.py` 在容器内执行（`PYTHONPATH=/app`），退出码 **0**。

| 场景 | 结果 |
|---|---|
| 勾「测试库」提案「编写新文档」 | 卡片候选 = 测试库 3 份资料（40/41/42）；范围外资料不出现 |
| 勾 40+41 → 执行 | 卡片回显 `source_file_ids=[40,41]`；执行结果 `sources` 恰为这两份，正文 `# 出差情况说明` |
| 清空参考文件 → 执行 | `params.sources` 已移除，回执「已清空参考文件：将按主题检索全库」；走 topic 全库路径，结果 sources = 40/41/42 三份 |
| 勾选后越界改库 | 服务端拒绝（`_scoped_libraries_or_raise` / 范围外资料报「不在本次检索范围内」；本次测试库里没有范围外资料，该分支由离线用例覆盖） |
| `armed` 后再改 | 409「不能再改参考文件」（离线用例覆盖） |
| 非法 id（不存在 / 非整数） | 明确拒绝：`参考文件不存在：987654321`、`参考文件必须是资料 id：'abc'` |

注：文档 `sources` 的顺序由关联表顺序决定（与勾选顺序无关），验证按集合比较。

### 5.1 界面层复验（Playwright，真实模型）

脚本 `artifacts/acceptance/verify-compose-source-picker-ui-20261019.py`，9/9 通过，退出码 0：

- 首页发「帮我写一份出差情况说明」→ 真实模型发起 `compose_document` → **出现待确认卡**；
- 卡上有「参考文件（可多选）」+ 3 份候选（测试库资料，带库名）+ 提示
  「不选参考文件就按主题检索全库」；
- 勾选一份立刻落库，卡上回执「已选参考文件：海口东-陵水.pdf」；
- 无 console 错误；最后点「放弃」把这张验证卡结掉，验证数据已清理。

截图：`artifacts/acceptance/compose-source-picker-card.png`；
结果 JSON：`artifacts/acceptance/compose-source-picker-ui-20261019.json`。

## 6. 回退

- 只想让卡片不显示选择器：回滚 `kb-assistant.js` + `kb-home.css` 两处前端；
- `sources` 留空本来就走全库，后端不需要回退；
- 镜像回退：`docker compose` 里换回上一个 `kb-app:delivery` tag 即可（迁移无变化，
  本次没有新增字段）。

## 7. 上线后现场发现并修掉的两个缺陷

两个都是在真机 UI 复验时暴露的，不是设计问题，已含在最终镜像里。

### 7.1 模型把"没选模板"写成空串，整张卡被参数校验枪毙

- 现象：用户发「帮我写一份出差情况说明」，模型正确发起 `compose_document`，
  但参数是 `{"title": "出差情况说明", "topic": "…", "sources": [], "template_id": ""}`；
  `_validate_params` 对 `int` 类型的 `template_id` 判定 `""` 不是整数 →
  抛「参数 template_id 必须是整数」→ **提案被拒**，界面上什么卡都没有，
  只剩一句"这一轮没有正文回答"。
- 修法：`_validate_params` 里把**可选参数的空串/纯空白**归一为"没传"
  （与既有的 `null` 处理同一口径）。真正的非法值（`"abc"`）仍然拒绝。
- 回归：`tests/test_compose_document_sources.py::test_blank_optional_params_are_treated_as_absent`。

### 7.2 无正文气泡仍写「基于全库编写新文档」

- 现象：卡上标题已是「编写新文档（可选参考文件）」，但助手空回复的兜底气泡
  （`kb-assistant.js` 的 `TOOL_NAMES`）还写着「基于全库编写新文档」，口径不一致。
- 修法：`TOOL_NAMES.compose_document` 改为「编写新文档（可选参考文件）」。
- 回归：`tests/test_compose_document_sources.py::test_empty_reply_wording_no_longer_says_based_on_whole_library`。

