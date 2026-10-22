"""不可篡改底层测试：ORM 层 + 模型层 + 领域方法边界。

需求 7 的"AI 权限最大，但绝不能修改已入库信息"在这里被拆成可执行断言：
即使调用方是 superuser、即使绕过 API 直接写 ORM，也改不动已入库资料。
"""

from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction

from apps.knowledge.models import DocumentChunk, IngestedFile, ParseStatus
from apps.knowledge.services import add_chunks, ingest_file
from governance.immutable import ImmutableRecordError
from governance.registry import (
    APPEND_ONLY_MODELS,
    CONTROLLED_MUTABLE_FIELDS,
    ImmutableManager,
    guard_instance_delete,
    guard_instance_save,
    is_protected_api_path,
    is_protected_model,
)

pytestmark = pytest.mark.django_db


def test_protected_models_are_registered():
    assert "knowledge.IngestedFile" in APPEND_ONLY_MODELS
    assert "knowledge.DocumentChunk" in APPEND_ONLY_MODELS
    assert "audit.AuditEvent" in APPEND_ONLY_MODELS
    assert is_protected_model("knowledge.IngestedFile") is True
    assert is_protected_model("knowledge.KnowledgeLibrary") is False


def test_protected_models_use_immutable_manager(make_file, library):
    record = make_file(library)
    assert isinstance(IngestedFile.objects, ImmutableManager)
    assert isinstance(DocumentChunk.objects, ImmutableManager)
    assert record.pk is not None


def test_controlled_fields_are_declared():
    declared = CONTROLLED_MUTABLE_FIELDS["knowledge.IngestedFile"]
    # P1 就有的字段
    assert {
        "parse_status",
        "parse_error",
        "chunk_count",
        "vector_point_id",
        "parsed_at",
    } <= set(declared)
    # P2 新增：解析/落盘/索引这些"处理进度"字段也必须走受控通道，
    # 否则流水线只能绕过守卫写库，等于把不可篡改保证捅穿。
    assert {
        "parse_attempts",
        "raw_path",
        "raw_size_bytes",
        "embedded_chunk_count",
        "vector_status",
        "vector_collection",
        "vector_collection_name",
        "vector_generation",
    } <= set(declared)
    # 模型自己声明的受控字段必须与登记表完全一致，避免"登记表放行、模型不放行"的漂移
    assert set(IngestedFile.CONTROLLED_FIELDS) == set(declared)
    # 不可篡改的核心字段一个都不许出现在受控通道里
    assert not (set(declared) & {"original_name", "content_sha256", "library", "directory_path"})


# ------------------------------------------------------------------ 资料守卫 ----
def test_update_existing_file_is_rejected(make_file, library):
    record = make_file(library)
    record.original_name = "改名.md"
    with pytest.raises(ImmutableRecordError) as excinfo:
        record.save()
    assert excinfo.value.model_label == "knowledge.IngestedFile"
    assert "修改（save）" in excinfo.value.operation
    record.refresh_from_db()
    assert record.original_name != "改名.md"


def test_second_save_of_persisted_instance_is_rejected(make_file, library):
    record = make_file(library)
    with pytest.raises(ImmutableRecordError):
        record.save()


def test_delete_file_is_rejected(make_file, library):
    record = make_file(library)
    with pytest.raises(ImmutableRecordError) as excinfo:
        record.delete()
    assert "删除（delete）" in excinfo.value.operation
    assert IngestedFile.objects.filter(pk=record.pk).exists()


def test_queryset_update_is_rejected(make_file, library):
    make_file(library)
    with pytest.raises(ImmutableRecordError, match="批量修改"):
        IngestedFile.objects.all().update(original_name="x")


def test_queryset_delete_is_rejected(make_file, library):
    make_file(library)
    with pytest.raises(ImmutableRecordError, match="批量删除"):
        IngestedFile.objects.all().delete()


def test_bulk_update_is_rejected(make_file, library):
    record = make_file(library)
    record.original_name = "x"
    with pytest.raises(ImmutableRecordError, match="批量修改"):
        IngestedFile.objects.bulk_update([record], fields=["original_name"])


def test_guard_helpers_reject_persisted_instance(make_file, library):
    record = make_file(library)
    with pytest.raises(ImmutableRecordError):
        guard_instance_save(record)
    with pytest.raises(ImmutableRecordError):
        guard_instance_delete(record)


def test_creation_without_hash_is_rejected(library):
    record = IngestedFile(library=library, original_name="no-hash.md", content_sha256="")
    with pytest.raises(ImmutableRecordError, match="content_sha256"):
        record.save()


def test_duplicate_content_in_same_library_is_rejected(library):
    payload = dict(
        library=library,
        original_name="a.md",
        content_sha256="a" * 64,
        size_bytes=10,
    )
    IngestedFile.objects.create(**payload)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            IngestedFile.objects.create(**{**payload, "original_name": "b.md"})


def test_same_hash_across_libraries_is_allowed(make_library):
    first = make_library(code="alpha", name="甲库")
    second = make_library(code="beta", name="乙库")
    common = dict(original_name="same.md", content_sha256="b" * 64, size_bytes=5)
    IngestedFile.objects.create(library=first, **common)
    IngestedFile.objects.create(library=second, **common)
    assert IngestedFile.objects.count() == 2


# ------------------------------------------------------------ 受控状态流转 ----
def test_update_parse_status_is_controlled(make_file, library):
    record = make_file(library, status=ParseStatus.PENDING)
    record.update_parse_status(ParseStatus.RUNNING)
    record.refresh_from_db()
    assert record.parse_status == ParseStatus.RUNNING

    record.update_parse_status(ParseStatus.DONE, chunk_count=12)
    record.refresh_from_db()
    assert record.parse_status == ParseStatus.DONE
    assert record.chunk_count == 12
    assert record.parsed_at is not None


def test_illegal_transition_is_rejected(make_file, library):
    record = make_file(library, status=ParseStatus.DONE)
    with pytest.raises(ImmutableRecordError, match="不是合法流转"):
        record.update_parse_status(ParseStatus.PENDING)


def test_controlled_update_does_not_touch_immutable_fields(make_file, library):
    record = make_file(library, status=ParseStatus.PENDING, name="原件.md")
    record.update_parse_status(ParseStatus.RUNNING)
    record.refresh_from_db()
    assert record.original_name == "原件.md"
    assert record.content_sha256


# ------------------------------------------------------------------ 分段守卫 ----
def test_chunks_are_append_only(make_file, library):
    record = make_file(library)
    chunks = add_chunks(record, ["第一段", "第二段"])
    assert [chunk.ordinal for chunk in chunks] == [0, 1]
    assert record.chunks.count() == 2

    chunk = chunks[0]
    chunk.text = "被改了"
    with pytest.raises(ImmutableRecordError):
        chunk.save()
    with pytest.raises(ImmutableRecordError):
        chunk.delete()
    with pytest.raises(ImmutableRecordError):
        DocumentChunk.objects.all().update(text="x")


def test_chunks_ordinal_is_unique_per_file(make_file, library):
    record = make_file(library)
    add_chunks(record, ["a"])
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            DocumentChunk.objects.create(file=record, ordinal=0, text="dup", permission_code="library:1")


def test_chunk_permission_code_follows_file(make_file, library):
    record = make_file(library)
    chunks = add_chunks(record, ["x"])
    assert chunks[0].permission_code == record.permission_code == f"library:{library.pk}"


# ------------------------------------------------------------------ 入库服务 ----
def test_ingest_is_idempotent_for_same_content(library, user):
    first = ingest_file(
        library=library,
        original_name="合同.md",
        content_sha256="c" * 64,
        size_bytes=128,
        actor=user,
    )
    second = ingest_file(
        library=library,
        original_name="合同-副本.md",
        content_sha256="c" * 64,
        size_bytes=128,
        actor=user,
    )
    assert first.created is True
    assert second.created is False
    assert second.reason == "duplicate_content"
    assert second.file.pk == first.file.pk
    assert IngestedFile.objects.count() == 1


def test_ingest_writes_audit_event(library, user):
    from apps.audit.models import AuditEvent

    ingest_file(
        library=library,
        original_name="接口.md",
        content_sha256="d" * 64,
        size_bytes=64,
        actor=user,
        conversation_key="conv-9",
    )
    event = AuditEvent.objects.get(action="file.ingested")
    assert event.resource_kind == "ingested_file"
    assert event.payload["sha256"] == "d" * 64
    assert event.payload["library"] == library.code
    assert event.conversation_key == "conv-9"


def test_ingest_skip_writes_audit_event(library, user):
    from apps.audit.models import AuditEvent

    ingest_file(library=library, original_name="x.md", content_sha256="e" * 64, size_bytes=1, actor=user)
    ingest_file(library=library, original_name="x.md", content_sha256="e" * 64, size_bytes=1, actor=user)
    assert AuditEvent.objects.filter(action="file.ingest_skipped").count() == 1


def test_ingest_defaults_follow_library(library, user):
    result = ingest_file(
        library=library,
        original_name="模板.docx",
        content_sha256="f" * 64,
        size_bytes=8,
        actor=user,
    )
    assert result.file.department == library.department
    assert result.file.file_type == "docx"
    assert result.file.vector_collection == library.vector_collection
    assert result.file.parse_status == ParseStatus.PENDING


# ------------------------------------------------------------ HTTP 路径登记 ----
@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/knowledge/files/",
        "/api/v1/knowledge/files/12/",
        "/api/v1/knowledge/files/12/chunks/",
        "/api/v1/audit/events/",
    ],
)
def test_protected_paths_detected(path):
    assert is_protected_api_path(path) is True


@pytest.mark.parametrize(
    "path",
    ["/api/v1/knowledge/libraries/", "/api/v1/knowledge/stats/", "/api/v1/planner/operations/"],
)
def test_unprotected_paths_pass(path):
    assert is_protected_api_path(path) is False
