"""会话删除入口的前端回归（源码级断言）。

验收点：
  * 侧栏的会话链接与删除按钮是独立控件，删除不会触发打开会话；
  * 删除走 planner 受控操作 `delete_conversation`，**没有**直连 DELETE；
  * 删掉的是当前打开的会话时回到首页（否则会被拉回一个已不存在的会话）；
  * 成功后刷新侧栏列表；
  * 新样式写在 `kb-preview.css`（`kb-proto.css` 是原型样式，只读）。
"""

from __future__ import annotations

from pathlib import Path

WEBUI = Path("apps/webui/static/webui")


def _read(name: str) -> str:
    return (WEBUI / name).read_text(encoding="utf-8")


def test_session_row_has_delete_button():
    nav = _read("kb-nav.js")
    assert "data-session-del=" in nav, "侧栏会话行没有删除入口"
    assert "session-del" in nav, "缺少删除按钮样式类"


def test_delete_click_does_not_navigate():
    """打开会话使用链接，删除按钮在链接之外并独立处理。"""
    nav = _read("kb-nav.js")
    start = nav.index('<a class="session-link"')
    end = nav.index("data-session-del=", start)
    assert "</a>" in nav[start:end]
    handler = nav.split("async function onSessionDelete", 1)[1].split("async function leaveOkay", 1)[0]
    assert 'event.target.closest("[data-session-del]")' in handler
    assert "deleteConversation(" in handler
    assert "location.href" not in handler


def test_deletion_goes_through_controlled_operation():
    nav = _read("kb-nav.js")
    assert 'intent: "delete_conversation"' in nav, "未走 planner 受控意图"
    assert "KB.controlled(" in nav
    # 不允许直连删除：任何形式的 DELETE 方法都不应出现
    for forbidden in ('method: "DELETE"', "method: 'DELETE'", "delete: true"):
        assert forbidden not in nav, f"出现了直连 DELETE 旁路：{forbidden}"
    # 会话端点的调用**只**用于列表读取（GET，无 method 选项）
    assert nav.count('KB.api("/api/v1/planner/conversations/")') == 1, (
        "会话端点出现了列表读取以外的调用"
    )


def test_active_session_delete_returns_home_and_list_refreshes():
    nav = _read("kb-nav.js")
    assert 'location.href = "/app/";' in nav, "删掉当前会话后未回首页"
    assert "await renderSessions(" in nav, "删除后未刷新侧栏列表"
    assert "currentActiveSession()" in nav, "未判断被删的是否为当前会话"
    # 取消与失败要分开反馈
    assert "已取消，未做任何改动" in nav
    assert 'result.detail || "删除失败"' in nav


def test_styles_added_to_preview_css_only():
    """新样式只能进 kb-preview.css；无引用的原型样式表已删除，不得复活。"""
    preview = _read("kb-preview.css")
    assert ".session-del" in preview, "删除按钮样式没写进 kb-preview.css"
    assert ".session-item:hover .session-del" in preview, "缺少 hover 显示规则"
    assert not (WEBUI / "kb-proto.css").exists(), "kb-proto.css 是无引用的死文件，已删除，不能复活"


def test_user_visible_copy_has_no_markdown_markers():
    """用户可见文案不得带 Markdown 标记（确认框里曾出现 **…**）。"""
    nav = _read("kb-nav.js")
    # 取 extraLines 里真正会渲染的那几行（注释里的 ** 不算）
    for fragment in ("将删除会话：", "该会话关联的受控操作记录与凭证"):
        line = [item for item in nav.splitlines() if fragment in item]
        assert line, f"找不到文案：{fragment}"
        for item in line:
            assert "**" not in item, f"用户可见文案仍有 Markdown 标记：{item.strip()}"


def test_backend_note_has_no_markdown_markers(db, admin_user):
    """后端返回界面的 note / summary 不得带 Markdown 标记。

    用**行为断言**（跑一遍受控流程看返回值），而不是扫描源码 ——
    函数 docstring 里出现 `**` 是说明文字，扫源码会误报（第一版就这么误报了）。
    """
    from apps.planner import services
    from apps.planner.models import Conversation, OperationRecord

    conversation = Conversation.objects.create(user=admin_user, title="文案检查会话")
    proposal = services.propose_operation(
        admin_user, intent="delete_conversation", params={"conversation": conversation.pk}
    )
    assert "**" not in proposal["summary"], proposal["summary"]
    operation = OperationRecord.objects.get(pk=proposal["operation_id"])
    services.confirm_operation(admin_user, operation, token=proposal["confirm_token"])
    operation.refresh_from_db()
    result = services.execute_operation(admin_user, operation)["result"]
    assert "**" not in result["note"], result["note"]
