"""文档面板状态机 + 前端错误处理 + 派生文档字段持久化（2026-10-16 用户/Codex 反馈）。

本轮反馈的原始现象：

* 用户：「红框里的『生成草稿/保存/预览』点了没反应，正文都生成好了，『生成草稿』没有合理语义」；
* Codex 独立审查：``renderPreview`` 只更新 HTML、反馈写在滚动区外的 ``#doc-notice``
  （实测 DOM top≈1929 而底栏按钮 top≈636）；``saveDocument`` 只 PUT ``content_markdown``，
  标题/主题是可编辑输入框却存不下来（假保存）；``kb-search.js`` 的 ``sendFeedback``
  不检查 ``result.ok``，403/500 也显示"已记录"；``KB.api`` 断网会 reject 且调用方多无
  ``.catch``；``openDocument`` 并发无票据校验、失败静默 return。

这里把"界面必须做到的事"钉成两类断言：
1. **前端源码级**（本仓没有 JS 测试运行器，用源码断言锁语义，见 README/既有用例约定）；
2. **后端真实 API**（标题/主题真的落库、非法值 400、越权 404、导出文件名用新标题）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.planner import services
from apps.planner.models import DerivedDocument

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parent.parent


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


ASSISTANT_JS = "apps/webui/static/webui/kb-assistant.js"
COMMON_JS = "apps/webui/static/webui/kb-common.js"
NAV_JS = "apps/webui/static/webui/kb-nav.js"
SEARCH_JS = "apps/webui/static/webui/kb-search.js"
ASSISTANT_HTML = "apps/webui/templates/webui/assistant.html"
HOME_CSS = "apps/webui/static/webui/kb-home.css"


def _document(user, *, title="测试草稿", body="# 草稿\n\n正文") -> DerivedDocument:
    return DerivedDocument.objects.create(
        created_by=user, title=title, topic="原始主题", content_markdown=body
    )


# ===================================================== 1) 面板状态与语义 ----


def test_preview_is_an_explicit_edit_preview_toggle():
    """「编辑 / 预览」必须是两个互斥视图，而不是"正文+预览同时铺开+一个预览按钮"。"""
    html = _read(ASSISTANT_HTML)
    assert 'id="doc-tab-edit"' in html and 'id="doc-tab-preview"' in html
    assert 'id="doc-preview-panel"' in html and "hidden" in html
    # 旧的独立「预览」按钮被标签页取代（避免"点了才刷新"的误解）
    assert 'id="doc-preview"' not in html
    js = _read(ASSISTANT_JS)
    assert "function switchDocTab(" in js
    assert 'switchDocTab("preview")' in js and 'switchDocTab("edit")' in js
    # 预览按**当前编辑器内容**渲染
    assert "renderPreview()" in js


def test_status_lives_outside_the_scrolling_body():
    """保存状态必须固定在滚动区之外（Codex：反馈原来在滚动区最底部，用户看不到）。"""
    html = _read(ASSISTANT_HTML)
    status_at = html.index('id="doc-status"')
    body_at = html.index('class="doc-pane-body"')
    assert status_at < body_at, "状态条必须放在 .doc-pane-body 之前（滚动区外）"
    assert 'class="doc-pane-status"' in html
    css = _read(HOME_CSS)
    assert ".doc-pane .doc-pane-status" in css
    assert "flex:0 0 auto" in css


def test_generate_button_only_shows_when_no_draft_is_open():
    """已有正文（打开了草稿记录）时不再显示「生成草稿」；那时给的是「新建草稿」。

    判据是**有没有文档记录**（currentDocId），不是"编辑器里有没有字"：
    用户先填标题/主题再点生成正是正常用法；若按字判断，一填标题按钮就没了
    （第一版实现踩过这个坑，浏览器验收直接点不到按钮）。
    """
    html = _read(ASSISTANT_HTML)
    assert 'id="doc-create"' in html and 'id="doc-new"' in html
    js = _read(ASSISTANT_JS)
    assert "createButton.hidden = hasDoc" in js
    assert "newButton.hidden = !hasDoc" in js
    assert "async function startNewDraft(" in js
    # 已打开草稿时即使被程序化触发也必须拒绝（不能顶掉正在编辑的内容）
    assert "已打开一份草稿" in js


def test_handwritten_content_has_a_real_save_path():
    """手写正文不能再"必须让 AI 覆盖"：按钮变「保存为新草稿」，后端直接建记录。"""
    js = _read(ASSISTANT_JS)
    assert "async function saveNewAuthoredDocument(" in js
    assert "保存为新草稿" in js
    # 手写路径走 POST /documents/ 的 content_markdown 分支，且如实标注非 AI 生成
    assert "content_markdown: body" in js
    assert "非 AI 生成" in js
    services_py = _read("apps/planner/services.py")
    assert "def create_authored_document(" in services_py
    assert 'author": "user"' in services_py
    view_py = _read("apps/planner/views.py")
    assert "create_authored_document(" in view_py


def test_save_has_saving_saved_dirty_and_error_states():
    js = _read(ASSISTANT_JS)
    for text in ("保存中", "已保存", "有未保存的改动", "保存失败"):
        assert text in js, text
    assert "function isDocDirty(" in js
    assert "docBaseline" in js
    # 状态条文本只能由 updateDocUI 统一决定，且"进行中"用 docBusy 优先显示
    assert "let docBusy" in js
    assert "if (docBusy) {" in js
    assert "docBusy = { kind: \"saving\"" in js


def test_save_races_do_not_mark_new_typing_as_saved():
    """Codex 第三批 A：保存返回成功，**不能**把保存期间新打的内容也算成已保存。"""
    js = _read(ASSISTANT_JS)
    assert "const snapshot = readEditor();" in js          # 请求前冻结提交内容
    assert "const targetId = currentDocId;" in js         # 冻结目标
    assert "docBaseline = confirmed;" in js               # 基线只用服务端确认的那一版
    assert "typedDuringSave" in js
    assert "保存期间的新改动还**没有**保存" in js
    assert "if (currentDocId !== targetId)" in js         # 目标切换不污染新目标


def test_opening_a_document_never_overwrites_concurrent_edits():
    """Codex 第三批 B：请求期间用户又编辑 → 不覆盖编辑器，明确提示。"""
    js = _read(ASSISTANT_JS)
    assert "const revisionAtRequest = docEditRevision;" in js
    assert "docEditRevision !== revisionAtRequest" in js
    assert "编辑器保持原样" in js
    # 新建草稿要作废未完成的加载
    assert "docLoadTicket += 1;" in js


def test_in_site_navigation_and_unload_are_guarded():
    """Codex 第三批 C：点导航/退出/关标签离开时，未保存正文不能静默丢。"""
    js = _read(ASSISTANT_JS)
    nav = _read(NAV_JS)
    assert "KB.docIsDirty = function" in js
    assert 'window.addEventListener("beforeunload"' in js
    # 捕获阶段拦截 + 放行修饰键/新标签
    assert 'document.addEventListener("click", function (event) {' in nav
    assert "event.metaKey || event.ctrlKey || event.shiftKey || event.altKey" in nav
    assert 'target.getAttribute("target") === "_blank"' in nav
    assert 'addEventListener("submit"' in nav and "logout" in nav
    # 有脏内容才拦（没脏完全走原生导航，零打扰）
    assert "if (!docDirty()) return;" in nav


def test_disabled_buttons_explain_themselves():
    js = _read(ASSISTANT_JS)
    html = _read(ASSISTANT_HTML)
    assert 'id="doc-hint"' in html
    assert "button.title = " in js            # 每个禁用都带 title 理由
    assert "hint.textContent = text2" in js   # 还有一行**可见**说明
    assert "先打开或生成一份草稿" in js


def test_unsaved_edits_are_never_dropped_silently():
    js = _read(ASSISTANT_JS)
    nav = _read(NAV_JS)
    # 切文档 / 收面板 / 新建会话 都要先确认
    assert "async function confirmDiscardDocEdits(" in js
    assert 'confirmDiscardDocEdits("切换到另一份草稿")' in js
    assert 'confirmDiscardDocEdits("收起文档面板")' in js
    assert 'confirmDiscardDocEdits("新建会话")' in js
    # 跨页导航（会话列表/新建会话/删除会话）走同一个钩子
    assert "KB.docDirtyGuard = function" in js
    assert "leaveOkay(" in nav and "KB.docDirtyGuard" in nav


def test_generation_never_overwrites_an_edited_document():
    """AI 撰写只会**新建**一份草稿；不会改动打开中的那份（也不能清掉未保存编辑）。"""
    js = _read(ASSISTANT_JS)
    # 生成走 POST /documents/（新建），不是 PUT（覆盖）
    assert 'method: "POST"' in js
    # 打开新生成的草稿时跳过"丢弃改动"确认（那是**另一份**记录），但保留结果提示；
    # 且只有真的打开了才说"已生成"，并且要消费生成期间的降级警告
    assert "const opened = await openDocument(result.body.document_id," in js
    assert "{ skipGuard: true, keepFlash: true }" in js
    assert "if (!opened) {" in js
    assert "pendingGenerationFlash" in js
    # 生成期间用户又编辑/又切草稿 → 不打开新草稿，也不动编辑器
    assert "但你在生成期间又编辑了内容，编辑器保持原样" in js


def test_open_document_has_a_ticket_against_out_of_order_responses():
    js = _read(ASSISTANT_JS)
    assert "docLoadTicket" in js
    assert "if (ticket !== docLoadTicket) return false;" in js
    # 失败必须可见，不能静默 return
    assert "打开失败：" in js


def test_export_uses_the_current_editor_version_and_surfaces_errors():
    js = _read(ASSISTANT_JS)
    assert "async function downloadDoc(" in js
    # 有未保存改动先保存；保存失败就不导出（否则导出的是旧版本）
    assert "if (isDocDirty())" in js and "const saved = await saveDocument();" in js
    assert "导出已取消：当前改动保存失败" in js
    # 用 fetch + Blob（不是 location.href：失败时会导航到 JSON 错误页，用户看不到原因）
    assert "await response.blob()" in js
    assert "导出失败（" in js
    assert 'location.href = "/api/v1/planner/documents/"' not in js
    # 下载文件名从 Content-Disposition 取（中文用 filename*=UTF-8）
    assert "function filenameFromDisposition(" in js
    assert "filename\\*=UTF-8" in js


def test_doc_panel_hidden_attribute_actually_hides():
    """`.field{display:block}` 会盖掉 UA 的 `[hidden]`，必须显式再写一条。"""
    css = _read(HOME_CSS)
    assert ".doc-pane [hidden]{display:none !important}" in css


# ================================================= 2) 客户端错误处理 ----


def test_kb_api_never_rejects_on_network_failure():
    js = _read(COMMON_JS)
    assert "} catch (err) {" in js
    assert "network: true" in js
    assert "网络请求失败" in js
    # 401 跳登录但**不抛异常**（旧实现 throw，调用方未 catch 就被打断）
    assert 'KB._redirectingToLogin' in js
    assert "登录已过期" in js
    # 统一把失败翻译成可显示文案
    assert "KB.errorText = function" in js


def test_kb_api_survives_a_broken_response_body():
    """`response.text()` 本身也可能失败（连接在流中途断开）—— 只 catch fetch 不够。"""
    js = _read(COMMON_JS)
    assert "读取响应失败" in js
    assert "text = await response.text();" in js


def test_unauthenticated_redirect_only_for_not_authenticated():
    """只把"未认证"当会话过期；权限 403 / CSRF 403 原地报错。"""
    js = _read(COMMON_JS)
    assert 'body.code === "not_authenticated"' in js
    assert "notAuthenticated" in js
    # 后端必须给出机器可读 code（不靠本地化文案）
    exc = _read("config/exceptions.py")
    assert "not_authenticated" in exc
    assert "permission_denied" in exc


def test_ime_enter_does_not_send_requests():
    """中文输入法组合中的 Enter 是选词，不能触发发送/检索（Codex 实测）。"""
    for path in (ASSISTANT_JS, SEARCH_JS):
        js = _read(path)
        assert "event.isComposing" in js, path
        assert "event.keyCode === 229" in js, path


def test_files_page_load_has_a_request_ticket():
    """文件列表也必须防乱序：慢的旧响应不能覆盖新筛选（Codex 实测）。"""
    js = _read("apps/webui/static/webui/kb-files.js")
    assert "let loadTicket = 0;" in js
    assert "const ticket = ++loadTicket;" in js
    assert "if (ticket !== loadTicket) return;" in js


def test_home_upload_reports_per_file_failures():
    """首页「文件」上传不能只报"已提交 N 份"：失败原因要逐份如实留下。"""
    js = _read(ASSISTANT_JS)
    assert "failures.push(file.name" in js
    assert "kb-upload-failures" in js
    assert "input.value = \"\"" in js or "input.value = ''" in js
    assert "uploadBusy" in js


def test_admin_only_controls_are_gated_for_plain_users():
    """普通账号：Agent 运维与「测试连通」必须置灰并写明原因（后端已 403）。"""
    pages = _read("apps/webui/static/webui/kb-pages.js")
    assert "const ADMIN_ONLY_SELECTORS" in pages
    for selector in ("[data-gen-activate]", "[data-gen-rollback]", "#audit-verify",
                     "[data-agent-action]", "[data-test-model]"):
        assert selector in pages, selector
    assert "function gateAdminControls(" in pages
    assert "gateAdminControls(main);" in pages
    # 两处调用：Agent 控制台与系统配置
    assert pages.count("gateAdminControls(main);") >= 2
    assert "需要系统管理员权限" in pages


def test_api_failures_are_not_rendered_as_empty_states():
    """处置清单 AF-07：接口失败不能渲染成"还没有…"（用户会以为数据没了）。"""
    js = _read(ASSISTANT_JS)
    # 草稿列表
    assert "草稿列表读取失败" in js
    assert "这不是「还没有草稿」" in js
    # 待办操作与清理任务
    assert "async function loadOperations()" in js
    assert "return null;" in js, "loadOperations 失败时没有与空数组区分开"
    assert "待办操作读取失败" in js
    assert "清理任务读取失败" in js
    assert "这不等于「没有清理任务」" in js
    # 统计失败不显示 0
    assert "function welcomeHtml(counts, statsFailed)" in js
    assert "统计读取失败" in js
    assert 'return statsFailed ? "—" : value;' in js


def test_tasks_page_uses_the_shared_nature_wording():
    """处置清单：任务列表页的性质空值也必须显示「待识别」（与文件/搜索同口径）。"""
    pages = _read("apps/webui/static/webui/kb-pages.js")
    assert "KB.natureLabel" in pages
    assert "KB.natureTooltip" in pages
    assert 'esc(file.classification || "—")' not in pages


def test_search_feedback_checks_result_ok():
    js = _read(SEARCH_JS)
    assert "result.ok" in js
    assert "反馈**没有**记录成功" in js
    assert "重试 · 准确" in js and "重试 · 不准确" in js
    # 不能再无条件说"已记录"
    assert '}).then(function () {' not in js


# ================================================= 3) 后端：字段真的落库 ----


def test_put_persists_title_topic_and_body(api, user):
    document = _document(user)
    api.force_authenticate(user=user)
    response = api.put(
        f"/api/v1/planner/documents/{document.pk}/",
        {
            "title": "新标题",
            "topic": "新主题",
            "content_markdown": "# 改过的正文",
        },
        format="json",
    )
    assert response.status_code == 200, response.content[:300]
    document.refresh_from_db()
    assert document.title == "新标题"
    assert document.topic == "新主题"
    assert document.content_markdown == "# 改过的正文"
    assert document.status == DerivedDocument.Status.EDITED
    # 返回体里也要带上，前端据此更新基线
    assert response.data["title"] == "新标题"
    assert response.data["topic"] == "新主题"


def test_put_without_title_field_keeps_existing_title(api, user):
    """旧调用方只发 content_markdown：标题不该被清空（向后兼容）。"""
    document = _document(user, title="原标题")
    api.force_authenticate(user=user)
    response = api.put(
        f"/api/v1/planner/documents/{document.pk}/",
        {"content_markdown": "只改正文"},
        format="json",
    )
    assert response.status_code == 200
    document.refresh_from_db()
    assert document.title == "原标题"
    assert document.topic == "原始主题"


@pytest.mark.parametrize(
    "payload, keyword",
    [
        ({"title": "   ", "content_markdown": "x"}, "标题不能为空"),
        ({"title": "长" * 301, "content_markdown": "x"}, "标题过长"),
        ({"topic": "长" * 301, "content_markdown": "x"}, "主题过长"),
    ],
)
def test_put_rejects_invalid_fields_with_400(api, user, payload, keyword):
    document = _document(user, title="原标题")
    api.force_authenticate(user=user)
    response = api.put(
        f"/api/v1/planner/documents/{document.pk}/", payload, format="json"
    )
    assert response.status_code == 400, response.content[:300]
    assert keyword in response.data["detail"]
    document.refresh_from_db()
    assert document.title == "原标题", "校验失败不能改脏数据"


def test_service_rejects_other_users_document(user, other_user):
    document = _document(other_user, title="别人的草稿")
    with pytest.raises(services.PlannerError):
        services.update_derived_document(user, document.pk, content_markdown="偷改")
    document.refresh_from_db()
    assert document.content_markdown == "# 草稿\n\n正文"


def test_put_and_delete_are_404_for_non_owner(api, user, other_user):
    document = _document(other_user)
    api.force_authenticate(user=user)
    assert api.put(
        f"/api/v1/planner/documents/{document.pk}/",
        {"content_markdown": "偷改"},
        format="json",
    ).status_code == 404
    assert api.delete(f"/api/v1/planner/documents/{document.pk}/").status_code == 404
    document.refresh_from_db()
    assert document.content_markdown == "# 草稿\n\n正文"


def test_export_filename_uses_the_saved_title(api, user):
    """标题改了以后，导出的文件名必须跟着变（否则"标题保存了"没有意义）。"""
    document = _document(user, title="旧标题")
    api.force_authenticate(user=user)
    assert api.put(
        f"/api/v1/planner/documents/{document.pk}/",
        {"title": "陵水出差报告", "content_markdown": "# 正文\n\n内容"},
        format="json",
    ).status_code == 200
    response = api.get(
        f"/api/v1/planner/documents/{document.pk}/download/?export_format=md"
    )
    assert response.status_code == 200
    disposition = response["Content-Disposition"]
    assert "UTF-8''" in disposition
    from urllib.parse import unquote

    assert "陵水出差报告" in unquote(disposition.split("UTF-8''")[1])
    assert response["X-Document-Format"] == "md"


def test_download_unknown_format_is_a_visible_400(api, user):
    """导出失败必须能让前端拿到 detail（用于状态条显示），不是空响应。"""
    document = _document(user)
    api.force_authenticate(user=user)
    response = api.get(
        f"/api/v1/planner/documents/{document.pk}/download/?export_format=rtf"
    )
    assert response.status_code == 400
    assert "detail" in response.data


def test_document_dict_carries_fields_the_frontend_needs(api, user):
    """前端要用 title/topic/content_markdown/updated_at/sources 来填面板与基线。"""
    document = _document(user, title="字段完整性")
    api.force_authenticate(user=user)
    body = api.get(f"/api/v1/planner/documents/{document.pk}/").data
    payload = json.loads(json.dumps(body, ensure_ascii=False, default=str))
    for key in ("id", "title", "topic", "content_markdown", "status", "sources", "updated_at"):
        assert key in payload, key


# ================================================= 4) 手写草稿（不叫模型） ----


def test_handwritten_draft_does_not_call_the_model(api, user, monkeypatch):
    """POST 带 content_markdown → 直接建记录，**不调用模型**（也不冒充 AI 生成）。"""
    called = {"compose": 0}

    def boom(*args, **kwargs):  # pragma: no cover - 被调用即失败
        called["compose"] += 1
        raise AssertionError("手写草稿不该走 compose_document")

    monkeypatch.setattr(services, "compose_document", boom)
    api.force_authenticate(user=user)
    response = api.post(
        "/api/v1/planner/documents/",
        {"title": "我自己写的草稿", "topic": "手写", "content_markdown": "# 手写\n\n内容"},
        format="json",
    )
    assert response.status_code == 201, response.content[:300]
    assert called["compose"] == 0
    assert response.data["author"] == "user"
    assert "不是 AI 生成" in response.data["note"]
    document = DerivedDocument.objects.get(pk=response.data["document_id"])
    assert document.title == "我自己写的草稿"
    assert document.content_markdown == "# 手写\n\n内容"
    assert document.sources.count() == 0


@pytest.mark.parametrize(
    "payload, keyword",
    [
        ({"content_markdown": "正文", "title": "   "}, "标题不能为空"),
        ({"content_markdown": "正文", "title": "长" * 301}, "标题过长"),
        ({"content_markdown": "正文", "title": "ok", "topic": "长" * 301}, "主题过长"),
    ],
)
def test_handwritten_draft_validates_fields(api, user, payload, keyword):
    api.force_authenticate(user=user)
    response = api.post("/api/v1/planner/documents/", payload, format="json")
    assert response.status_code == 400, response.content[:200]
    assert keyword in response.data["detail"]
    assert DerivedDocument.objects.count() == 0


def test_handwritten_draft_can_be_edited_and_exported(api, user):
    api.force_authenticate(user=user)
    created = api.post(
        "/api/v1/planner/documents/",
        {"title": "手写可导出", "content_markdown": "# 标题\n\n正文"},
        format="json",
    )
    doc_id = created.data["document_id"]
    assert api.put(
        f"/api/v1/planner/documents/{doc_id}/",
        {"title": "手写可导出（改名）", "content_markdown": "# 改过的正文"},
        format="json",
    ).status_code == 200
    download = api.get(f"/api/v1/planner/documents/{doc_id}/download/?export_format=md")
    assert download.status_code == 200
    assert "改过的正文" in download.content.decode("utf-8")


# ================================================= 5) 未认证与审计 limit ----


def test_unauthenticated_is_401_with_machine_code(api):
    """未认证必须是 401 + code（DRF 默认渲染成 403，前端就无法区分会话过期与没权限）。"""
    response = api.get("/api/v1/planner/documents/")
    assert response.status_code == 401, response.status_code
    assert response.data.get("code") == "not_authenticated"


def test_permission_403_is_not_reported_as_session_expired(api, user):
    """权限 403 必须保持 403 + permission_denied（不能诱导前端跳登录）。"""
    api.force_authenticate(user=user)
    response = api.get("/api/v1/audit/events/")
    assert response.status_code == 403
    assert response.data.get("code") == "permission_denied"


@pytest.mark.parametrize("bad", ["abc", "0", "-3", "1.5", "999999999"])
def test_audit_verify_limit_is_validated(api, admin_user, bad):
    api.force_authenticate(user=admin_user)
    response = api.get(f"/api/v1/audit/verify/?limit={bad}")
    assert response.status_code == 400, response.content[:200]
    assert response.data.get("code") == "invalid_limit"


def test_audit_verify_limit_accepts_a_bounded_integer(api, admin_user):
    api.force_authenticate(user=admin_user)
    response = api.get("/api/v1/audit/verify/?limit=5")
    assert response.status_code == 200


# ------------------- 公文模板预览：按公文口径排印（2026-10-19） -------------------


def test_official_preview_marks_chinese_numbered_headings() -> None:
    """预览与 Word 导出同一套口径：给「一、」「（一）」打类，而不是靠 Markdown 标题。"""
    js = _read(ASSISTANT_JS)
    assert "function decorateOfficialPreview" in js
    assert "gb-h1" in js and "gb-h2" in js and "gb-h3" in js
    assert "gb-signature" in js
    assert "gb-title" in js
    assert "decorateOfficialPreview(target)" in js


def test_official_preview_css_styles_gb_classes() -> None:
    css = _read(HOME_CSS)
    for selector in (
        ".paper.is-official .gb-h1",
        ".paper.is-official .gb-h2",
        ".paper.is-official .gb-h3",
        ".paper.is-official .gb-signature",
        ".paper.is-official h1, .paper.is-official .gb-title",
    ):
        assert selector in css
    # 三种字体（黑体/楷体/仿宋）与居右落款都要在样式里
    assert "SimHei" in css
    assert "KaiTi" in css
    assert "text-align:right" in css


def test_official_preview_separates_signature_lines() -> None:
    """Markdown 会把相邻行合并成一段；公文要求落款单位与成文日期各占一段。"""
    js = _read(ASSISTANT_JS)
    assert "function prepareOfficialMarkdown" in js
    assert "prepareOfficialMarkdown(raw)" in js
