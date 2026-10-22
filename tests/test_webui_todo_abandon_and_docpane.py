"""首页两处界面改动的源码级回归（用户要求）：

  1. 工具栏「待办事项」里的**删除（放弃）按钮**；
  2. 文档侧边栏宽度放大到原来的 **1.5 倍**。

为什么要源码级断言（而不是只靠浏览器手测）：
  * 「放弃」的语义容易在后续重构里退化成"真的删掉审计记录"——这是本文件盯住的红线；
  * 侧边栏宽度是原型对齐的一部分（收起时几何必须与原型一致），数值被随手改掉不会报错。
"""

from __future__ import annotations

from pathlib import Path

WEBUI = Path("apps/webui/static/webui")
TEMPLATES = Path("apps/webui/templates/webui")


def _read(name: str) -> str:
    return (WEBUI / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. 待办的删除（放弃）按钮
# ---------------------------------------------------------------------------


def test_pending_card_has_abandon_button():
    """待办卡上有 `data-op-abandon` 入口，且给出"不会删资料"的 tooltip。"""
    assistant = _read("kb-assistant.js")
    assert "data-op-abandon=" in assistant, "待办卡没有删除（放弃）按钮"
    assert "data-op-abandon]" in assistant, "按钮没有绑定处理器"
    assert "不会删除任何资料" in assistant, "按钮/确认框没有讲清不会删资料"


def test_only_unfinished_states_get_the_button():
    """终态（已执行/已放弃/已过期/失败）不该出现"放弃"按钮。"""
    assistant = _read("kb-assistant.js")
    assert '["proposed", "confirmed_1", "confirmed_2", "armed"].indexOf(op.status)' in assistant, (
        "放弃按钮没有按状态收敛，终态卡会出现删除入口"
    )
    # 执行中的状态也不在其中（后端另有拒绝，前端也不该给入口）
    assert '"executing"' not in assistant.split("abandonable")[1].split(";")[0]


def test_abandon_goes_through_confirm_dialog():
    """放弃前必须二次确认，且确认文案说明"保留历史"。"""
    assistant = _read("kb-assistant.js")
    start = assistant.index("async function abandonCard(")
    body = assistant[start : start + 2600]
    assert "KB.confirmDialog(" in body, "放弃没有走确认框"
    assert "保留" in body, "确认文案没有说明会保留操作记录（容易被当成抹掉历史）"
    assert "abandon" in body, "没有调用放弃接口"


def test_abandon_is_not_a_raw_delete():
    """放弃**不是** REST 删除：不允许出现 DELETE 旁路。"""
    assistant = _read("kb-assistant.js")
    start = assistant.index("async function abandonCard(")
    body = assistant[start : start + 2600]
    for forbidden in ('method: "DELETE"', "method: 'DELETE'", 'method: "delete"'):
        assert forbidden not in body, f"放弃走成了 REST 删除：{forbidden}"
    assert "/abandon/" in body, "没有调用后端的 abandon 端点"


def test_abandoned_card_is_visually_distinct():
    """已放弃的卡在 `kb-preview.css` 里有独立样式（`kb-proto.css` 只读，不能改）。"""
    preview = _read("kb-preview.css")
    assert ".op-card.state-rejected" in preview, "已放弃的卡没有独立视觉，用户看不出它不再等待操作"
    proto = _read("kb-proto.css")
    assert ".op-card.state-rejected" not in proto, "改到了只读的原型样式表 kb-proto.css"


def test_pending_toolbar_refreshes_after_abandon():
    """在待办**列表**里放弃后要重新拉列表（条目才会消失）。"""
    assistant = _read("kb-assistant.js")
    start = assistant.index("async function abandonCard(")
    body = assistant[start : start + 2600]
    assert "loadPendingOps(" in body, "放弃后没有刷新待办列表"
    assert "loadPendingOps(pendingBox);" in body, "刷新待办列表时没有用工具栏那个容器"


# ---------------------------------------------------------------------------
# 2. 文档侧边栏宽度 ×1.5
# ---------------------------------------------------------------------------


def test_doc_pane_is_1_5x_current_width():
    """宽度演进：380px（原型）→ 570px（×1.5）→ 855px（570×1.5）→ 750px → 700px（2026-10-19 最终值）。"""
    home = _read("kb-home.css")
    assert "flex:0 0 380px" not in home, "文档栏还是原来的 380px"
    assert "flex:0 0 570px" not in home, "文档栏还是 570px 那一版"
    assert "flex:0 0 855px" not in home, "文档栏还是 855px 那一版"
    assert "flex:0 0 750px" not in home, "文档栏还是 750px 那一版"
    assert "flex:0 0 700px" in home, "文档栏宽度不是用户指定的 700px"


def test_doc_pane_width_keeps_chat_usable():
    """750px 不能把会话列挤没、也不能让文档栏冲出视口：必须有 max-width 兜底。"""
    home = _read("kb-home.css")
    pane = home[home.index(".doc-pane{") : home.index(".doc-pane{") + 900]
    assert "max-width:calc(100vw - var(--sidebar-width, 220px) - 620px)" in pane, (
        "缺少「会话列至少 620px」的宽度兜底（.chat-col 的 min-content 实测约 609px）"
    )


def test_doc_pane_width_change_is_documented_and_scoped():
    """宽度改动要有注释说明；窄屏媒体查询不能被这次改动破坏。"""
    home = _read("kb-home.css")
    pane = home[home.index(".doc-pane{") : home.index(".doc-pane{") + 700]
    assert "1.5" in pane, "宽度改动没有留下依据注释"
    assert "display:none" in pane, "文档栏默认收起的前提被改掉了（会影响与原型的一致性）"
    # 窄屏走的是纵向堆叠，宽度值在那里必须是 auto
    narrow = home[home.index("@media (max-width:960px)") :]
    assert ".doc-pane.open{flex:0 0 auto" in narrow, "窄屏布局被宽度改动影响"


def test_doc_pane_still_toggled_by_docs_button():
    """侧边栏仍由「文档 ▸」按钮切换（宽度改动不应动到交互）。"""
    template = (TEMPLATES / "assistant.html").read_text(encoding="utf-8")
    assert 'id="doc-pane"' in template
    assert 'id="btn-docs"' in template
