"""派生文档删除入口（审计 D5：此前**只能改数据库**，没有任何删除途径）。

派生文档是用户自己的草稿（既不是原始字节，也不在 ``APPEND_ONLY_MODELS`` 里），
所以这里允许属主直删——但必须：

* 只认属主，越权与不存在返回同一个 404（不确认存在性）；
* **绝不触碰来源资料**（内容、归属、指纹、分段全都不变）；
* 留一条审计事件（草稿删了也要能追溯是谁删的）；
* UI 上真的有一个入口（按钮 + DELETE 调用），不是"只能靠 HTTP 手写"。
"""

from __future__ import annotations

import pytest

from apps.audit.models import AuditEvent
from apps.knowledge.models import DocumentChunk, IngestedFile
from apps.planner import services
from apps.planner.models import DerivedDocument

pytestmark = pytest.mark.django_db


def _document(user, *, title="待删草稿") -> DerivedDocument:
    return DerivedDocument.objects.create(
        created_by=user, title=title, content_markdown="# 草稿\n\n正文"
    )


def _chunked(library, make_file, name="来源.md"):
    record = make_file(library, name=name, status="done")
    DocumentChunk.objects.create(file=record, ordinal=0, text="来源正文", token_estimate=2)
    IngestedFile._base_manager.filter(pk=record.pk).update(
        chunk_count=1, embedded_chunk_count=1
    )
    record.refresh_from_db()
    return record


def test_owner_can_delete_own_derived_document(api, user, library, make_file):
    record = _chunked(library, make_file)
    document = _document(user)
    document.sources.set([record])
    before_file = IngestedFile._base_manager.filter(pk=record.pk).values().first()
    before_chunks = DocumentChunk.objects.filter(file=record).count()

    api.force_authenticate(user=user)
    response = api.delete(f"/api/v1/planner/documents/{document.pk}/")

    assert response.status_code == 200, response.content[:300]
    assert response.data["deleted"] == "derived_document"
    assert not DerivedDocument.objects.filter(pk=document.pk).exists()
    # 来源资料与分段一个字节没动
    assert IngestedFile._base_manager.filter(pk=record.pk).values().first() == before_file
    assert DocumentChunk.objects.filter(file=record).count() == before_chunks
    # 草稿删除也要留痕
    event = AuditEvent.objects.filter(action="planner.derived_document_deleted").first()
    assert event is not None, "删草稿没有留审计"
    assert event.resource_key == str(document.pk)
    assert event.payload["title"] == "待删草稿"


def test_deleting_someone_elses_document_is_404_and_keeps_it(api, user, other_user):
    document = _document(other_user, title="别人的草稿")

    api.force_authenticate(user=user)
    response = api.delete(f"/api/v1/planner/documents/{document.pk}/")

    assert response.status_code == 404, "越权删除不是 404（会泄露存在性）"
    assert DerivedDocument.objects.filter(pk=document.pk).exists(), "越权竟然删掉了"
    assert not AuditEvent.objects.filter(
        action="planner.derived_document_deleted"
    ).exists()


def test_service_raises_for_unknown_document(user):
    with pytest.raises(services.PlannerError):
        services.delete_derived_document(user, 999999)


def test_document_delete_route_is_registered():
    """路由没有被改回只读：解析 /api/v1/planner/documents/<pk>/ 必须命中带 delete 的视图。"""
    from django.urls import resolve

    match = resolve("/api/v1/planner/documents/1/")
    view = getattr(match.func, "cls", None) or match.func
    assert callable(getattr(view, "delete", None)), "该路由没有 DELETE 处理器"


def test_ui_has_a_real_delete_entry():
    """UI 层：按钮 + DELETE 调用都在，不能只有后端接口。"""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    template = (root / "apps/webui/templates/webui/assistant.html").read_text(
        encoding="utf-8"
    )
    script = (root / "apps/webui/static/webui/kb-assistant.js").read_text(
        encoding="utf-8"
    )
    assert 'id="doc-delete"' in template, "文档栏没有删除按钮"
    assert 'method: "DELETE"' in script, "删除按钮没有真的发 DELETE"
    # 删除请求打到"这一份草稿"的 URL 上（行内删除与底栏删除共用同一个函数）
    assert '"/api/v1/planner/documents/" + id + "/"' in script
    # 二次确认必须写明"来源资料不会被删"
    assert "来源资料与原始文件不会被删除" in script


def test_doc_list_has_inline_delete_entry():
    """列表里每一份草稿都要能**就地删掉**（用户实测：找不到删除入口）。

    旧版列表只有"打开"按钮，删除入口只在底栏、且只作用于"当前打开的那一份"；
    没打开任何草稿时点底栏删除只会弹"请先打开一份文档"，用户于是认为"删不掉"。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    script = (root / "apps/webui/static/webui/kb-assistant.js").read_text(
        encoding="utf-8"
    )
    css = (root / "apps/webui/static/webui/kb-home.css").read_text(encoding="utf-8")

    assert "async function deleteDocumentById(" in script, "没有按 id 删除的实现"
    assert 'remove.setAttribute("data-del-doc", String(row.id))' in script, (
        "行内删除按钮没带上要删的草稿 id"
    )
    assert "deleteDocumentById(row.id, row.title)" in script, "行内 ✕ 没接删除逻辑"
    # 底栏按钮也必须复用同一条路径，不能各写一套（两处行为必须一致）
    assert "await deleteDocumentById(currentDocId, title)" in script
    # 行内按钮要有样式并且能落进列表行
    assert ".doc-pane .doc-list-row" in css
    assert ".doc-pane .doc-del" in css


def test_doc_pane_opens_latest_document_automatically():
    """打开文档栏时若还没打开任何草稿，就自动选中最新一份。

    否则正文区一直是占位文字「打开一份文档后可在此编辑」，底栏的删除/保存/导出
    在用户眼里全是"点了没反应"。自动打开**不能覆盖用户正在输入的内容**。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    script = (root / "apps/webui/static/webui/kb-assistant.js").read_text(
        encoding="utf-8"
    )
    assert "async function refreshDocPane(" in script
    # 自动打开显式 skipGuard：走到这里已经确认过"编辑器是空的且没有未保存改动"，
    # 所以不该再弹一次"丢弃改动"的确认（那会在打开面板时吓人一跳）。
    assert "await openDocument(rows[0].id, { skipGuard: true })" in script, "没有自动打开最新一份"
    assert "if (!currentDocId && !editorHasInput() && !isDocDirty() && rows.length)" in script, (
        "自动打开没有加'用户没在输入、也没有未保存改动'的前置条件"
    )
    assert "refreshDocPane();" in script, "面板打开时没走 refreshDocPane"
    # 自动打开不能顶掉"操作执行完显式打开刚生成的那份"——用序号票互斥
    assert "docOpenTicket" in script, "自动打开与显式打开之间没有互斥"
    # listDocuments 必须把列表返回给调用方（自动打开要用它）
    assert "return rows;" in script
