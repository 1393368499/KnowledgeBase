"""基础 API 测试：库筛选、统计、上传追加、只读边界。

这些用例覆盖"AI 权限最大但不能改已入库信息"的 API 侧证据：
- 列表/详情可读，且按可读范围过滤；
- 任何 PUT/PATCH/DELETE 在进入视图之前就被中间件拒绝（并留审计事件）；
- 上传只能"追加"，重复内容幂等。
"""

from __future__ import annotations

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.audit.models import AuditEvent
from apps.knowledge.models import DocumentChunk, IngestedFile, LibraryLevel, MemberRole, ParseStatus
from apps.knowledge.services import add_chunks

pytestmark = pytest.mark.django_db


# ------------------------------------------------------------------ 认证边界 ----
def test_files_require_authentication(api):
    response = api.get("/api/v1/knowledge/files/")
    assert response.status_code in {401, 403}


def test_stats_require_authentication(api):
    response = api.get("/api/v1/knowledge/stats/")
    assert response.status_code in {401, 403}


def test_libraries_require_authentication(api):
    response = api.get("/api/v1/knowledge/libraries/")
    assert response.status_code in {401, 403}


def test_health_is_open(api):
    assert api.get("/api/v1/health/live/").status_code == 200
    ready = api.get("/api/v1/health/ready/")
    assert ready.status_code == 200
    assert ready.json()["checks"]["vector_data"] == "ok"
    # 迁移也要如实检查：只 SELECT 1 的话空库也是 200（假绿灯）
    assert ready.json()["checks"]["migrations"] == "ok"


# ------------------------------------------------------------------ 库可见性 ----
def test_internal_library_hidden_without_membership(auth_api, user, library):
    response = auth_api(user).get("/api/v1/knowledge/libraries/")
    assert response.status_code == 200
    assert response.json()["count"] == 0


def test_member_sees_internal_library(auth_api, user, library, grant_member):
    grant_member(library, user, MemberRole.READER)
    body = auth_api(user).get("/api/v1/knowledge/libraries/").json()
    assert body["count"] == 1
    assert body["results"][0]["code"] == "product"
    assert body["results"][0]["my_role"] == MemberRole.READER


def test_external_library_visible_to_any_authenticated_user(auth_api, user, make_library):
    make_library(code="public", name="外部资料库", level=LibraryLevel.EXTERNAL)
    body = auth_api(user).get("/api/v1/knowledge/libraries/").json()
    assert body["count"] == 1
    assert body["results"][0]["level"] == LibraryLevel.EXTERNAL


def test_system_admin_sees_everything(auth_api, admin_user, library):
    body = auth_api(admin_user).get("/api/v1/knowledge/libraries/").json()
    assert body["count"] == 1
    assert body["results"][0]["my_role"] == MemberRole.ADMIN


def test_inactive_library_hidden_by_default(auth_api, admin_user, make_library):
    make_library(code="off", name="停用库", active=False)
    assert auth_api(admin_user).get("/api/v1/knowledge/libraries/").json()["count"] == 0
    assert (
        auth_api(admin_user).get("/api/v1/knowledge/libraries/?active=0").json()["count"] == 1
    )


# ------------------------------------------------------------------ 资料列表 ----
def test_files_are_filtered_to_readable_libraries(
    auth_api, user, library, make_library, make_file, grant_member
):
    other = make_library(code="other", name="别的库")
    make_file(library, name="可见.md")
    make_file(other, name="不可见.md")
    grant_member(library, user, MemberRole.READER)

    body = auth_api(user).get("/api/v1/knowledge/files/").json()
    names = [row["original_name"] for row in body["results"]]
    assert names == ["可见.md"]


def test_file_list_filters(auth_api, admin_user, library, make_file):
    make_file(library, name="已完成.md", status=ParseStatus.DONE)
    make_file(library, name="失败.md", status=ParseStatus.FAILED)
    make_file(library, name="会话来.md", source="session")

    client = auth_api(admin_user)
    assert client.get("/api/v1/knowledge/files/?status=failed").json()["count"] == 1
    assert client.get("/api/v1/knowledge/files/?q=已完成").json()["count"] == 1
    assert client.get(f"/api/v1/knowledge/files/?library={library.pk}").json()["count"] == 3
    assert client.get("/api/v1/knowledge/files/?library=product").json()["count"] == 3
    assert client.get("/api/v1/knowledge/files/?session=1").json()["count"] == 1


def test_file_detail_and_chunks(auth_api, admin_user, library, make_file):
    record = make_file(library)
    add_chunks(record, ["第一段", "第二段"])

    client = auth_api(admin_user)
    detail = client.get(f"/api/v1/knowledge/files/{record.pk}/")
    assert detail.status_code == 200
    assert detail.json()["original_name"] == record.original_name

    chunks = client.get(f"/api/v1/knowledge/files/{record.pk}/chunks/")
    assert chunks.json()["count"] == 2
    assert [row["ordinal"] for row in chunks.json()["results"]] == [0, 1]


def test_file_detail_is_hidden_for_other_users(auth_api, other_user, library, make_file):
    record = make_file(library)
    assert auth_api(other_user).get(f"/api/v1/knowledge/files/{record.pk}/").status_code == 404


def test_stats_counts(auth_api, admin_user, library, make_file):
    done = make_file(library, name="a.md", status=ParseStatus.DONE)
    make_file(library, name="b.md", status=ParseStatus.PENDING)
    make_file(library, name="c.md", status=ParseStatus.FAILED)
    add_chunks(done, ["x", "y"])

    body = auth_api(admin_user).get("/api/v1/knowledge/stats/").json()
    assert body == {
        "libraries": 1,
        "files": 3,
        "done": 1,
        # 处理中：首页"待处理"标签与统计需要它（此前只有 pending，语义不完整）
        "processing": 0,
        "pending": 1,
        "failed": 1,
        # 会话来源**精确**等于 source="session"，不再用 exclude(source="manual")
        "session_files": 0,
        "chunks": 2,
    }


def test_file_stats_action(auth_api, admin_user, library, make_file):
    make_file(library, status=ParseStatus.DONE)
    make_file(library, status=ParseStatus.ARCHIVED)
    body = auth_api(admin_user).get("/api/v1/knowledge/files/stats/").json()
    assert body["total"] == 2
    assert body["by_status"]["done"] == 1
    assert body["by_status"]["archived"] == 1
    assert body["by_status"]["pending"] == 0


# -------------------------------------------------------------- 中间件强制拦截 ----
@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_write_methods_blocked_before_view(auth_api, admin_user, library, make_file, method):
    record = make_file(library)
    client = auth_api(admin_user)
    response = getattr(client, method)(
        f"/api/v1/knowledge/files/{record.pk}/", {}, format="json"
    )
    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "immutable_resource"
    assert body["resource"] == "已入库资料"
    assert body["method"] == method.upper()
    # 拦截动作必须留痕
    assert AuditEvent.objects.filter(action="immutable.write_blocked").count() == 1
    assert IngestedFile.objects.filter(pk=record.pk).exists()


def test_audit_events_collection_is_blocked_too(auth_api, admin_user):
    response = auth_api(admin_user).delete("/api/v1/audit/events/")
    assert response.status_code == 403
    assert response.json()["resource"] == "审计事件"


def test_blocked_write_is_recorded_even_for_superuser(auth_api, admin_user, library, make_file):
    record = make_file(library)
    response = auth_api(admin_user).patch(
        f"/api/v1/knowledge/files/{record.pk}/", {"original_name": "改名"}, format="json"
    )
    assert response.status_code == 403
    record.refresh_from_db()
    assert record.original_name != "改名"


# -------------------------------------------------------------------- 上传 ----
def _upload(name: str = "新资料.md", content: bytes = b"hello world") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, content, content_type="text/markdown")


def test_upload_appends_file_and_writes_audit(auth_api, admin_user, library):
    response = auth_api(admin_user).post(
        "/api/v1/knowledge/files/upload/",
        {"file": _upload(), "library": str(library.pk)},
        format="multipart",
    )
    # 队列模式（默认 auto）下上传统一返回 202 已受理；inline 模式才是 201。
    assert response.status_code in {201, 202}
    body = response.json()
    assert body["created"] is True
    assert body["file"]["original_name"] == "新资料.md"
    assert IngestedFile.objects.count() == 1

    event = AuditEvent.objects.get(action="file.ingested")
    assert event.payload["original_name"] == "新资料.md"
    assert event.payload["size_bytes"] == 11
    assert len(event.payload["sha256"]) == 64


def test_upload_is_idempotent_for_same_bytes(auth_api, admin_user, library):
    client = auth_api(admin_user)
    first = client.post(
        "/api/v1/knowledge/files/upload/",
        {"file": _upload(), "library": str(library.pk)},
        format="multipart",
    )
    second = client.post(
        "/api/v1/knowledge/files/upload/",
        {"file": _upload("换个名字.md"), "library": str(library.pk)},
        format="multipart",
    )
    assert first.status_code in {201, 202}
    assert second.status_code == 200
    assert second.json()["created"] is False
    assert second.json()["reason"] == "duplicate_content"
    assert IngestedFile.objects.count() == 1


def test_upload_accepts_library_code(auth_api, admin_user, library):
    response = auth_api(admin_user).post(
        "/api/v1/knowledge/files/upload/",
        {"file": _upload(), "library": library.code},
        format="multipart",
    )
    assert response.status_code in {201, 202}


def test_upload_requires_library(auth_api, admin_user, library):
    response = auth_api(admin_user).post(
        "/api/v1/knowledge/files/upload/", {"file": _upload()}, format="multipart"
    )
    assert response.status_code == 400
    assert "library" in response.json()


def test_upload_rejects_unknown_library(auth_api, admin_user, library):
    response = auth_api(admin_user).post(
        "/api/v1/knowledge/files/upload/",
        {"file": _upload(), "library": "99999"},
        format="multipart",
    )
    assert response.status_code == 404


def test_upload_rejects_unsupported_extension(auth_api, admin_user, library):
    response = auth_api(admin_user).post(
        "/api/v1/knowledge/files/upload/",
        {"file": SimpleUploadedFile("木马.exe", b"MZ", content_type="application/octet-stream"),
         "library": str(library.pk)},
        format="multipart",
    )
    assert response.status_code == 400
    assert "不支持的文件类型" in str(response.json())


def test_upload_rejects_empty_file(auth_api, admin_user, library):
    response = auth_api(admin_user).post(
        "/api/v1/knowledge/files/upload/",
        {"file": SimpleUploadedFile("空.md", b"", content_type="text/markdown"),
         "library": str(library.pk)},
        format="multipart",
    )
    assert response.status_code == 400


def test_upload_requires_writer_role(auth_api, user, library, grant_member):
    grant_member(library, user, MemberRole.READER)
    response = auth_api(user).post(
        "/api/v1/knowledge/files/upload/",
        {"file": _upload(), "library": str(library.pk)},
        format="multipart",
    )
    assert response.status_code == 403
    assert IngestedFile.objects.count() == 0


def test_upload_allowed_for_writer_role(auth_api, user, library, grant_member):
    grant_member(library, user, MemberRole.WRITER)
    response = auth_api(user).post(
        "/api/v1/knowledge/files/upload/",
        {"file": _upload(), "library": str(library.pk)},
        format="multipart",
    )
    assert response.status_code in {201, 202}


def test_upload_rejected_on_inactive_library(auth_api, admin_user, make_library):
    inactive = make_library(code="off", name="停用库", active=False)
    response = auth_api(admin_user).post(
        "/api/v1/knowledge/files/upload/",
        {"file": _upload(), "library": str(inactive.pk)},
        format="multipart",
    )
    assert response.status_code == 400
    assert IngestedFile.objects.count() == 0


def test_upload_metadata_is_persisted(auth_api, admin_user, library):
    # 性质取值现在**全链路收敛到五项**（申请/合同/报表/普通文档/涉密文档）：
    # 这里必须用合法值，非法值由下面的用例证明会被拒绝。
    response = auth_api(admin_user).post(
        "/api/v1/knowledge/files/upload/",
        {
            "file": _upload(),
            "library": str(library.pk),
            "directory_path": "产品信息库/技术接口",
            "classification": "普通文档",
            "customer": "华东石化股份有限公司",
            "department": "产品部",
            "source": "ai_chat",
        },
        format="multipart",
    )
    assert response.status_code in {201, 202}
    record = IngestedFile.objects.get()
    assert record.directory_path == "产品信息库/技术接口"
    assert record.classification == "普通文档"
    assert record.customer == "华东石化股份有限公司"
    assert record.source == "ai_chat"


def test_upload_rejects_a_classification_outside_the_five_values(auth_api, admin_user, library):
    """五项之外的性质必须被拒（旧行为是"什么都收"，会让筛选与统计口径漂移）。"""
    response = auth_api(admin_user).post(
        "/api/v1/knowledge/files/upload/",
        {"file": _upload(), "library": str(library.pk), "classification": "技术文档"},
        format="multipart",
    )
    assert response.status_code == 400, response.content[:200]
    assert IngestedFile.objects.count() == 0


# ------------------------------------------------------------------ 向量集合 ----
def test_collections_are_read_only(auth_api, admin_user, collection):
    body = auth_api(admin_user).get("/api/v1/knowledge/collections/").json()
    assert body["count"] == 1
    assert body["results"][0]["dimension"] == 1024
    assert body["results"][0]["embedding_model"] == "bge-large-zh-v1.5"


def test_chunk_serializer_exposes_permission_code(auth_api, admin_user, library, make_file):
    record = make_file(library)
    add_chunks(record, ["一段"])
    row = auth_api(admin_user).get(f"/api/v1/knowledge/files/{record.pk}/chunks/").json()["results"][0]
    assert row["permission_code"] == f"library:{library.pk}"
    assert DocumentChunk.objects.count() == 1


# ------------------------------------------------------------------ 操作登记表 ----
def test_operation_catalog_lists_intents(auth_api, admin_user):
    body = auth_api(admin_user).get(reverse("planner:operation-catalog")).json()
    intents = {row["intent"] for row in body["results"]}
    assert {"delete_file", "delete_library", "compose_document"} <= intents
    assert body["count"] == len(body["results"])
