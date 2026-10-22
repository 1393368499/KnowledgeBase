"""信息库删库入口与顶部按钮移除的前端回归（源码级断言）。

验收点：
  * 顶部导航的「清理与重置」按钮**已从模板移除**，且 **JS 里不留死 handler**；
  * 信息库卡片提供「删除信息库」，**仅系统管理员可见**；
  * 删库**必须**走 planner 受控操作（`delete_library`）——三次确认 + 末次输入完整库名，
    **没有**任何直连 `DELETE` 的旁路；
  * 确认卡写明影响范围（库名 / 资料数量 / 索引影响）；
  * 成功后刷新页面；未全部完成时**诚实反馈**并可重试。
"""

from __future__ import annotations

from pathlib import Path

WEBUI = Path("apps/webui/static/webui")
TEMPLATES = Path("apps/webui/templates/webui")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_top_nav_cleanup_button_is_removed():
    """顶部导航**不再有**该按钮元素。

    注意断言对象是**按钮标记**而不是这四个字：说明为什么移除的注释里
    仍会出现这个短语，用文案断言会误报（我第一版就误报了）。
    """
    import re

    base = _read(TEMPLATES / "base.html")
    assert "btn-reset" not in base, "顶部导航仍残留按钮 id"
    # 任何 <button> 里都不应再出现这个文案
    buttons = re.findall(r"<button[^>]*>.*?</button>", base, flags=re.S)
    for markup in buttons:
        assert "清理与重置" not in markup, f"顶部导航仍有该按钮：{markup[:80]}"


def test_no_dead_bindreset_handler_left():
    nav = _read(WEBUI / "kb-nav.js")
    assert "bindReset" not in nav, "按钮已移除，但仍留有 bindReset 死代码/调用"
    assert "btn-reset" not in nav, "仍在查找已移除的元素"


def test_library_card_has_admin_only_delete_button():
    pages = _read(WEBUI / "kb-pages.js")
    assert "data-lib-delete" in pages, "信息库卡片没有删除入口"
    assert "isSystemAdmin()" in pages, "删除入口未做管理员可见性判断"
    # 管理员标记来自模板（前端隐藏不是安全边界，后端仍会验权限）
    base = _read(TEMPLATES / "base.html")
    assert 'data-system-admin="1"' in base, "模板未下发系统管理员标记"


def test_deletion_goes_through_controlled_flow_only():
    pages = _read(WEBUI / "kb-pages.js")
    assert 'intent: "delete_library"' in pages, "未走 planner 受控意图"
    assert "KB.controlled(" in pages, "未复用受控流程（三次确认 + 凭证）"
    # **不得**存在绕过受控流程的直连删除
    for forbidden in ('method: "DELETE"', "method: 'DELETE'", "delete_library_ipc"):
        assert forbidden not in pages, f"出现了不受控的删除旁路：{forbidden}"


def test_confirm_card_states_scope_and_typed_name():
    common = _read(WEBUI / "kb-common.js")
    # 影响说明进入确认卡
    assert "extraLines" in common, "受控流程不支持展示影响说明"
    # 末次确认必须输入目标名称，并把 typed_name 交给服务端复核
    assert "requireTypedName" in common
    assert "typed_name: answer.typedName" in common, "typed_name 未提交给服务端复核"
    pages = _read(WEBUI / "kb-pages.js")
    for fragment in ("将删除信息库", "库内资料记录", "向量索引", "原件按内容寻址只读保留"):
        assert fragment in pages, f"确认卡缺少影响说明：{fragment}"


def test_success_refreshes_and_partial_is_honest():
    pages = _read(WEBUI / "kb-pages.js")
    assert "await loadKnowledge()" in pages, "删除成功后未刷新页面"
    assert "未全部完成" in pages, "未如实反馈未完成的清理步骤"
    assert "可重试" in pages, "未提示可重试"
    # 取消时不得改动任何东西
    assert "已取消，未做任何改动" in pages


def test_admin_flag_matches_backend_is_system_admin(db, client):
    """模板上的 `data-system-admin` 必须与后端 `is_system_admin` **同口径**。

    后端口径是 `is_staff or is_superuser`；模板此前只判 `is_staff`，
    会出现"超级用户（非 staff）后端能删、界面却没有入口"的不一致。
    """
    from django.contrib.auth import get_user_model
    from django.urls import reverse

    from apps.knowledge.permissions import is_system_admin

    user_model = get_user_model()
    superuser_only = user_model.objects.create_user(username="su-only", password="pw-x-123")
    superuser_only.is_superuser = True
    superuser_only.is_staff = False
    superuser_only.save(update_fields=["is_superuser", "is_staff"])

    client.force_login(superuser_only)
    body = client.get(reverse("webui:home")).content.decode("utf-8")
    assert is_system_admin(superuser_only) is True, "后端认定它是系统管理员"
    assert 'data-system-admin="1"' in body, "模板未按后端口径下发管理员标记"


def test_admin_flag_absent_for_plain_user(db, client, user):
    from django.urls import reverse

    client.force_login(user)
    body = client.get(reverse("webui:home")).content.decode("utf-8")
    assert 'data-system-admin="1"' not in body


def test_impact_comes_from_proposal_not_frontend_guessing():
    """影响范围必须来自**提案响应**，前端不得自己查（会因字段类型/分页而失真）。"""
    pages = _read(WEBUI / "kb-pages.js")
    assert "impactFormatter" in pages, "未使用后端返回的影响范围"
    # 旧实现的两个真实缺陷都要消失
    assert "knowledge/collections" not in pages, "前端仍在自查集合（library 是整数 pk，比对会永远为空）"
    assert "page_size=500" not in pages, "前端仍在用会被服务端封顶的 page_size"
    common = _read(WEBUI / "kb-common.js")
    assert "body.impact" in common, "受控流程未消费提案返回的 impact"


def test_pdf_alt_tracks_current_page():
    """PDF 的 alt 必须随页码更新（否则翻到第 3 页仍写着"第 1 页"）。"""
    common = _read(WEBUI / "kb-common.js")
    # 精确切到 bindPdfViewer：文件里 bindImageViewer 也有同名 "load" 监听，
    # 用全局第一次出现会切到**另一个**函数体（第一版断言就是这么误报的）。
    fn_start = common.index("function bindPdfViewer(")
    fn_end = common.index("KB.toast = function", fn_start)
    pdf_fn = common[fn_start:fn_end]
    assert 'img.alt = "第 " + page + " 页"' in pdf_fn, "alt 未随页码同步"
    load_start = pdf_fn.index("const load = function ()")
    assert "img.alt" in pdf_fn[load_start:], "alt 未在处理翻页的 load() 中更新"
