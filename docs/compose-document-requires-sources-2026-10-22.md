# 契约变更：`compose_document` 不再允许「空选回落全库」（2026-10-22）

## 1. 为什么改

2026-10-19 的实现口径是：待确认卡上一份参考文件都不勾 → 按主题检索**全部可读库**
（沿用旧行为）。2026-10-20 的修改把它收紧成**必须至少选一份具体参考文件**，代码
（`apps/planner/services.py` 的 `_require_compose_sources`，及 `:4319-4320` 执行路径）
和前端确认卡（`kb-assistant.js` 的 `sourcePickerHtml` / `TOOL_NAMES`）都已生效，但
当日文档与回归用例没有同步：

- `docs/compose-document-reference-files-2026-10-19.md` §2.2/§3/§5.1 仍写「空选=全库」；
- `tests/test_planner_library_scope.py` 两条用例仍断言空选会回落；
- 结果：测试基线红了 5 条，交付文档描述的是已被代码拒绝的行为。

## 2. 现行口径（本次拍板）

| 场景 | 行为 |
|---|---|
| 待确认卡上勾了 ≥1 份参考文件 | 只依据所选资料撰写；勾选立即落库，可改可清空 |
| 一份都不勾就点确认 / 执行 | **拒绝**：`请至少选择一份参考文件，再继续确认或执行`（HTTP 400，不生成文档） |
| 模型提案时没带 `sources` | 允许（提案阶段不阻断），用户在确认卡上补选 |
| 选了范围外信息库的资料 | 拒绝：`资料《…》不在本次检索范围内`（范围校验优先于撰写） |

**为什么选「强制选源」而不是恢复「空选全库」**：
1. 生成类操作会写出正式文档并落库，依据面越大，越容易把无关/越权资料写进正文；
   强制明确来源是"最小依据面"原则，误用代价更低。
2. 卡片已有候选列表与搜索/全选，"至少选一份"只多一步，不会挡住正常使用。
3. 若将来确实需要"按主题全库写作"，那是**另一个入口**（`POST /api/v1/planner/documents/`
   的「生成草稿」仍允许省略 `source_file_ids` = 全库），不应与受控操作混用一个语义。

## 3. 落点（本次同步）

| 文件 | 变更 |
|---|---|
| `apps/planner/services.py:633-638` `_require_compose_sources` | 确认（`:1864`）与执行（`:3909`）前置校验；执行路径 `:4319-4320` 同样拒绝 |
| `apps/webui/static/webui/kb-assistant.js` | 确认卡文案/按钮禁用已按「至少选一份」实现（`:506,523,534,784`） |
| `tests/test_planner_library_scope.py` | 两条旧用例改写：空选→拒绝；范围内选源→库范围锁定；范围外选源→拒绝 |
| `tests/test_planner_library_scope.py` 头部 docstring | 同步第 6 条"兼容底线"的口径 |
| `docs/compose-document-reference-files-2026-10-19.md` | 顶部加变更横幅，指向本文 |

## 4. 回归

```
.venv\Scripts\python.exe -m pytest -q tests/test_planner_library_scope.py
```

涵盖：空选拒绝、范围内选源锁定库范围、范围外资料拒绝、范围外库拒绝。
