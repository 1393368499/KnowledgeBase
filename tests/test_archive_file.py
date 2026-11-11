"""受控**归档资料**（移出检索）的专项回归。

背景（独立审计 D1）：``ParseStatus.ARCHIVED`` 一直存在于界面与状态机里
（``apps/knowledge/models.py`` 明确写了 ``DONE → ARCHIVED``，且 ``ARCHIVED`` 是终态），
但全仓**没有一处写入方** —— "已归档"这个状态在真实系统里永远不会出现。
本文件锁死新意图 ``archive_file`` 的口径：

* 只有 ``DONE`` 能归档（状态机不允许从 PENDING/RUNNING/FAILED 跳过去）；
* 归档**不删任何东西**：业务记录、分段记录、磁盘原始字节全部不变；
* 归档要**真的把它移出检索**：删掉它在 collection 里的向量点
  （``point_id_for`` 是稳定派生，可精确删除）；
* 归档是**单向**的（终态），重复归档是幂等的、且不重复留痕；
* 需要 ``library:admin``；公开入口（catalog / 提案 / 确认 / 执行 API、模型工具）
  已按 ``RETIRED_INTENTS`` 停用（410 / 目录不出现），登记表仍保留参数与权限定义
  供历史记录校验；内部实现与其它非删除动作一样**提案即执行**（零人工确认），
  审计、目标身份核验、权限复核与幂等语义不变；
* 向量点删失败时**仍然归档**，但如实报 ``complete=False``。

全部使用隔离测试数据（临时库/临时原件/临时向量目录）。
"""

from __future__ import annotations

import pytest

from apps.audit.models import AuditEvent
from apps.knowledge import storage
from apps.knowledge.models import (
    DocumentChunk,
    IngestedFile,
    KnowledgeLibrary,
    ParseStatus,
    VectorCollection,
)
from apps.planner import services
from apps.planner.models import OperationRecord

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _isolated_blobs(isolated_paths):
    """原件一律落在临时目录：绝不往真实 ``media/raw`` 写测试数据。"""
    return isolated_paths


class _KeepAliveClient:
    """转发一切调用、``close()`` 故意空实现。

    测试里 ``vectorindex.build_client()`` 拿到的客户端由夹具共享
    （``tests/conftest.py::library_setup_vector``），而归档逻辑按生产约定
    会在 ``finally`` 里关掉自己 build 的客户端 —— 生产里那是对的（每次新建），
    在测试里会把共享客户端关掉。这里只让 ``close()`` 失效。
    """

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def close(self):  # noqa: D401 - 有意为空
        """有意不做任何事（见类 docstring）。"""


@pytest.fixture
def vector_env(library_setup_vector, monkeypatch):
    """把 ``vectorindex.build_client`` 指向夹具的共享客户端（不关它）。"""
    from apps.knowledge import vectorindex

    monkeypatch.setattr(
        vectorindex, "build_client", lambda: _KeepAliveClient(library_setup_vector)
    )
    return library_setup_vector


# ------------------------------------------------------------------ 辅助 ----


def _file_with_blob(library, make_file, name="归档测试.md", *, status=ParseStatus.DONE):
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


def _real_vector_file(library, make_file, isolated_paths, client):
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

    body = "# 归档真实用例\n\n质保期：设备验收合格之日起 12 个月。\n".encode()
    stored = storage.store_bytes(body)
    record = make_file(
        library=library,
        name="real-archive.md",
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


def _run(user, file_id: int, params_extra: dict | None = None) -> dict:
    """提案即执行（现行策略：非删除动作零人工确认），返回执行结果体。

    不再有"确认一次再执行"这一步：``propose_operation`` 内部就把操作执行完，
    签发/核销确认凭证的分支只对删除保留。
    """
    params = {"file_id": file_id}
    params.update(params_extra or {})
    proposal = services.propose_operation(user, intent="archive_file", params=params)
    assert proposal["auto_executed"] is True, proposal
    assert proposal["confirm_token"] == "", "非删除动作不应再签发确认凭证"
    operation = OperationRecord.objects.get(pk=proposal["operation_id"])
    assert operation.status == OperationRecord.Status.EXECUTED, operation.status
    assert operation.confirmations == 0
    assert operation.error == ""
    return proposal["result"]


# ------------------------------------------------------------------ 登记 ----


def test_spec_keeps_historical_definition_but_is_retired_and_auto_executes():
    """归档已从公开入口停用；登记表保留定义供历史记录校验，内部实现直接执行。

    现行策略（``docs/delete-only-confirmation-2026-10-27.md``）：只有删除保留
    人工确认，其它非删除动作校验通过后直接执行 —— 归档不再有"一次确认"这一步。
    停用只作用于界面 / 模型工具 / 公共 API（``RETIRED_INTENTS``）。
    """
    spec = services.get_spec("archive_file")
    assert spec is not None
    assert spec.params == {"file_id": "int", "reason": "str?"}
    assert spec.permission == "library:admin"
    # 只有删除要求人工确认；归档不是删除 → 提案即执行、不签发凭证
    assert spec.auto_execute is True
    assert spec.requires_confirmation is False
    assert spec.requires_destructive_flow is False
    assert spec.ticket_scope == ""
    # 公开入口已停用：操作目录里不再出现（但登记项本身仍可被历史记录校验）
    from apps.planner.operations import RETIRED_INTENTS, all_specs

    assert "archive_file" in RETIRED_INTENTS
    assert "archive_file" not in {row.intent for row in all_specs()}


# ------------------------------------------------------------------ 状态机 ----


def test_only_done_can_be_archived(admin_user, library, make_file):
    record = _file_with_blob(library, make_file, "未完成.md", status=ParseStatus.PENDING)
    with pytest.raises(services.PlannerError) as excinfo:
        _run(admin_user, record.pk)
    assert "只有已完成解析的资料才能归档" in str(excinfo.value)
    record.refresh_from_db()
    assert record.parse_status == ParseStatus.PENDING


@pytest.mark.parametrize("status", [ParseStatus.RUNNING, ParseStatus.FAILED, ParseStatus.ARCHIVED])
def test_other_states_are_refused(admin_user, library, make_file, status):
    """RUNNING/FAILED 拒绝；已归档则幂等返回（不报错、不重复留痕）。"""
    record = _file_with_blob(library, make_file, f"{status}.md")
    IngestedFile._base_manager.filter(pk=record.pk).update(parse_status=status)
    record.refresh_from_db()
    if status == ParseStatus.ARCHIVED:
        result = _run(admin_user, record.pk)
        assert result["already_archived"] is True
        assert result["complete"] is True
    else:
        with pytest.raises(services.PlannerError) as excinfo:
            _run(admin_user, record.pk)
        assert "只有已完成解析的资料才能归档" in str(excinfo.value)


# ------------------------------------------------------------------ 语义 ----


def test_archive_keeps_records_chunks_and_bytes(admin_user, library, make_file):
    record = _file_with_blob(library, make_file)
    raw_before = storage.read_blob(record.content_sha256)
    chunks_before = DocumentChunk.objects.filter(file=record).count()

    result = _run(admin_user, record.pk, {"reason": "合同已过期，不再参与检索"})

    assert result["archived"] is True
    assert result["status"] == ParseStatus.ARCHIVED
    assert result["target"] == record.original_name
    record.refresh_from_db()
    assert record.parse_status == ParseStatus.ARCHIVED
    # 一行都没删、一个字节都没改
    assert IngestedFile._base_manager.filter(pk=record.pk).exists()
    assert DocumentChunk.objects.filter(file=record).count() == chunks_before
    assert storage.read_blob(record.content_sha256) == raw_before, "原件字节被改动"
    assert storage.verify_blob(record.content_sha256) is True
    assert KnowledgeLibrary.objects.filter(pk=library.pk).exists()


def test_archive_appends_audit_event(admin_user, library, make_file):
    record = _file_with_blob(library, make_file)
    _run(admin_user, record.pk, {"reason": "过期"})
    event = AuditEvent.objects.filter(action="knowledge.file_archived").order_by("-id").first()
    assert event is not None
    assert event.resource_key == str(record.pk)
    assert event.payload.get("reason") == "过期"
    assert event.payload.get("library_id") == library.pk


def test_archive_is_idempotent_without_second_event(admin_user, library, make_file):
    record = _file_with_blob(library, make_file)
    _run(admin_user, record.pk)
    before = AuditEvent.objects.filter(action="knowledge.file_archived").count()
    result = _run(admin_user, record.pk)
    assert result["already_archived"] is True
    assert AuditEvent.objects.filter(action="knowledge.file_archived").count() == before


def test_archived_file_still_has_its_points_removed(admin_user, library, make_file, isolated_paths, vector_env):
    """**真实**跑一遍：归档后该 collection 里这份资料的点真的没了，分段还在。"""
    from apps.knowledge import vectorindex

    record, collection = _real_vector_file(library, make_file, isolated_paths, vector_env)
    chunks_before = DocumentChunk.objects.filter(file=record).count()
    points_before = vectorindex.count_points(vector_env, collection)
    assert points_before > 0

    result = _run(admin_user, record.pk)

    assert result["complete"] is True, result
    assert result["steps"]["vectors"]["points_deleted"] == points_before
    assert vectorindex.count_points(vector_env, collection) == 0, "向量点没删干净"
    record.refresh_from_db()
    assert record.parse_status == ParseStatus.ARCHIVED
    assert DocumentChunk.objects.filter(file=record).count() == chunks_before
    assert storage.verify_blob(record.content_sha256) is True


def test_vector_failure_still_archives_but_reports_partial(
    admin_user, library, make_file, monkeypatch
):
    """删点失败**不阻断**归档，但必须如实报 ``complete=False`` 与原因。"""
    from apps.knowledge import vectorindex

    record = _file_with_blob(library, make_file)
    IngestedFile._base_manager.filter(pk=record.pk).update(
        vector_collection_name="kb-does-not-exist-g1"
    )
    record.refresh_from_db()

    def boom(client, name, point_ids):
        raise RuntimeError("Qdrant 连接被拒绝")

    monkeypatch.setattr(vectorindex, "delete_points", boom)
    result = _run(admin_user, record.pk)

    assert result["archived"] is True
    assert result["complete"] is False
    assert result["steps"]["vectors"]["ok"] is False
    assert "Qdrant 连接被拒绝" in result["steps"]["vectors"]["detail"]
    record.refresh_from_db()
    assert record.parse_status == ParseStatus.ARCHIVED, "删点失败也不能挡住归档"


# ------------------------------------------------------------------ 权限 ----


def test_archive_requires_library_admin(user, library, make_file, grant_member):
    """普通库成员（WRITER）不能归档：必须库管理员。"""
    from apps.knowledge.models import MemberRole

    grant_member(library, user, MemberRole.WRITER)
    record = _file_with_blob(library, make_file)
    with pytest.raises(services.PlannerError) as excinfo:
        services.propose_operation(user, intent="archive_file", params={"file_id": record.pk})
    assert "管理权限" in str(excinfo.value)


def test_archive_target_must_exist(admin_user, library):
    with pytest.raises(services.PlannerError) as excinfo:
        services.propose_operation(admin_user, intent="archive_file", params={"file_id": 999999})
    assert "资料不存在" in str(excinfo.value)
