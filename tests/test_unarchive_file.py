"""受控**恢复归档**（``unarchive_file``）专项。

背景（独立复现，`artifacts/acceptance/independent-archive-reupload-20261016.json`）：
归档原本是**终态**，而说明写着"要重新启用请重新上传原件" —— 实测重传同内容会命中
内容寻址去重：HTTP 200、`created=false`、`reason=duplicate_content`、仍是同一条
``archived`` 记录、`queued=false`，检索里依然没有它。**承诺不成立**。

本文件把新的真实口径钉死：

* 恢复是**受控入口**：`library:admin` 权限 + 一次独立确认 + 审计事件；
* 恢复**真的重建索引**（真原件 → 真解析/分段/嵌入 → 写回当前生效代次 → DONE）；
  完整走一遍"归档 → 恢复"往返，核对向量点数、分段与原件字节；
* 只有 ``ARCHIVED`` 能恢复；``DONE`` 幂等返回 ``already_active``；其它状态明确拒绝；
* 失败**如实报**（``complete=false`` + 原因，资料仍不在检索里），绝不假装恢复成功；
* **同内容重传不会静默保留 archived**：接口如实返回"需要走恢复入口"。

全部使用隔离测试数据（临时原件与隔离向量目录），不触碰主库与真实 `media/raw`。
"""

from __future__ import annotations

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from apps.audit.models import AuditEvent
from apps.knowledge import storage
from apps.knowledge.models import (
    DocumentChunk,
    IngestedFile,
    KnowledgeLibrary,
    MemberRole,
    ParseStatus,
    VectorCollection,
)
from apps.knowledge.pipeline import PipelineError, StepResult
from apps.planner import services
from apps.planner.models import OperationRecord

pytestmark = pytest.mark.django_db

UPLOAD_URL = "/api/v1/knowledge/files/upload/"


@pytest.fixture(autouse=True)
def _isolated_blobs(isolated_paths):
    """原件一律落在临时目录：绝不往真实 ``media/raw`` 写测试数据。"""
    return isolated_paths


class _KeepAliveClient:
    """转发一切调用，但 ``close()`` 不真的关（共享夹具客户端不能被关掉）。"""

    def __init__(self, inner):
        self._inner = inner

    def close(self) -> None:
        return None

    def __getattr__(self, item):
        return getattr(self._inner, item)


@pytest.fixture
def vector_env(library_setup_vector, monkeypatch):
    """把 ``vectorindex.build_client`` 指向夹具的共享客户端（不关它）。"""
    from apps.knowledge import vectorindex

    monkeypatch.setattr(
        vectorindex, "build_client", lambda: _KeepAliveClient(library_setup_vector)
    )
    return library_setup_vector


# ------------------------------------------------------------------ 辅助 ----


def _file_with_blob(library, make_file, name="归档件.md", *, status=ParseStatus.DONE):
    """造一份有**真原件**、有分段的资料（原件落在隔离目录）。"""
    payload = f"{name}：质保期 12 个月。".encode()
    blob = storage.store_bytes(payload)
    record = make_file(library=library, name=name, sha=blob.sha256, status=status)
    IngestedFile._base_manager.filter(pk=record.pk).update(
        raw_path=blob.rel_path, chunk_count=1, raw_size_bytes=blob.size_bytes
    )
    DocumentChunk.objects.create(
        file=record, ordinal=0, text=payload.decode(), permission_code=f"library:{library.pk}"
    )
    record.refresh_from_db()
    return record


def _archived(library, make_file, name="归档件.md"):
    record = _file_with_blob(library, make_file, name)
    record.update_parse_status(ParseStatus.ARCHIVED)
    record.refresh_from_db()
    assert record.parse_status == ParseStatus.ARCHIVED
    return record


def _real_vector_file(library, make_file, isolated_paths, client, name="恢复真实.md"):
    """真原件 → 真解析/分段 → 真 Qdrant 写入，返回 (record, collection)。"""
    from apps.knowledge import pipeline, vectorindex
    from apps.knowledge import testing_adapter as ta
    from apps.knowledge.embedding import active_profile

    collection = ta.test_collection_name(library.pk, 1)
    vectorindex.ensure_collection(client, collection, 1024)
    profile = active_profile()
    VectorCollection.objects.create(
        name=collection,
        dimension=1024,
        embedding_model=profile.model,
        model_version=profile.model_version,
        profile=profile.name,
        library=library,
        storage_mode="embedded",
        storage_path=str(isolated_paths["vector"]),
        is_active=True,
    )
    KnowledgeLibrary.objects.filter(pk=library.pk).update(
        active_generation=1, vector_collection=collection
    )
    library.refresh_from_db()

    body = f"# {name}\n\n质保期：设备验收合格之日起 12 个月。\n".encode()
    stored = storage.store_bytes(body)
    record = make_file(
        library=library,
        name=name,
        sha=stored.sha256,
        size_bytes=stored.size_bytes,
        raw_path=stored.rel_path,
        raw_size_bytes=stored.size_bytes,
        status=ParseStatus.PENDING,
        vector_collection_name=collection,
    )
    IngestedFile._base_manager.filter(pk=record.pk).update(
        vector_generation=1, vector_collection_name=collection
    )
    record.refresh_from_db()
    step = pipeline.process_file(record, client=client)
    assert step.ok is True, step.detail
    record.refresh_from_db()
    assert record.parse_status == ParseStatus.DONE
    assert vectorindex.count_points(client, collection) == record.chunk_count > 0
    return record, collection


def _run(user, file_id: int, *, intent: str = "unarchive_file") -> dict:
    """提案 → 一次确认 → 执行，返回执行结果体。"""
    proposal = services.propose_operation(user, intent=intent, params={"file_id": file_id})
    operation = OperationRecord.objects.get(pk=proposal["operation_id"])
    services.confirm_operation(user, operation, token=proposal["confirm_token"])
    operation.refresh_from_db()
    assert operation.status == OperationRecord.Status.ARMED, operation.status
    return services.execute_operation(user, operation)["result"]


# ------------------------------------------------------------------ 登记 ----
def test_spec_is_controlled_with_library_admin():
    spec = services.get_spec("unarchive_file")
    assert spec is not None
    assert spec.params == {"file_id": "int"}
    assert spec.permission == "library:admin"
    assert spec.requires_confirmation is True
    # 恢复不是删除：**不**走三次确认的破坏性流程
    assert spec.requires_destructive_flow is False
    assert spec.ticket_scope == "unarchive_file"


def test_reader_cannot_propose(library, user, make_file, grant_member):
    grant_member(library, user, role=MemberRole.READER)
    record = _archived(library, make_file)
    with pytest.raises(services.PlannerError) as exc:
        services.propose_operation(user, intent="unarchive_file", params={"file_id": record.pk})
    assert "权限" in str(exc.value)


def test_library_admin_can_propose(library, user, make_file, grant_member):
    grant_member(library, user, role=MemberRole.ADMIN)
    record = _archived(library, make_file)
    proposal = services.propose_operation(
        user, intent="unarchive_file", params={"file_id": record.pk}
    )
    assert proposal["operation_id"]
    assert proposal["target_label"] == record.original_name


# ------------------------------------------------------- 真实往返：归档→恢复 ----
def test_archive_then_restore_round_trip_rebuilds_vectors(
    admin_user, library, make_file, isolated_paths, vector_env, monkeypatch
):
    from apps.knowledge import pipeline as pipeline_module, vectorindex

    client = vector_env
    record, collection = _real_vector_file(library, make_file, isolated_paths, client)
    points_before = record.chunk_count
    raw_before = storage.read_blob(record.content_sha256)
    monkeypatch.setattr(
        pipeline_module, "QDRANT_CLIENT_FACTORY", lambda: _KeepAliveClient(client)
    )

    archived = _run(admin_user, record.pk, intent="archive_file")
    assert archived["status"] == ParseStatus.ARCHIVED
    record.refresh_from_db()
    assert vectorindex.count_points(client, collection) == 0, "归档应删掉它的向量点"
    assert storage.read_blob(record.content_sha256) == raw_before, "归档不得改原件"

    restored = _run(admin_user, record.pk, intent="unarchive_file")
    assert restored["restored"] is True, restored
    assert restored["complete"] is True, restored
    record.refresh_from_db()
    assert record.parse_status == ParseStatus.DONE
    assert record.vector_status == "indexed"
    assert record.chunk_count == points_before
    assert vectorindex.count_points(client, collection) == points_before, "恢复应把向量写回来"
    assert DocumentChunk.objects.filter(file=record).count() == record.chunk_count
    assert storage.read_blob(record.content_sha256) == raw_before, "恢复不得改原件"
    assert storage.verify_blob(record.content_sha256) is True
    event = AuditEvent.objects.filter(action="knowledge.file_unarchived").order_by("-id").first()
    assert event is not None and event.payload["ok"] is True


def test_repeat_restore_is_idempotent(admin_user, library, make_file, monkeypatch):
    """第二次恢复：它已经在检索里 → 如实返回 already_active，不假装又做一次。"""
    record = _archived(library, make_file, name="重复恢复.md")

    def fake_retry(file, *, actor=None, profile=None, client=None, allow_archived=False):
        IngestedFile._base_manager.filter(pk=file.pk).update(
            parse_status=ParseStatus.DONE,
            vector_status="indexed",
            chunk_count=1,
            embedded_chunk_count=1,
        )
        return StepResult(ok=True, detail="fake restore")

    monkeypatch.setattr("apps.knowledge.pipeline.retry_file", fake_retry)
    first = _run(admin_user, record.pk)
    assert first["restored"] is True and first["already_active"] is False

    record.refresh_from_db()
    assert record.parse_status == ParseStatus.DONE
    second = _run(admin_user, record.pk)
    assert second["restored"] is False
    assert second["already_active"] is True
    assert second["complete"] is True
    assert "没有做任何改动" in second["note"]


def test_non_archived_states_are_refused(admin_user, library, make_file):
    record = _file_with_blob(library, make_file, name="非归档件.md")
    IngestedFile._base_manager.filter(pk=record.pk).update(parse_status=ParseStatus.FAILED)
    record.refresh_from_db()
    with pytest.raises(services.PlannerError) as exc:
        _run(admin_user, record.pk)
    assert "只有已归档的资料才能恢复" in str(exc.value)


def test_restore_failure_is_reported_honestly(admin_user, library, make_file, monkeypatch):
    """恢复失败（模型不可用/原件校验失败）：complete=false + 原因，绝不假装成功。"""
    record = _archived(library, make_file, name="失败恢复.md")

    def boom(file, *, actor=None, profile=None, client=None, allow_archived=False):
        raise PipelineError("模型服务不可用：连接被拒绝")

    monkeypatch.setattr("apps.knowledge.pipeline.retry_file", boom)
    result = _run(admin_user, record.pk)

    assert result["restored"] is False
    assert result["complete"] is False
    assert "不可用" in result["steps"]["status"]["detail"]
    record.refresh_from_db()
    assert record.parse_status == ParseStatus.ARCHIVED, "失败的恢复不得把状态改坏"
    event = AuditEvent.objects.filter(action="knowledge.file_unarchived").order_by("-id").first()
    assert event is not None and event.payload["ok"] is False


# ------------------------------------- 权限绕过：普通入口不得恢复归档 ----
#
# 独立审查发现的**权限绕过**：`retry_file` 曾无条件接受 ARCHIVED，于是
# `POST files/<id>/process/?force=true`（writer 权限即可）、planner 的 `parse_file`
# （library:write）以及后台普通任务都能把 archived 直接变成 RUNNING ——
# 绕过了 `unarchive_file` 的 library:admin + 确认 + 审计。下面把四条路都钉死。


def test_writer_cannot_restore_via_process_endpoint(
    api, library, user, make_file, grant_member
):
    grant_member(library, user, role=MemberRole.WRITER)
    record = _archived(library, make_file, name="越权恢复.md")
    api.force_authenticate(user=user)
    for payload in ({}, {"force": "1"}):
        response = api.post(
            f"/api/v1/knowledge/files/{record.pk}/process/", payload, format="json"
        )
        assert response.status_code >= 400, response.content[:300]
        record.refresh_from_db()
        assert record.parse_status == ParseStatus.ARCHIVED, "普通 process 接口把归档恢复了"


def test_background_pipeline_cannot_restore(admin_user, library, make_file):
    """后台普通任务（worker 直接调 pipeline）也不得反归档 —— 即使调用者是管理员。"""
    from apps.knowledge.pipeline import PipelineError, process_file, retry_file

    record = _archived(library, make_file, name="后台越权.md")
    with pytest.raises(PipelineError) as exc1:
        retry_file(record, actor=admin_user)
    assert "已归档" in str(exc1.value)
    with pytest.raises(PipelineError) as exc2:
        process_file(record, actor=admin_user, force=True)
    assert "已归档" in str(exc2.value)
    record.refresh_from_db()
    assert record.parse_status == ParseStatus.ARCHIVED
    assert record.parse_lease_token == "", "被拒绝的调用不得留下租约"


def test_parse_file_intent_cannot_restore(admin_user, library, make_file):
    """planner 的 `parse_file`（library:write）不得反归档：明确失败且状态不变。"""
    record = _archived(library, make_file, name="解析意图越权.md")
    proposal = services.propose_operation(
        admin_user, intent="parse_file", params={"file_id": record.pk}
    )
    operation = OperationRecord.objects.get(pk=proposal["operation_id"])
    services.confirm_operation(admin_user, operation, token=proposal["confirm_token"])
    operation.refresh_from_db()
    with pytest.raises(services.PlannerError) as exc:
        services.execute_operation(admin_user, operation)
    assert "已归档" in str(exc.value)
    record.refresh_from_db()
    assert record.parse_status == ParseStatus.ARCHIVED, "parse_file 把归档恢复了"


def test_admin_controlled_restore_still_works_after_guard(
    admin_user, library, make_file, monkeypatch
):
    """堵住绕过之后，受控入口本身必须照常可用（不能把路全堵死）。"""
    record = _archived(library, make_file, name="受控恢复仍可用.md")

    def fake_retry(file, *, actor=None, profile=None, client=None, allow_archived=False):
        assert allow_archived is True, "受控恢复必须显式打开 allow_archived 能力"
        IngestedFile._base_manager.filter(pk=file.pk).update(
            parse_status=ParseStatus.DONE,
            vector_status="indexed",
            chunk_count=1,
            embedded_chunk_count=1,
        )
        return StepResult(ok=True, detail="controlled restore")

    monkeypatch.setattr("apps.knowledge.pipeline.retry_file", fake_retry)
    result = _run(admin_user, record.pk)
    assert result["restored"] is True and result["complete"] is True, result
    record.refresh_from_db()
    assert record.parse_status == ParseStatus.DONE


# -------------------------------------------- 重传不得静默保留 archived ----
def test_reupload_of_archived_content_is_explicit_not_silent(
    api, admin_user, library, library_setup_vector, isolated_paths
):
    """独立复现场景：同内容重传**必须**如实说明"需要走恢复入口"，不静默留在归档态。"""
    api.force_authenticate(user=admin_user)
    body = b"# archive reupload\n\nsame bytes\n"
    with override_settings(KB_UPLOAD_PROCESS_MODE="queue"):
        first = api.post(
            UPLOAD_URL,
            {"file": SimpleUploadedFile("reupload.md", body), "library": library.code},
            format="multipart",
        )
        assert first.status_code in (200, 201, 202), first.content[:300]
        record = IngestedFile._base_manager.get(library=library)
        # 直接置为 DONE（PENDING→DONE 不是合法流转），再走受控流转 DONE→ARCHIVED
        IngestedFile._base_manager.filter(pk=record.pk).update(parse_status=ParseStatus.DONE)
        record.refresh_from_db()
        record.update_parse_status(ParseStatus.ARCHIVED)
        record.refresh_from_db()
        assert record.parse_status == ParseStatus.ARCHIVED

        second = api.post(
            UPLOAD_URL,
            {"file": SimpleUploadedFile("reupload.md", body), "library": library.code},
            format="multipart",
        )
    assert second.status_code == 200, second.content[:300]
    payload = second.json()
    assert payload["created"] is False
    assert payload["reason"] == "duplicate_content"
    processing = payload["processing"]
    assert processing["archived"] is True
    assert processing["restore_required"] is True
    assert processing["queued"] is False
    assert "恢复" in processing["detail"]
    record.refresh_from_db()
    assert record.parse_status == ParseStatus.ARCHIVED, "重传不得静默把它恢复（要走受控入口）"
    assert IngestedFile._base_manager.filter(library=library).count() == 1
