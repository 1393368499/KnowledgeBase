"""`export_document` 必须是**真实导出**（审查第 9 条）。

旧行为：忽略 `format`、只把状态改成 EXPORTED，调用方拿不到任何可下载产物 ——
"看起来导出了"。现在的口径：
  * 登记的 `format` 必须生效，返回**受权下载地址**与对应 content-type；
  * 不支持的格式、内容为空的文档 → 明确报错（**不翻状态冒充成功**）；
  * 下载地址必须**真的能取到字节**，且格式与声明一致。
"""

from __future__ import annotations

import pytest


def _armed(user, params):
    from django.utils import timezone

    from apps.planner.models import Conversation, OperationRecord
    from apps.planner.operations import get as get_spec
    from apps.planner.services import _describe_target

    ref, label = _describe_target(get_spec("export_document"), params)
    conversation = Conversation.objects.create(user=user)
    return OperationRecord.objects.create(
        user=user,
        conversation=conversation,
        intent="export_document",
        params=params,
        target_ref=ref,
        target_label=label,
        status=OperationRecord.Status.ARMED,
        armed_at=timezone.now(),
        armed_expires_at=timezone.now() + timezone.timedelta(minutes=10),
    )


@pytest.fixture
def document(db, user):
    from apps.planner.models import DerivedDocument

    return DerivedDocument.objects.create(
        created_by=user,
        title="导出用例",
        content_markdown="# 标题\n\n正文内容。\n",
    )


def _export(user, document, **params):
    from apps.planner import services

    payload = {"document_id": document.pk}
    payload.update(params)
    return services.execute_operation(user, _armed(user, payload))["result"]


def test_md_export_returns_real_download_url(db, user, document):
    payload = _export(user, document, format="md")
    assert payload["format"] == "md"
    assert payload["content_type"].startswith("text/markdown")
    assert "export_format=md" in payload["download_url"]


def test_html_export_is_honored(db, user, document):
    payload = _export(user, document, format="html")
    assert payload["format"] == "html"
    assert payload["content_type"].startswith("text/html")


def test_docx_export_is_honored(db, user, document):
    """DOCX 现在是**支持**的格式（用户第 5 项：真实 DOCX/PDF 导出）。"""
    payload = _export(user, document, format="docx")
    assert payload["format"] == "docx"
    assert payload["content_type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert payload["download_url"].endswith("export_format=docx")
    assert payload["filename"].endswith(".docx")


def test_unsupported_format_is_rejected(db, user, document):
    """**真的**不支持的格式仍要明确拒绝（不是"常见格式一律放行"）。"""
    from apps.planner.services import PlannerError

    with pytest.raises(PlannerError) as exc:
        _export(user, document, format="rtf")
    assert "不支持" in str(exc.value)


def test_empty_document_is_rejected_without_faking_status(db, user):
    from apps.planner.models import DerivedDocument
    from apps.planner.services import PlannerError

    empty = DerivedDocument.objects.create(
        created_by=user, title="空文档", content_markdown="   "
    )
    with pytest.raises(PlannerError) as exc:
        _export(user, empty)
    assert "为空" in str(exc.value)
    empty.refresh_from_db()
    assert empty.status != DerivedDocument.Status.EXPORTED, "空文档不得被标成已导出"


def test_download_url_actually_serves_bytes(auth_api, user, document):
    """下载地址必须**真的**返回对应格式的字节（不是空响应）。"""
    payload = _export(user, document, format="md")
    client = auth_api(user)
    response = client.get(payload["download_url"])
    assert response.status_code == 200, response.content[:200]
    body = response.content.decode("utf-8", errors="replace")
    assert "正文内容" in body
    assert response["X-Document-Format"] == "md"
