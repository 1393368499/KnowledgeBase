# 文档侧边栏宽度：570 → 855 → 750 → 定为 700px（2026-10-19）

## 1. 用户要求

同一天三次调整，逐步收敛到最终值：

1. **"还有，文档侧边栏的宽度扩大到现在的1.5倍"** —— 当时的宽度是 2026-10-15 轮定下的 570px
   （原型 380px 的 1.5 倍），所以先改成 **570 × 1.5 = 855px**。
2. **"宽度改成750px"** —— 看完 855px 后收窄到 750px。
3. **"调整700px"** —— 再收窄到 **700px**，这是最终口径。

宽度演进：**380px（原型）→ 570px（×1.5）→ 855px（570×1.5）→ 750px → 700px（最终）**。
前面的值只作为演进注释保留在 CSS 里。

## 2. 改动

| 位置 | 改动 |
|---|---|
| `apps/webui/static/webui/kb-home.css` `.doc-pane` | `flex:0 0 855px` → `750px` → **`flex:0 0 700px`**；保留 `max-width:calc(100vw - var(--sidebar-width, 220px) - 620px)`；注释记录 380 → 570 → 855 → 750 → 700 的演进与兜底理由。 |
| `tests/test_webui_todo_abandon_and_docpane.py` | `test_doc_pane_is_1_5x_current_width`：断言 380/570/855/750 都不再出现，必须是 **700px**；`test_doc_pane_width_keeps_chat_usable` 断言兜底 `max-width` 为 620px。 |
| `artifacts/acceptance/verify-docpane-width-20261019.py` | 期望值 855 → 750 → **700**；1440（兜底 600px）/1280（兜底 440px）两档 + "文档栏右边缘不超出视口"；截图名 `docpane-700-*.png`。 |
| `docs/delivery-status-2026-10-15.md` §0 第 2 行 | 补注 2026-10-19 的加宽与两次收窄。 |

### 为什么还留着 `max-width` 兜底（会话列下限 620px）

700px 在 1920 宽的屏上很宽裕（左栏 220 + 文档栏 700，会话列约 1000px）；但在 1440/1280 这类窄屏上
固定 700px 会把会话列挤成一条、甚至让文档栏右半截跑到视口外。实测 `.chat-col` 的 min-content
约 **609px**（再窄就会内部挤压），所以兜底下限取 620px：

```
flex:0 0 700px;
max-width: calc(100vw - var(--sidebar-width, 220px) - 620px);
```

- 视口 ≥ 1540px（= 220 + 700 + 620）时 → 正好 **700px**（用户屏幕 1848/1920 都属于这档）；
- 更窄时按 620px 会话列下限自动收窄（1440 → 600px，1280 → 440px），文档栏右边缘始终贴齐视口；
- ≤960px 仍走原媒体查询纵向堆叠（`flex:0 0 auto`），与宽度改动无关；
- 收起时仍 `display:none`，"收起时几何与原型一致"的前提不变。

## 3. 验证

- 离线：`tests/test_webui_todo_abandon_and_docpane.py` 通过；全量 `pytest` exit 0。
- 浏览器（Playwright，真实部署镜像 `kb-app:delivery` =
  `sha256:1dfed82cd1a6ea9ade1be6b9a06238df2ddb137eb969854cf1b05d41c0e8e048`，2026-10-19 11:42:35）：
  脚本 `artifacts/acceptance/verify-docpane-width-20261019.py`，11/11 通过，退出码 0：
  - 收起时渲染宽度 = 0px（不占位）；
  - **1920 视口：文档栏 = 700px**，会话列 1000px，右边缘贴齐 1920；
  - **1440 视口：兜底收窄到 600px**，会话列 620px，右边缘贴齐 1440；
  - **1280 视口：兜底收窄到 440px**，右边缘贴齐 1280；
  - 无前端 console 错误。
  - 截图：`artifacts/acceptance/docpane-700-at-1920.png`、`docpane-700-at-1440.png`、
    `docpane-700-at-1280.png`；结果 JSON：`artifacts/acceptance/docpane-width-20261019.json`。

## 4. 回退

改回 `flex:0 0 570px`（或 855 / 750px）并去掉 `max-width` 一行即可；纯前端样式，不涉及迁移与数据。
