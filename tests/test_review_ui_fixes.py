"""暂停前那批网页/接口修复的回归证据（审查要求：每项都要有测试，不靠手测）。

对应用户实测反馈的 4 个问题：

1. 文件行"重新处理"发 **JSON**，但视图只声明 multipart/form → 实际 HTTP 415
   （"不支持 application/json"）。这里用**真实 JSON + session/CSRF** 打。
2. 解析成功但向量化不可用（模型离线）时，``chunk_count`` 必须是真实分段数，
   不能是 0——否则界面与统计都在骗人。
3. 上传结果要能**区分"已保存"与"处理完成"**（同一行可能已保存但处理失败）。
4. 检索的信息库控件是**单选下拉**且带"全部信息库"选项（不是多选列表）。
"""

from __future__ import annotations


import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.urls import reverse

from apps.knowledge.models import (
    DocumentChunk,
    IngestedFile,
    KnowledgeLibrary,
    ParseStatus,
)

pytestmark = pytest.mark.django_db


# ------------------------------------------- 1) JSON 重新处理不再 415 ----


@override_settings(KB_UPLOAD_PROCESS_MODE="inline", KB_TEST_ADAPTER="offline")
def test_process_action_accepts_json(api, admin_user, library, isolated_paths):
    """`process` 动作必须接受 JSON（旧实现只声明 multipart → 415）。"""
    api.force_authenticate(user=admin_user)
    upload = api.post(
        "/api/v1/knowledge/files/upload/",
        {
            "library": library.code,
            "file": SimpleUploadedFile("json-retry.txt", "质保期 12 个月".encode()),
        },
        format="multipart",
    )
    assert upload.status_code in {200, 201, 202}, upload.content[:300]
    record = IngestedFile.objects.get(pk=upload.json()["file"]["id"])
    # 让它可以被"重新处理"
    IngestedFile._base_manager.filter(pk=record.pk).update(
        parse_status=ParseStatus.FAILED, parse_attempts=1, parse_error="模拟失败"
    )

    response = api.post(
        f"/api/v1/knowledge/files/{record.pk}/process/",
        {"force": "1"},
        format="json",  # ← 关键：JSON 而不是 multipart
    )
    assert response.status_code != 415, response.content[:300]
    assert response.status_code in {200, 202, 409}, response.content[:300]


def test_process_action_accepts_multipart_too(api, admin_user, library, isolated_paths):
    """兼容旧调用方：multipart 也不能被拒（415 的另一种形态）。"""
    api.force_authenticate(user=admin_user)
    upload = api.post(
        "/api/v1/knowledge/files/upload/",
        {
            "library": library.code,
            "file": SimpleUploadedFile("mp-retry.txt", b"multipart retry body"),
        },
        format="multipart",
    )
    record = IngestedFile.objects.get(pk=upload.json()["file"]["id"])
    IngestedFile._base_manager.filter(pk=record.pk).update(
        parse_status=ParseStatus.FAILED, parse_attempts=1
    )
    response = api.post(
        f"/api/v1/knowledge/files/{record.pk}/process/",
        {"force": "1"},
        format="multipart",
    )
    assert response.status_code != 415


def test_process_action_via_session_and_csrf(admin_user, library, isolated_paths):
    """真实浏览器路径：session 登录 + CSRF 头 + JSON body。"""
    client = Client(enforce_csrf_checks=True)
    assert client.login(username="admin", password="pw-not-a-secret-123") is True
    # 先取 CSRF cookie（GET 一次页面即可）
    page = client.get(reverse("webui:files"))
    assert page.status_code == 200
    record = IngestedFile.objects.create(
        library=KnowledgeLibrary.objects.get(pk=library.pk),
        original_name="csrf.txt",
        content_sha256="a" * 64,
        size_bytes=3,
        raw_size_bytes=3,
        parse_status=ParseStatus.FAILED,
        parse_attempts=1,
    )

    response = client.post(
        f"/api/v1/knowledge/files/{record.pk}/process/",
        data='{"force": "1"}',
        content_type="application/json",
        HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
    )
    # 关键：不是 403（CSRF 通过）、不是 415（JSON 被接受）
    assert response.status_code not in {403, 415}, response.content[:300]


# --------------------------- 2) 解析成功但向量不可用时 chunk_count 如实 ----


@override_settings(
    KB_UPLOAD_PROCESS_MODE="inline",
    KB_MODEL_CALLS_DISABLED=True,
    KB_TEST_ADAPTER="",
)
def test_chunk_count_recorded_when_vectorization_unavailable(
    api, admin_user, library, offline_paths
):
    """离线（模型不可用）时：解析成功就要如实写下分段数，向量状态标不可用。"""
    api.force_authenticate(user=admin_user)
    response = api.post(
        "/api/v1/knowledge/files/upload/",
        {
            "library": library.code,
            "file": SimpleUploadedFile(
                "offline-chunks.txt", "第一段内容。\n第二段内容。\n".encode()
            ),
        },
        format="multipart",
    )
    assert response.status_code in {200, 201, 202}, response.content[:300]
    record = IngestedFile.objects.get(pk=response.json()["file"]["id"])

    real_chunks = DocumentChunk.objects.filter(file=record).count()
    assert real_chunks > 0, "解析应当已经写出分段"
    record.refresh_from_db()
    # **核心断言**：分段数不能是 0（旧缺陷：解析成功却显示 0）
    assert record.chunk_count == real_chunks
    # 向量那一路如实标不可用，而不是假装成功
    assert record.vector_status != "indexed"
    assert record.embedded_chunk_count == 0


# ------------------------------- 3) 上传结果区分"已保存/处理完成" ----


@override_settings(
    KB_UPLOAD_PROCESS_MODE="inline", KB_MODEL_CALLS_DISABLED=True, KB_TEST_ADAPTER=""
)
def test_upload_response_separates_saved_from_processed(
    api, admin_user, library, offline_paths
):
    """上传响应必须能区分"已保存"与"处理完成"（界面据此给出正确措辞）。"""
    api.force_authenticate(user=admin_user)
    response = api.post(
        "/api/v1/knowledge/files/upload/",
        {
            "library": library.code,
            "file": SimpleUploadedFile("saved-not-processed.txt", b"body for offline"),
        },
        format="multipart",
    )
    payload = response.json()
    # 已保存：有落盘信息 + 记录
    assert payload["stored"]["sha256"]
    assert payload["stored"]["read_only"] is True
    # 处理：明确失败（不是含糊的"完成"）
    processing = payload["processing"]
    assert processing["attempted"] is True
    assert processing["ok"] is False
    assert processing["detail"], "失败必须带原因（界面据此显示可操作提示）"
    record = IngestedFile.objects.get(pk=payload["file"]["id"])
    assert record.raw_path, "原件必须已安全落盘"


@override_settings(KB_UPLOAD_PROCESS_MODE="queue", KB_TEST_ADAPTER="offline")
def test_upload_response_marks_queued_as_not_done(api, admin_user, library, isolated_paths):
    """队列模式：响应必须标 queued 且 ok 不为真（界面显示"待处理"）。"""
    api.force_authenticate(user=admin_user)
    response = api.post(
        "/api/v1/knowledge/files/upload/",
        {
            "library": library.code,
            "file": SimpleUploadedFile("queued-state.txt", b"queued body"),
        },
        format="multipart",
    )
    payload = response.json()
    processing = payload["processing"]
    assert processing["queued"] is True
    assert processing["ok"] is False
    assert response.status_code == 202


# --------------------------------- 4) 检索用单选下拉 + 全部信息库 ----


def test_search_page_has_no_library_control_per_user_override():
    """需求覆盖：搜索页**不再显示**信息库下拉（"除首页右文档栏外不能加控件"）。

    后台 libraries 过滤接口仍保留，URL 条件继续支持。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    template = (root / "apps/webui/templates/webui/search.html").read_text(encoding="utf-8")
    assert 'id="libraries"' not in template, "搜索页又出现了信息库下拉"

    script = (root / "apps/webui/static/webui/kb-search.js").read_text(encoding="utf-8")
    # URL 条件仍要支持（不能被悄悄丢掉）
    assert 'params.get("customer")' in script, "customer URL 条件丢了"
    # 且必须只提交条件、不枚举候选
    assert "payload.file_ids" not in script, "又在客户端枚举候选了"


def test_search_page_keeps_three_prototype_modes():
    """严格原型三模式：文件名 + 内容 / 只搜文件名 / 只搜正文。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    template = (root / "apps/webui/templates/webui/search.html").read_text(encoding="utf-8")
    assert template.count("<option value=") >= 3
    assert 'value="semantic"' not in template, "第 4 个模式应已撤掉"

def test_libraries_api_remains_available_even_though_ui_hides_it():
    """界面撤掉下拉，但 libraries 接口必须保留（URL 条件与其它页面仍用它）。"""
    from django.urls import get_resolver

    patterns = []

    def walk(items):
        for item in items:
            if hasattr(item, "url_patterns"):
                walk(item.url_patterns)
            else:
                patterns.append(str(getattr(item, "pattern", "")))

    walk(get_resolver().url_patterns)
    assert any("libraries" in pattern for pattern in patterns), (
        "libraries 接口被一起删掉了；需求只要求界面不显示，接口必须保留"
    )
