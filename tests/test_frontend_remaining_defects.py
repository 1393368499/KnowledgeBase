"""处置清单（2026-10-16）本轮 7 项前端修复的回归锁。

本文件只锁**用户可见行为**，每条都尽量给出「源码级断言 + 真实接口/渲染证据」：

1. 消息发送失败 → 会话区留下**持久**错误条目（带服务端 detail）+ 可重试（复用原文）；
2. 所有弹窗 Esc 可关（焦点进入弹窗、关闭后回到触发元素；确认框 Esc = 取消不推进）；
3. 切换/新建会话 → 清 URL 的 `?conversation=`、清空「指定来源」勾选与文案；
4. 非管理员的资料操作入口 → 按**后端真实权限**置灰（解析 = 可写入；删除/归档/
   改性质 = 库管理员；「全部解析」= 系统管理员），只做显示层，不替代后端校验；
5. 预览失败 → 显示服务端 detail（不再是"可能没有权限"）；
6. 搜索清空 → 地址栏同步清空（可刷新、可分享）；
7. 任务页分页 → 请求在途禁用 + 页码文案与**真实请求页**一致。

（另附一条随本轮一并对齐的口径：行内「性质」下拉不再承诺"清空后会自动识别"。）
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model

from apps.knowledge.models import (
    IngestedFile,
    KnowledgeLibrary,
    LibraryMember,
    MemberRole,
    ParseStatus,
    VectorCollection,
)

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parents[1]

ASSISTANT_JS = ROOT / "apps/webui/static/webui/kb-assistant.js"
COMMON_JS = ROOT / "apps/webui/static/webui/kb-common.js"
FILES_JS = ROOT / "apps/webui/static/webui/kb-files.js"
PAGES_JS = ROOT / "apps/webui/static/webui/kb-pages.js"
SEARCH_JS = ROOT / "apps/webui/static/webui/kb-search.js"
FILES_HTML = ROOT / "apps/webui/templates/webui/files.html"
HOME_CSS = ROOT / "apps/webui/static/webui/kb-home.css"

User = get_user_model()


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _slice(source: str, start_marker: str, end_marker: str) -> str:
    """取两个标记之间的源码（断言只作用于这段，避免同名字符串在别处命中）。"""
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


def clip_part(text: str, limit: int = 160) -> str:
    return " ".join(str(text or "").split())[:limit]


# =========================================== 1) 发送失败：持久提示 + 重试 ====


def test_send_failure_is_a_persistent_entry_with_server_reason():
    js = _read(ASSISTANT_JS)
    # 失败分支不能再是"弹一句 toast 就完事"
    assert 'KB.toast("请求失败，请稍后重试")' not in js
    assert "function insertSendFailure(" in js
    body = _slice(js, "function insertSendFailure(", "async function sendOnce(")
    assert 'node.id = "kb-send-failure"' in body
    assert "notice-bar danger" in body
    assert "服务端原因：" in body
    # **契约已更正**（用户 2026-10-16）：后端在模型/检索之前就写了用户消息，
    # ChatView 没有整体事务 —— 500 或响应断网可能发生在**已落库之后**。
    # 所以只有"没收到成功回执"是可以断言的；不能宣称没写库、也不能说刷新不保留。
    assert "没有收到成功回执" in body
    assert "暂时无法确认这条消息是否已经保存" in body
    assert "请先核对上面的会话记录" in body
    assert "没有**写进会话记录" not in body
    assert "刷新后不会保留" not in body
    # 只有服务端**明确拒绝**（400）才可以说"请求被拒绝/没有执行"
    assert 'Number(status) === 400' in body
    assert "请求被服务端拒绝" in body
    # 失败分支把服务端 detail 原样带出来，并把状态码传下去（决定文案口径）
    send_body = _slice(js, "async function sendOnce(", "// ------------------------------------------------------------ 工具栏 ----")
    assert "KB.errorText(result" in send_body
    assert "insertSendFailure(content, detail, result.status)" in send_body


def test_send_failure_retry_reuses_the_original_text():
    js = _read(ASSISTANT_JS)
    # 重试按钮 + 原文存在条目上（不是靠 DOM 里的气泡反查）
    assert 'node.dataset.retryText = content;' in js
    assert "kb-send-retry" in js
    handler = _slice(js, "inner.addEventListener(\"click\"", "const nav = event.target.closest")
    assert 'closest("#kb-send-retry, [data-retry-send]")' in handler
    assert 'area.value = text;' in handler          # 原文放回输入框
    assert "send(text);" in handler                  # 用原文重发
    # 503 分支同样给出重试入口，并保留服务端 detail
    assert 'data-retry-send="' in js
    assert 'data-retry-wrap="1"' in js


def test_duplicate_send_attempt_is_not_silently_swallowed():
    js = _read(ASSISTANT_JS)
    send_body = _slice(js, "async function send(text)", "function insertSendFailure(")
    assert "上一条还在发送中" in send_body


# ============================================ 2) Esc 关模态 + 焦点管理 ====


def test_modal_stack_closes_on_escape_with_cancel_semantics():
    js = _read(COMMON_JS)
    assert "const modalStack = [];" in js
    assert "function closeModal(mask)" in js
    assert "KB.closeModal = closeModal;" in js
    # Esc 只关栈顶那一层（叠层弹窗不能一次按键拆掉两层）
    keydown = _slice(js, 'document.addEventListener("keydown"', "/** 与原型 modal() 一致")
    assert 'event.key !== "Escape"' in keydown
    assert "modalStack[modalStack.length - 1]" in keydown
    # 确认框 / 选择框：任何关闭路径都按"取消"结算（不推进、不写库）
    assert "KB.confirmDialog = function" in js
    confirm = _slice(js, "KB.confirmDialog = function", "/** 通用\"选一个\"弹窗")
    assert "onClose: function () { finish({ confirmed: false, typedName: \"\" }); }" in confirm
    choice = _slice(js, "KB.choice = function", "/* ------------------------------------------------------- 弹窗栈 ----")
    assert "onClose: function () { finish(null); }" in choice


def test_modal_moves_focus_in_and_restores_it_on_close():
    js = _read(COMMON_JS)
    modal = _slice(js, "KB.modal = function (html, width, options)", "KB.el = function")
    assert "const startedFrom = document.activeElement;" in modal
    assert "mask._kbRestoreFocus" in modal
    assert "restore.focus();" in js
    # 焦点进入弹窗（优先 autofocus，否则第一个可聚焦元素，最后兜底弹层）
    assert "MODAL_FOCUSABLE" in js
    assert "target.focus();" in modal
    # 覆盖 remove()：所有既有调用点（Esc/遮罩/✕/程序化）都走同一条结算路径
    assert "mask.remove = requestClose;" in modal
    # 破坏性确认要抄名称时焦点直接进输入框
    assert 'initialFocus: options.requireTypedName' in js


def test_modal_stack_is_wired_for_the_controlled_confirm_flow():
    """受控流程（KB.controlled 的三次确认）必须能被 Esc 取消。"""
    js = _read(COMMON_JS)
    controlled = _slice(js, "KB.controlled = async function", "KB.confirmDialog = function")
    assert "const answer = await KB.confirmDialog(" in controlled
    assert "if (!answer.confirmed) {" in controlled
    assert "skipped: true" in controlled


# ================================= 3) 切会话：清 URL + 清来源勾选 ==========


def test_new_session_clears_conversation_param_and_chosen_sources():
    js = _read(ASSISTANT_JS)
    assert "function clearChosenSources()" in js
    assert "function clearConversationFromUrl()" in js
    clear = _slice(js, "function clearConversationFromUrl()", "// ---------------------------------------------------------------- 启动 ----")
    assert 'params.delete("conversation")' in clear
    assert "history.replaceState" in clear
    handler = _slice(js, '$("btn-new-session").addEventListener', '$("btn-tools")')
    assert "clearConversationFromUrl();" in handler
    assert "clearChosenSources();" in handler
    # 新会话要重新读会话区，而不是留着旧消息
    assert "renderHome(true);" in handler


def test_clearing_chosen_sources_also_unchecks_the_dom():
    js = _read(ASSISTANT_JS)
    body = _slice(js, "function clearChosenSources()", "function clearConversationFromUrl()")
    assert "chosenSources.clear();" in body
    assert 'input[type="checkbox"]' in body
    assert "check.checked = false;" in body
    assert "updateChosen();" in body


def test_switching_drafts_does_not_leak_source_selection():
    """打开另一份草稿 / 新建草稿都必须清空来源勾选（AF-13 的根因）。"""
    js = _read(ASSISTANT_JS)
    new_draft = _slice(js, "async function startNewDraft()", "function filenameFromDisposition")
    assert "clearChosenSources();" in new_draft
    open_doc = _slice(js, "async function openDocument(id, options)", "function renderPreview()")
    assert "clearChosenSources();" in open_doc


# ============================ 4) 非管理员的资料操作入口（显示层） ==========


def test_files_page_gates_row_actions_by_real_backend_permissions():
    js = _read(FILES_JS)
    # 角色来自服务端序列化字段 my_role，不是前端猜的
    assert "row.my_role" in js
    assert "const ROLE_RANK = { reader: 1, writer: 2, admin: 3 };" in js
    assert "function canActOn(file, required)" in js
    assert "function denyControl(element, reason)" in js
    # 解析 = 可写入；删除/归档/改性质 = 库管理员
    assert '!canActOn(file, "writer")' in js
    assert '!canActOn(file, "admin")' in js
    # 置灰原因必须写进 title，且要有**可见**的说明区
    assert "data-perm-denied" in js
    assert 'id="files-perm-note"' in _read(FILES_HTML)
    assert "function updatePermissionNote()" in js
    # 用户可见文案讲清"哪个库、缺什么权限、下一步怎么做"；**口令与术语**的最终裁判
    # 是真实浏览器验收（artifacts/acceptance/verify-remaining-defects-browser.py
    # 的 *_sees_*_reason_with_next_step 三条），这里只锁最低要求：
    assert "可联系该库管理员" in js
    assert "需要你在「" in js
    # 置灰的 title 与可见说明区都必须是"人话"，不能把 HTTP 状态码当理由
    assert "需要该信息库的可写入权限" not in js
    assert "需要该信息库的库管理员权限" not in js
    # 「全部解析」按**库的写入权限**判定（不是"必须系统管理员"：库 writer 在
    # 自己的库里批量解析本来就是合法的）
    assert "function bulkParseAllowed()" in js
    assert "function updateBulkActionsGate()" in js
    assert "isSystemAdmin()" in js
    assert "data-system-admin" in js
    bulk = _slice(js, "function bulkParseAllowed()", "function updateBulkActionsGate()")
    assert 'canActOn({ library: id }, "writer")' in bulk
    assert "系统管理员" not in bulk
    # 上传/目录选择弹窗**只列可写入的库**（不给只读库一个注定失败的入口）
    assert "KB.fetchWritableLibraries" in js
    assert "writable.blocked.length" in js
    common = _read(COMMON_JS)
    assert "KB.fetchWritableLibraries = async function" in common
    assert 'row.my_role === "writer" || row.my_role === "admin"' in common


def test_denied_buttons_are_disabled_not_just_styled():
    js = _read(FILES_JS)
    modal = _slice(js, "function permissionCellHtml(", "/** 「性质」单元格")
    assert 'bits.push(" disabled");' in modal
    assert "data-perm-denied" in modal
    # 状态机原因与权限原因**同时**保留在 title 里（不能互相盖掉）
    assert "const combined = permissionReason + (attrs.title ? \" \" + attrs.title : \"\");" in modal


def test_files_page_gating_does_not_replace_backend_checks():
    """前端只做显示层：信息取不到时**全部放行**，由后端如实报错。"""
    js = _read(FILES_JS)
    assert "if (!libraryRoles) return true;" in js
    assert "if (!libraryRoles.has(key)) return true;" in js
    assert "libraryRoles = null;" in js
    # 后端权限本身没有被动过：管理员权限类仍在，且操作目录仍声明 library:admin
    services = _read(ROOT / "apps/planner/services.py")
    assert "CanManageLibrary() if permission == \"library:admin\"" in services
    operations = _read(ROOT / "apps/planner/operations.py")
    for intent in ("delete_file", "archive_file", "unarchive_file", "classify_file"):
        assert f'"{intent}"' in operations
    assert operations.count('permission="library:admin"') >= 6


def test_files_page_permission_styles_exist():
    css = _read(HOME_CSS)
    assert '.table-actions button[data-perm-denied="1"]' in css
    assert ".perm-note" in css


def test_modals_that_resolve_promises_always_settle_on_escape():
    """自建 `new Promise` + `KB.modal` 的弹窗必须在 onClose 上结算。

    否则公共 Esc 走 closeModal 直接移除遮罩，不触发任何 click 监听 →
    Promise 永久悬挂、调用方的互斥标志（上传的 `uploadBusy`）永远是 true，
    下一批操作被"上一批还在上传"挡住（用户读成"点了没反应"）。
    """
    common = _read(COMMON_JS)
    assistant = _read(ASSISTANT_JS)
    # 公共路径：confirmDialog / choice / modalChoice 都注册了 onClose
    for marker in ("KB.confirmDialog", "KB.choice", "KB.modalChoice"):
        assert f"{marker} = " in common or f"{marker} = function" in common, marker
    assert common.count("onClose:") >= 3, "仍有 Promise 弹窗没有 onClose 结算"
    # 自建 helper 必须只结算一次
    helper = _slice(common, "KB.modalChoice = function", "/** 库列表里**当前账号真的能写入**")
    assert "onClose: function () { settle(null); }" in helper
    assert "if (settled) return;" in helper
    # 首页上传选库改走 KB.modalChoice（不再自建悬挂 Promise）
    upload = _slice(assistant, "async function uploadFiles(files)",
                    "// ---------------------------------------------------------------- 启动 ----")
    assert "await KB.modalChoice(" in upload
    assert "new Promise(" not in upload
    assert 'uploadBusy = false;      // 取消（含 Esc）也要解锁' in upload
    # 全仓不应再有"自建 Promise + KB.modal"的悬挂写法
    assert "new Promise(function (resolve) {\n      KB.modal(" not in assistant
    for path in (ASSISTANT_JS, FILES_JS, PAGES_JS, SEARCH_JS, COMMON_JS):
        text = _read(path)
        assert "new Promise(function (resolve) {\n      KB.modal(" not in text, path.name


def test_libraries_api_really_exposes_a_role_for_plain_users(api, make_user, make_library,
                                                             grant_member):
    """前端置灰所依赖的 `my_role` 必须是**服务端真实返回**的。"""
    plain = make_user("plain-role-user")
    library = make_library(code=f"role-{uuid.uuid4().hex[:6]}")
    grant_member(library, plain, role=MemberRole.READER)
    api.force_authenticate(user=plain)
    response = api.get("/api/v1/knowledge/libraries/")
    assert response.status_code == 200, response.content[:200]
    rows = response.data.get("results") or response.data
    row = [item for item in rows if item["id"] == library.pk][0]
    assert row["my_role"] == MemberRole.READER


def test_plain_reader_really_cannot_delete_archive_or_parse(api, make_user, make_library,
                                                            grant_member, make_file):
    """置灰的根据：这些动作对只读成员**真的被服务端拒绝**（权限没有被前端改动）。

    注意提案接口把 ``PlannerError``（含权限不足）统一渲染成 400 —— 关键是
    **没有被受理**（不是 2xx），且资料状态一位都没动。
    """
    plain = make_user("plain-reader-user")
    library = make_library(code=f"read-{uuid.uuid4().hex[:6]}")
    grant_member(library, plain, role=MemberRole.READER)
    record = make_file(library, name="只读成员看不到按钮.md")
    api.force_authenticate(user=plain)

    # 解析需要"可写入成员"
    assert api.post(
        f"/api/v1/knowledge/files/{record.pk}/process/", {}, format="json"
    ).status_code == 403
    # 删除/归档/改性质走受控提案 → 提案阶段就该被拒（不会进入确认流程）
    for intent, params in (
        ("delete_file", {"file_id": record.pk}),
        ("archive_file", {"file_id": record.pk}),
        ("classify_file", {"file_id": record.pk, "classification": "合同"}),
    ):
        proposed = api.post(
            "/api/v1/planner/operations/propose/",
            {"intent": intent, "params": params, "conversation_id": None},
            format="json",
        )
        assert proposed.status_code >= 400, (intent, proposed.status_code)
        assert proposed.data.get("detail"), (intent, proposed.data)
    record.refresh_from_db()
    assert record.parse_status == ParseStatus.DONE
    assert record.classification != "合同"


def test_library_admin_can_propose_but_plain_reader_cannot(api, make_user, make_library,
                                                           grant_member, make_file):
    """同一份资料：库管理员能拿到提案，只读成员拿不到 —— 这就是置灰的判据。"""
    admin = make_user("lib-admin-user")
    reader = make_user("lib-reader-user")
    library = make_library(code=f"adm-{uuid.uuid4().hex[:6]}")
    grant_member(library, admin, role=MemberRole.ADMIN)
    grant_member(library, reader, role=MemberRole.READER)
    record = make_file(library, name="权限对照.md")

    payload = {"intent": "classify_file", "params": {"file_id": record.pk, "classification": "合同"},
               "conversation_id": None}
    api.force_authenticate(user=admin)
    admin_response = api.post("/api/v1/planner/operations/propose/", payload, format="json")
    assert admin_response.status_code == 201, admin_response.data
    api.force_authenticate(user=reader)
    reader_response = api.post("/api/v1/planner/operations/propose/", payload, format="json")
    assert reader_response.status_code >= 400, reader_response.data
    assert "管理权限" in str(reader_response.data.get("detail", ""))


# ========================================= 5) 预览失败：具体错误 ==========


def test_preview_failure_reports_the_server_detail():
    js = _read(COMMON_JS)
    body = _slice(js, "KB.previewFile = async function", "/** 原图的加载态与失败态")
    # 失败提示必须带服务端 detail（旧文案"可能没有权限"把 404/422/500 一律说成权限）
    assert "KB.errorText(result" in body
    assert "无法打开预览：" in body
    assert 'KB.toast("无法打开预览（可能没有权限）")' not in js


def test_preview_error_detail_is_really_available_from_the_api(api, make_user, make_library,
                                                               grant_member):
    """预览接口失败时确实带着可显示的 detail（前端只是把它显示出来）。"""
    plain = make_user("preview-detail-user")
    library = make_library(code=f"prev-{uuid.uuid4().hex[:6]}")
    grant_member(library, plain, role=MemberRole.READER)
    api.force_authenticate(user=plain)
    response = api.get("/api/v1/knowledge/files/999999/preview/")
    assert response.status_code == 404
    assert response.data.get("detail"), response.data


# ================================== 6) 搜索清空同步 URL ==================


def test_search_clears_and_syncs_url_conditions():
    js = _read(SEARCH_JS)
    assert "function syncSearchUrl()" in js
    assert "function clearSearchUrl()" in js
    assert "const URL_KEYS = [\"q\", \"mode\", \"department\", \"nature\", \"customer\", \"amount_min\"];" in js
    # 清空按钮：复位条件之后必须把地址栏也清干净
    reset = _slice(js, '$("search-reset").addEventListener', '$("search-prev").addEventListener')
    assert "clearSearchUrl();" in reset
    # 跑检索时把条件写回地址栏
    run = _slice(js, "async function runSearch(resetPage)", "/** 上一条检索结果的真实条数")
    assert "syncSearchUrl();" in run
    assert "clearSearchUrl();" in run          # 空条件 = 清干净
    # 翻页也要同步（否则下一页的地址栏与界面不一致）
    pager = _slice(js, '$("search-prev").addEventListener', "document.addEventListener(\"DOMContentLoaded\"")
    assert pager.count("syncSearchUrl();") >= 2


def test_search_does_not_promise_a_page_param_it_cannot_restore():
    """地址栏不写 page：hybrid 通路没有分页，写进去就是一个会撒谎的可分享参数。"""
    js = _read(SEARCH_JS)
    sync = _slice(js, "function searchQueryString()", "function syncSearchUrl()")
    assert 'params.set("page"' not in sync
    assert 'params.delete(key)' in sync


def test_search_page_really_refills_conditions_from_the_url():
    """可分享的前提：URL 上的条件真的会被读回来（这是旧实现就有的行为，别改坏）。"""
    js = _read(SEARCH_JS)
    boot = _slice(js, 'document.addEventListener("DOMContentLoaded"', "})();")
    for key in ("q", "mode", "department", "nature", "customer", "amount_min"):
        assert f'params.get("{key}")' in boot, key


# ================================== 7) 任务页分页防重复点击 ==============


def test_tasks_pagination_disables_buttons_while_loading():
    js = _read(PAGES_JS)
    assert "let tasksBusy = false;" in js
    assert "let tasksTicket = 0;" in js
    assert "let tasksTotalPages = 1;" in js
    tasks = _slice(js, "async function loadTasks(page)", "async function openTaskPreview")
    assert "setPagerBusy(true);" in tasks
    assert "setPagerBusy(false);" not in tasks       # 恢复走 finally 里的按页夹紧
    assert "if (tasksBusy || button.disabled) return;" in tasks
    assert "if (ticket !== tasksTicket) return;" in tasks


def test_tasks_pagination_never_requests_an_out_of_range_page():
    js = _read(PAGES_JS)
    tasks = _slice(js, "async function loadTasks(page)", "async function openTaskPreview")
    # 请求页先用已知总页数夹紧
    assert "Math.min(Number(page) || 1, tasksTotalPages)" in tasks
    # 页码文案与真实请求页同源（shownPage 由服务端 counts 推出，current 是真实请求页）
    assert "const shownPage = Math.min(current, pages);" in tasks
    assert '"&page=" + current' in tasks
    assert "共 ' + total + \" 条会话来源资料 · 第 \" +\n            shownPage" in tasks or "shownPage + \" / \" + pages" in tasks
    # 恢复禁用态时按真实页码判定，不能一律打开
    assert "target > tasksTotalPages" in tasks
    assert "if (!Number.isFinite(next) || next < 1 || next > tasksTotalPages) return;" in tasks


# ===================== 附：性质下拉不再承诺"清空后会自动识别" ============


def test_nature_dropdown_does_not_promise_auto_reclassification():
    """行内「性质」下拉的**可见文案**不再承诺"清空后会自动识别"。

    依据（服务端真实行为）：人工选择（含人工选空）记为 `manual`，自动识别看到
    `manual` 直接 `skipped_manual` —— 所以那个承诺是不成立的。
    `classifyFile` 里给**确认卡**写的那句"清空后…可由后端按正文自动识别"属于
    同一处口径，也一并核到（它是用户点确认时看到的原文）。
    """
    js = _read(FILES_JS)
    select_body = _slice(js, "function natureSelect(file)",
                         "// ------------------------------------------------------ 预览 / 详情 ----")
    assert "人工设置（manual）" in select_body
    assert "自动识别不会覆盖它" in select_body
    # 说明"要重新按正文识别需要系统侧的新操作（本页暂不提供）"——不凭空承诺
    assert "本页暂不提供" in select_body
    assert "性质保持为空" in select_body
    # 旧承诺（"选待识别 = 清空，可由后端按正文自动识别"）必须消失
    assert "可由后端按正文自动识别" not in select_body
    assert "留空 = 还没识别出性质；后端会按正文自动识别" not in select_body

    # 受控确认卡里的同一句口径也必须改掉（用户点确认时读的就是它）
    classify_body = _slice(js, "async function classifyFile(id, next)",
                           "/** 全部解析：")
    assert "可由后端按正文自动识别" not in classify_body
    assert "人工选空也记为人工设置" in classify_body

    # 上传弹窗的选项文案同样准确（不人工指定 ≠ 人工清空）
    upload = _slice(js, "async function uploadFiles(files)",
                    "// ------------------------------------------------------ 行内动作 ----")
    assert "不人工指定（由系统按正文自动识别）" in upload
    assert "不会被自动识别覆盖" in upload


def test_nature_manual_source_really_wins_on_the_server():
    """文案的依据：人工（含人工清空）记为 manual，自动识别看到 manual 直接跳过。"""
    services = _read(ROOT / "apps/planner/services.py")
    assert 'record.classification_source = ClassificationSource.MANUAL' in services
    assert "清空（value=\"\"）也必须置 manual" in services
    classification = _read(ROOT / "apps/knowledge/classification.py")
    assert 'if previous_source == "manual":' in classification
    assert "性质由人工设置（manual），自动识别不得覆盖" in classification


# ============ 追加批次（AF-09/10/15/16/27/28/29、OP-21/22/23） ============


def test_abandon_button_says_abandon_not_delete():
    """AF-09：待办卡上那个按钮只是"放弃"，文案不能写「删除」。"""
    js = _read(ASSISTANT_JS)
    assert 'data-op-abandon="' + "' + op.id +" in js
    assert '" title="放弃这条待办：标记为已放弃并保留操作审计，不会删除任何资料">放弃</button>' in js
    assert "不会删除任何资料\">删除</button>" not in js


def test_tools_dialog_only_shows_pending_operations():
    """待办弹窗仅处理已有待办，不再展示工具目录与清理记录。"""
    js = _read(ASSISTANT_JS)
    body = _slice(js, "async function openTools()", "/** 按 catalog")
    assert "待办操作" in body
    assert "loadPendingOps(" in body
    assert "/planner/catalog/" not in body
    assert "loadJobs(" not in body


def test_sending_shows_in_flight_feedback():
    """AF-15：等待模型期间必须有可见反馈（按钮文案 + 进行中提示），重复提交要有轻提示。"""
    js = _read(ASSISTANT_JS)
    assert "function showSendingIndicator()" in js
    assert "function clearSendingIndicator()" in js
    assert 'node.id = "kb-sending"' in js
    assert "正在生成回答" in js
    send_body = _slice(js, "async function send(text)", "function insertSendFailure(")
    assert 'sendButton.textContent = "发送中…";' in send_body
    assert "showSendingIndicator();" in send_body
    assert "clearSendingIndicator();" in send_body       # finally 里一定撤掉
    assert "上一条还在发送中" in send_body


def test_derived_document_status_is_localized_in_the_frontend():
    """AF-16：列表里显示中文状态，未知取值原样回显（不编造）。"""
    js = _read(ASSISTANT_JS)
    assert "const DOCUMENT_STATUS_TEXT = {" in js
    for value, label in (("draft", "草稿"), ("edited", "已编辑"), ("exported", "已导出")):
        assert f'{value}: "{label}"' in js, value
    assert "function documentStatusLabel(row)" in js
    assert "DOCUMENT_STATUS_TEXT[raw] || raw" in js
    assert "row.title + \" · \" + documentStatusLabel(row)" in js
    # 与后端枚举一致（前端不自己发明取值）
    models = _read(ROOT / "apps/planner/models.py")
    for value in ("draft", "edited", "exported"):
        assert f'"{value}"' in models, value


def test_new_upload_batch_resets_the_note_area():
    """AF-27：新一批上传开始时先重置提示区，不能留着上一批的失败清单。"""
    js = _read(FILES_JS)
    body = _slice(js, "async function uploadFiles(files)", "// ------------------------------------------------------ 行内动作 ----")
    reset_at = body.index("正在上传 ")
    loop_at = body.index("for (let index = 0;")
    assert reset_at < loop_at, "提示区重置必须发生在开始逐份上传之前"
    assert 'KB.notice("files-upload-note"' in body
    assert "结果会写在这里" in body


def test_detail_button_never_fails_silently():
    """AF-28：行内「详情」找不到记录时必须明确提示，不能静默 return。"""
    js = _read(FILES_JS)
    body = _slice(js, "function openDetail(id)", "// ---------------------------------------------------------- 上传 ----")
    assert "if (!file) {" in body
    assert "请刷新后重试" in body
    assert "showPageNote(" in body
    assert "if (!file) return;" not in body


def test_session_filter_exposes_aria_pressed_and_a_stated_scope():
    """AF-29：筛选开关要有 aria-pressed，并写清括号里那个数字的口径。"""
    js = _read(FILES_JS)
    assert "function updateSessionFilterState()" in js
    assert 'session.setAttribute("aria-pressed"' in js
    assert "全部**会话来源资料的总份数" in js or "全部" in js and "总份数" in js
    # 点击与从 URL 带进来两条路径都要写上状态
    assert js.count("updateSessionFilterState();") >= 3


def test_search_failure_shows_field_level_error_text():
    """OP-21：金额下限这类字段级校验必须显示**具体**原因，不能只说"请求失败"。"""
    js = _read(SEARCH_JS)
    assert "function searchErrorText(result, fallback)" in js
    assert 'typeof body.detail === "string"' in js
    assert "non_field_errors" in js
    assert 'showEmpty(searchErrorText(result, "请求失败"));' in js
    assert 'showEmpty(searchErrorText(result, "检索请求失败"));' in js
    # 不能再退回"只认 detail、否则通用文案"的写法
    assert 'showEmpty((result.body && result.body.detail) || "请求失败");' not in js


def test_amount_min_error_is_really_field_level_with_detail(api, make_user, make_library,
                                                            grant_member):
    """真实接口：非法金额下限返回 400，且**带上可显示的原因**（前端据此显示）。"""
    user = make_user("amount-user")
    library = make_library(code=f"amt-{uuid.uuid4().hex[:6]}")
    grant_member(library, user, role=MemberRole.READER)
    api.force_authenticate(user=user)
    response = api.get("/api/v1/knowledge/files/?amount_min=-1")
    assert response.status_code == 400, response.content[:200]
    detail = str(response.data.get("detail") or "")
    assert "金额" in detail or "amount_min" in str(response.data), response.data


def test_switching_mode_reruns_or_explains():
    """OP-22：有任一条件就重跑；一个条件都没有时明确说"还没生效"。"""
    js = _read(SEARCH_JS)
    body = _slice(js, '$("mode").addEventListener("change"', '$("search-reset").addEventListener')
    assert "if (hasAnyCondition()) {" in body
    assert "collectFilters();" in body
    assert "runSearch(true);" in body
    assert "会自动生效" in body
    assert "if (state.q) runSearch(true);" not in body


def test_search_mode_copy_states_model_call_and_page_limit():
    """OP-23：第一项是智能检索（会调模型、最多 10 条、不分页），文案必须说清。"""
    html = _read(ROOT / "apps/webui/templates/webui/search.html")
    assert "智能检索（文件名 + 正文，最多 10 条）" in html
    assert "调模型" in html
    assert "不分页" in html
    assert "只搜文件名（可翻页）" in html
    assert ">文件名 + 内容<" not in html
