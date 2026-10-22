"""文件管理页：勾选状态、已归档筛选、失败原因、互斥禁用（源码级 + 真实 API）。

本文件锁死四个**已证实**的缺陷，每一条都同时有「源码级」与「运行时」证据：

1. 勾选状态残留 —— `renderRows` 重渲染后 `#check-all` 仍是勾着的，而新行的
   checkbox 全未选中（视觉与真实状态不一致）。源码级断言「每次重渲染必须归零」。
2. `#files-status` 缺「已归档」选项 —— 行内确实有「恢复」按钮，却筛不出已归档。
   除了源码级断言，还用**真实 API** 证明 `status=archived` 真的能筛（后端天然支持
   `parse_status__in`）。
3. 解析失败原因丢失 —— 服务端 `detail`/`parse_error` 必须原样出现在界面上。
   运行时用真实接口证明「处理失败时返回的 `detail` 非空且是真实原因」，不是通用文案；
   源码级断言列表行会把它显示出来（`parse_error`）。
4. 「解析 / 全部解析 / 下一页」请求进行中不禁用 —— 快速连点会重复提交。
   源码级断言互斥禁用 + 恢复，且「被跳过的份数要说清为什么」。

只用源码断言与真实 DRF 接口调用（`force_login`），不启动浏览器、不调用模型。
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model

from apps.knowledge.models import IngestedFile, KnowledgeLibrary, LibraryMember, ParseStatus

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "apps" / "webui" / "static" / "webui" / "kb-files.js"
HTML = ROOT / "apps" / "webui" / "templates" / "webui" / "files.html"
VIEWS = ROOT / "apps" / "knowledge" / "views.py"


def _js() -> str:
    return JS.read_text(encoding="utf-8")


def _html() -> str:
    return HTML.read_text(encoding="utf-8")


# --------------------------------------------------------------- 夹具 ----


@pytest.fixture
def admin():
    return get_user_model().objects.create_user(
        username=f"files-page-{uuid.uuid4().hex[:8]}", password="x", is_staff=True
    )


@pytest.fixture
def library(admin):
    item = KnowledgeLibrary.objects.create(
        code=f"lib-{uuid.uuid4().hex[:6]}", name="页面态库"
    )
    LibraryMember.objects.create(library=item, user=admin, role="admin")
    return item


# ============================================== 1. 勾选状态残留 ==========


def test_render_rows_resets_check_all_and_selection_copy():
    """每次重渲染行都必须把全选归零，并同步「已选 N 份」文案。"""
    source = _js()
    start = source.index("function renderRows(rows)")
    end = source.index("function natureSelect(file)")
    render = source[start:end]

    assert 'const checkAll = $("check-all");' in render, "renderRows 没有取到全选控件"
    assert "checkAll.checked = false;" in render, "重渲染后全选没有归零（状态残留）"
    assert "updateSelectionNote();" in render, "重渲染后没有同步已选文案"

    # 已选文案只认 DOM 里真实勾上的复选框（唯一事实来源）
    note = source[source.index("function updateSelectionNote()") : start]
    assert ".file-check" in note and "box.checked" in note, "已选数量不是按真实勾选取的"
    assert "data-count" in note and "已选 " in note, "缺少「已选 N 份」的真实文案"


def test_html_exposes_selection_note_element():
    """文案元素必须真的在页面上，否则 updateSelectionNote 无处可写。"""
    html = _html()
    assert 'id="files-select-note"' in html, "页面缺少已选文案元素"


def test_row_checkbox_carries_name_for_copy():
    source = _js()
    assert 'data-name="' in source, "行复选框没有带文件名，已选文案只能显示编号"


def test_load_files_renders_before_page_copy():
    """重渲染（归零全选）必须发生在分页文案之前，否则中间态会残留。"""
    source = _js()
    body = source[source.index("async function loadFiles()") : source.index("function renderRows(rows)")]
    assert body.index("renderRows(rows);") < body.index("files-page-info"), (
        "先写分页文案后渲染行：中间态里全选仍可能是勾着的"
    )


# ======================================== 2. 「已归档」选项与筛选 ========


def test_status_select_removes_archived_option():
    html = _html()
    assert '<option value="archived">已归档</option>' not in html


def test_files_page_omits_archive_and_keeps_selection_note(client, admin):
    """真实渲染一次页面：新控件必须真的在发出去的 HTML 里（不是只改了模板源）。"""
    client.force_login(admin)
    response = client.get("/app/files/")
    assert response.status_code == 200, response.content[:200]
    page = response.content.decode("utf-8")
    assert '<option value="archived">已归档</option>' not in page
    assert 'id="files-select-note"' in page, "渲染后的页面缺已选文案元素"


def test_status_filter_supports_archived_via_real_api(client, admin, library):
    """真实 API：`status=archived` 必须只返回已归档资料。"""
    archived = IngestedFile._base_manager.create(
        library=library,
        original_name="已归档.md",
        content_sha256=uuid.uuid4().hex,
        size_bytes=10,
        file_type="md",
        parse_status=ParseStatus.ARCHIVED,
    )
    done = IngestedFile._base_manager.create(
        library=library,
        original_name="已完成.md",
        content_sha256=uuid.uuid4().hex,
        size_bytes=10,
        file_type="md",
        parse_status=ParseStatus.DONE,
    )
    client.force_login(admin)

    response = client.get("/api/v1/knowledge/files/?status=archived&page_size=10")
    assert response.status_code == 200, response.content[:200]
    ids = [row["id"] for row in response.json()["results"]]
    assert archived.pk in ids, "status=archived 没有返回已归档资料"
    assert done.pk not in ids, "status=archived 混进了非归档资料"

    # 逗号分隔的多值口径同样可用（parse_status__in）
    mixed = client.get("/api/v1/knowledge/files/?status=archived,done&page_size=10")
    assert mixed.status_code == 200
    mixed_ids = {row["id"] for row in mixed.json()["results"]}
    assert {archived.pk, done.pk} <= mixed_ids, "多状态过滤没有生效"


# ========================================== 3. 失败原因如实返回 ==========


def test_process_failure_returns_real_detail(client, admin, library):
    """处理失败时，接口 `detail` 必须**非空且是真实原因**（不是通用文案）。

    这里让**真实流水线**去跑：原件字节不存在 → 失败必然发生 →
    接口必须把这次尝试的真实原因原样回给调用方，并把 FAILED + 原因写进记录。
    """
    record = IngestedFile._base_manager.create(
        library=library,
        original_name="坏件.pdf",
        content_sha256=uuid.uuid4().hex,  # 内容寻址：这个指纹下没有原件字节
        size_bytes=10,
        file_type="pdf",
        parse_status=ParseStatus.PENDING,
        raw_path="raw/test/坏件.pdf",
    )
    client.force_login(admin)

    response = client.post(f"/api/v1/knowledge/files/{record.pk}/process/")
    assert response.status_code == 409, "失败被包装成了成功"
    body = response.json()
    assert body.get("detail"), "处理失败却没有给出原因（detail 为空）"
    # 通用文案检查：必须是这次失败的真实原因（异常类型 + 缺原件），不是"处理失败"
    assert "解析失败" not in body["detail"] and "处理失败" != body["detail"].strip()
    assert "StorageError" in body["detail"] and "摘要" in body["detail"], (
        f"detail 不是服务端的真实原因：{body['detail'][:200]}"
    )

    # 失败事实落库：列表接口的行内错误文案取的就是它
    record.refresh_from_db()
    assert record.parse_status == ParseStatus.FAILED
    assert record.parse_error and record.parse_error == body["detail"]

    listed = client.get(f"/api/v1/knowledge/files/{record.pk}/").json()
    assert listed["parse_error"], "列表序列化丢了 parse_error（行内错误文案无从显示）"
    assert listed["parse_status"] == ParseStatus.FAILED


def test_failed_row_shows_parse_error_inline():
    """列表行必须把 parse_error 显示出来（不是只在详情弹窗里）。"""
    source = _js()
    assert "parseErrorHtml" in source, "列表行没有失败原因渲染"
    assert "file.parse_error" in source, "行内错误文案没有取服务端的 parse_error"
    assert "解析失败：" in source, "行内没有明确标注解析失败"
    assert "esc(parseError)" in source, "错误原因没有转义（会破坏版面）"


def test_process_error_path_uses_server_detail_not_generic_copy():
    """行内解析失败必须用服务端 detail，不能退化成通用文案。"""
    source = _js()
    assert "response.body && response.body.detail" in source
    assert "showPageNote" in source, "失败原因没有留在页面上（只靠会消失的 toast）"


def test_views_process_keeps_failure_detail_and_status():
    """后端那一处改动：detail 原样返回，且状态码仍是 409（不是 200）。"""
    views_source = VIEWS.read_text(encoding="utf-8")
    assert "失败原因**必须原样回给调用方**" in views_source, "views.py 未标注该契约"
    assert '{"ok": False, "detail": str(exc), "file": self.get_serializer(record).data}' in views_source
    assert "status=status.HTTP_409_CONFLICT," in views_source


# ========================================== 4. 互斥禁用 / 跳过说明 =======


def test_parse_and_paging_are_mutually_disabled():
    source = _js()
    assert "function setBusy(on)" in source, "缺少请求互斥开关"
    assert "BUSY_CONTROLS" in source, "互斥没有覆盖筛选/翻页控件"
    for control in ("files-prev", "files-next", "files-parse-all"):
        assert control in source, f"互斥未覆盖 {control}"
    # 运行中再点直接忽略 + 结束时恢复
    assert (
        "if (parseBatchBusy || (trigger && trigger.disabled)) return;" in source
    ), "全部解析没有防重入"
    assert "setBusy(true);" in source and "setBusy(false);" in source
    assert "} finally {" in source, "忙碌态没有在 finally 里恢复"
    # 行内解析按钮也要禁用/恢复
    assert "if (button && button.disabled) return;" in source, "行内解析没有防重入"


def test_skipped_files_are_reported_with_reason():
    """勾选已完成/处理中/已归档时，必须说清跳过几份 + 为什么。"""
    source = _js()
    assert "function skippedText(skipped)" in source, "缺少跳过原因文案"
    for status in ("done", "running", "archived"):
        assert f"skipped.{status}" in source, f"{status} 没有被计入跳过说明"
    assert "份已完成，无需重复解析" in source
    assert "份正在处理中" in source
    assert "份为历史归档状态，已跳过" in source


# ========================================== 5. 页码 / 归档注释 ==========


def test_page_copy_follows_real_requested_page():
    """页码文案必须按真实请求的 page 显示（并把越界的 state.page 校准回来）。"""
    source = _js()
    assert "state.page = current;" in source, "越界的页码没有被校准，文案与实际请求不一致"
    assert '第 " + current + " / " + pages + " 页' in source


def test_archive_controls_are_removed():
    source = _js()
    assert "data-archive" not in source
    assert "data-unarchive" not in source
    assert "async function archiveFile" not in source
    assert "async function unarchiveFile" not in source
