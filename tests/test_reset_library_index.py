"""受控**重置整库索引**的专项回归。

用户文档把这一项写成"清空/重建整库向量"。实现选择的语义是**逐份强制重建**，
本文件把这条口径和它的安全边界一起锁死：

* **必须走受控流程**：`reset_library_index` 需要 `system:operate`、**三次独立确认**、
  且第 3 次**必须准确输入库名**；没有不受控入口；
* **原件与业务记录一行不动**：资料记录、分段记录、磁盘原始字节都不变；
* **先清空再重建是错的**：中途失败/超上限时**不许**清理旧代次，
  只如实报 `PARTIAL`（避免留下半清空的库）；
* **只有全部成功才清旧代次**：清理后登记表要与现实一致（collection 真没了才删登记行）；
* **执行窗口**：有资料正在处理（租约未过期）→ 拒绝执行；
* **目标一致性**：确认后库内资料或索引代次发生变化 → 必须重新提案；
* **审计留痕**：授权与完成各有事件。

全部使用隔离测试数据（临时库/临时原件），绝不触碰主库与真实 `media/raw`。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.knowledge import storage
from apps.knowledge.models import (
    DocumentChunk,
    IngestedFile,
    KnowledgeLibrary,
    ParseStatus,
    VectorCollection,
)
from apps.knowledge.pipeline import StepResult
from apps.planner import services
from apps.planner.models import OperationRecord
from apps.planner.operations import DESTRUCTIVE_SCOPES

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _isolated_blobs(isolated_paths):
    """原件一律落在临时目录：绝不往真实 ``media/raw`` 写测试数据。"""
    return isolated_paths


# ------------------------------------------------------------------ 辅助 ----


def _original(library, make_file, name: str, text: str = "重置索引测试内容", *, raw: bytes = b""):
    """造一份**有真原件、有分段**的资料（原件落在隔离目录里）。"""
    payload = raw or f"{name}-{text}".encode()
    blob = storage.store_bytes(payload)
    record = make_file(library=library, name=name, sha=blob.sha256)
    IngestedFile._base_manager.filter(pk=record.pk).update(
        raw_path=blob.rel_path, chunk_count=1
    )
    DocumentChunk.objects.create(
        file=record, ordinal=0, text=text, permission_code=f"library:{library.pk}"
    )
    record.refresh_from_db()
    return record


def _prepare(admin_user, library, typed_name: str = ""):
    """提案 + 三次确认（第 3 次输入库名），返回 (proposal, operation)。"""
    proposal = services.propose_operation(
        admin_user, intent="reset_library_index", params={"library": library.code}
    )
    operation = OperationRecord.objects.get(pk=proposal["operation_id"])
    token = proposal["confirm_token"]
    name = typed_name or proposal["target_label"]
    for _ in range(services.DESTRUCTIVE_CONFIRMATIONS):
        step = services.confirm_operation(
            admin_user, operation, token=token, typed_name=name
        )
        token = step.get("next_confirm_token") or ""
    operation.refresh_from_db()
    return proposal, operation


def _ok_retry(monkeypatch, calls: list | None = None, ok: bool = True):
    def fake_retry(file, *, actor=None, profile=None, client=None):
        if calls is not None:
            calls.append(file.pk)
        return StepResult(ok=ok, detail="fake rebuild" if ok else "fake incomplete")

    monkeypatch.setattr("apps.knowledge.pipeline.retry_file", fake_retry)


def _spy_drop(monkeypatch, results: list):
    def fake_drop(name: str):
        results.append(name)
        return {"ok": True, "name": name, "verified_absent": True}

    monkeypatch.setattr(services, "_drop_collection", fake_drop)


# ------------------------------------------------------------------ 登记 ----


def test_spec_registered_as_destructive():
    spec = services.get_spec("reset_library_index")
    assert spec is not None
    assert spec.params == {"library": "str"}
    assert spec.permission == "system:operate"
    assert spec.requires_destructive_flow is True
    assert spec.ticket_scope == "reset_library_index"
    assert spec.ticket_scope in DESTRUCTIVE_SCOPES


def test_non_admin_cannot_propose(user, library):
    with pytest.raises(services.PlannerError) as exc:
        services.propose_operation(
            user, intent="reset_library_index", params={"library": library.code}
        )
    assert "权限" in str(exc.value)


def test_unknown_library_rejected(admin_user):
    with pytest.raises(services.PlannerError):
        services.propose_operation(
            admin_user, intent="reset_library_index", params={"library": "no-such-lib"}
        )


# -------------------------------------------------- 三次确认与目标清单 ----


def test_proposal_records_manifest_and_impact(admin_user, library, make_file):
    _original(library, make_file, name="a.md")
    proposal, operation = _prepare(admin_user, library)
    assert proposal["required_confirmations"] == 3
    impact = proposal["impact"]
    assert impact["file_count"] == 1
    assert "重建分段与向量" in impact["index_note"]
    assert operation.result["target_manifest"]["count"] == 1
    assert operation.status == OperationRecord.Status.ARMED


def test_final_confirmation_requires_exact_library_name(admin_user, library, make_file):
    _original(library, make_file, name="a.md")
    proposal = services.propose_operation(
        admin_user, intent="reset_library_index", params={"library": library.code}
    )
    operation = OperationRecord.objects.get(pk=proposal["operation_id"])
    token = proposal["confirm_token"]
    for _ in range(2):
        step = services.confirm_operation(
            admin_user, operation, token=token, typed_name=proposal["target_label"]
        )
        token = step["next_confirm_token"]
    with pytest.raises(services.PlannerError):
        services.confirm_operation(
            admin_user, operation, token=token, typed_name="不是这个库"
        )
    operation.refresh_from_db()
    assert operation.status != OperationRecord.Status.ARMED


def test_execution_requires_confirmed_armed_operation(admin_user, library, make_file):
    _original(library, make_file, name="a.md")
    proposal = services.propose_operation(
        admin_user, intent="reset_library_index", params={"library": library.code}
    )
    operation = OperationRecord.objects.get(pk=proposal["operation_id"])
    with pytest.raises(services.PlannerError) as exc:
        services.execute_operation(admin_user, operation)
    assert "确认" in str(exc.value)


def test_manifest_change_requires_new_proposal(admin_user, library, make_file):
    _original(library, make_file, name="a.md")
    _, operation = _prepare(admin_user, library)
    # 确认之后又有人上传了一份 → 清单指纹变化
    _original(library, make_file, name="late.md")
    with pytest.raises(services.PlannerError) as exc:
        services.execute_operation(admin_user, operation)
    assert "变化" in str(exc.value)


def test_in_flight_file_refuses_reset(admin_user, library, make_file):
    record = _original(library, make_file, name="a.md")
    _, operation = _prepare(admin_user, library)
    IngestedFile._base_manager.filter(pk=record.pk).update(
        parse_status=ParseStatus.RUNNING,
        parse_lease_expires_at=timezone.now() + timedelta(hours=1),
    )
    with pytest.raises(services.PlannerError) as exc:
        services.execute_operation(admin_user, operation)
    assert "正在处理" in str(exc.value)


# ------------------------------------------------------------ 执行路径 ----


def test_rebuilds_every_file_and_keeps_business_rows(
    admin_user, library, make_file, monkeypatch
):
    first = _original(library, make_file, name="a.md", text="甲")
    second = _original(library, make_file, name="b.md", text="乙")
    before_bytes = {
        row.pk: storage.read_blob(row.content_sha256) for row in (first, second)
    }
    _prepare(admin_user, library)
    calls: list[int] = []
    _ok_retry(monkeypatch, calls)
    dropped: list[str] = []
    _spy_drop(monkeypatch, dropped)
    operation = OperationRecord.objects.get(intent="reset_library_index")
    result = services.execute_operation(admin_user, operation)["result"]

    assert result["complete"] is True, result
    assert sorted(calls) == sorted([first.pk, second.pk]), "必须逐份真的重建"
    assert result["rebuilt"] == 2

    # **原件与业务记录一行不动**
    assert IngestedFile._base_manager.filter(pk__in=[first.pk, second.pk]).count() == 2
    assert DocumentChunk.objects.filter(file_id__in=[first.pk, second.pk]).count() == 2
    for pk, raw in before_bytes.items():
        sha = IngestedFile._base_manager.get(pk=pk).content_sha256
        assert storage.read_blob(sha) == raw, "原始字节被改写了"
    assert KnowledgeLibrary.objects.filter(pk=library.pk).exists()
    assert result["steps"]["originals"]["ok"] is True


def test_stale_generation_cleaned_only_after_full_success(
    admin_user, library, make_file, monkeypatch
):
    active = VectorCollection.objects.create(
        name=f"kb-{library.pk}-g9", dimension=1024, library=library, is_active=True
    )
    stale = VectorCollection.objects.create(
        name=f"kb-{library.pk}-g8", dimension=1024, library=library, is_active=False
    )
    _original(library, make_file, name="a.md")
    _prepare(admin_user, library)
    _ok_retry(monkeypatch)
    dropped: list[str] = []
    _spy_drop(monkeypatch, dropped)
    operation = OperationRecord.objects.get(intent="reset_library_index")
    result = services.execute_operation(admin_user, operation)["result"]

    assert dropped == [stale.name], "只允许清掉不再生效的旧代次"
    assert result["steps"]["vectors"]["ok"] is True
    assert not VectorCollection.objects.filter(pk=stale.pk).exists(), "登记表与现实不一致"
    assert VectorCollection.objects.filter(pk=active.pk).exists(), "清掉了活跃代次"


def test_incomplete_rebuild_never_cleans_stale_generations(
    admin_user, library, make_file, monkeypatch
):
    stale = VectorCollection.objects.create(
        name=f"kb-{library.pk}-g7", dimension=1024, library=library, is_active=False
    )
    _original(library, make_file, name="a.md")
    _prepare(admin_user, library)
    _ok_retry(monkeypatch, ok=False)  # 跑完但 step.ok 为假
    dropped: list[str] = []
    _spy_drop(monkeypatch, dropped)
    operation = OperationRecord.objects.get(intent="reset_library_index")
    result = services.execute_operation(admin_user, operation)["result"]

    assert result["complete"] is False
    assert result["incomplete"] == 1
    assert dropped == [], "未全部重建成功时不许清旧代次"
    assert VectorCollection.objects.filter(pk=stale.pk).exists()
    assert "旧代次" in result["note"]


def test_unrebuildable_record_is_reported_and_blocks_cleanup(
    admin_user, library, make_file, monkeypatch
):
    """缺原件的资料**不许冒充可重建**：如实列入 unrebuildable，并阻止清理。"""
    stale = VectorCollection.objects.create(
        name=f"kb-{library.pk}-g6", dimension=1024, library=library, is_active=False
    )
    good = _original(library, make_file, name="good.md")
    # 历史记录：没有原件路径
    orphan = make_file(library=library, name="orphan.md")
    IngestedFile._base_manager.filter(pk=orphan.pk).update(raw_path="")
    _prepare(admin_user, library)
    calls: list[int] = []
    _ok_retry(monkeypatch, calls)
    dropped: list[str] = []
    _spy_drop(monkeypatch, dropped)
    operation = OperationRecord.objects.get(intent="reset_library_index")
    result = services.execute_operation(admin_user, operation)["result"]

    assert result["complete"] is False
    assert calls == [good.pk], "只重建能重建的"
    assert [item["id"] for item in result["unrebuildable"]] == [orphan.pk]
    assert "没有原件路径" in result["unrebuildable"][0]["reason"]
    assert dropped == []
    assert VectorCollection.objects.filter(pk=stale.pk).exists()
    assert IngestedFile._base_manager.filter(pk=orphan.pk).exists(), "不许顺手删资料"


def test_limit_reports_remaining_without_cleanup(
    admin_user, library, make_file, monkeypatch
):
    _original(library, make_file, name="a.md", text="甲")
    _original(library, make_file, name="b.md", text="乙")
    _prepare(admin_user, library)
    monkeypatch.setattr(services, "RESET_LIBRARY_LIMIT", 1)
    calls: list[int] = []
    _ok_retry(monkeypatch, calls)
    dropped: list[str] = []
    _spy_drop(monkeypatch, dropped)
    operation = OperationRecord.objects.get(intent="reset_library_index")
    result = services.execute_operation(admin_user, operation)["result"]

    assert len(calls) == 1
    assert result["remaining"] == 1
    assert result["complete"] is False
    assert dropped == []


def test_empty_library_reports_completed(admin_user, library, monkeypatch):
    _prepare(admin_user, library)
    _ok_retry(monkeypatch)
    dropped: list[str] = []
    _spy_drop(monkeypatch, dropped)
    operation = OperationRecord.objects.get(intent="reset_library_index")
    result = services.execute_operation(admin_user, operation)["result"]
    assert result["complete"] is True
    assert result["checked"] == 0
    assert result["rebuilt"] == 0


def test_audit_events_record_authorization_and_outcome(
    admin_user, library, make_file, monkeypatch
):
    _original(library, make_file, name="a.md")
    _prepare(admin_user, library)
    _ok_retry(monkeypatch)
    _spy_drop(monkeypatch, [])
    operation = OperationRecord.objects.get(intent="reset_library_index")
    services.execute_operation(admin_user, operation)

    authorized = AuditEvent.objects.filter(
        action="knowledge.library_index_reset_authorized"
    ).first()
    assert authorized is not None
    assert authorized.payload["snapshot"]["code"] == library.code
    done = AuditEvent.objects.filter(action="knowledge.library_index_reset").first()
    assert done is not None
    assert done.payload["complete"] is True


def test_other_library_is_not_touched(admin_user, make_file, make_library, monkeypatch):
    target = make_library(code="reset-target", name="目标库")
    other = make_library(code="reset-other", name="其它库")
    other_file = _original(other, make_file, name="keep.md")
    _original(target, make_file, name="target.md")
    _prepare(admin_user, target)
    calls: list[int] = []
    _ok_retry(monkeypatch, calls)
    _spy_drop(monkeypatch, [])
    operation = OperationRecord.objects.get(intent="reset_library_index")
    services.execute_operation(admin_user, operation)

    assert other_file.pk not in calls, "重置越界到别的库了"
    assert IngestedFile._base_manager.filter(pk=other_file.pk).exists()
    assert DocumentChunk.objects.filter(file=other_file).count() == 1


# ------------------------------------------------- 真实流水线（非 mock） ----


class _KeepAliveClient:
    """转发一切调用，但 ``close()`` 不真的关。

    为什么需要它：``process_file`` 在 ``client=None`` 时认为"客户端是我开的"，
    收尾会 ``close()``；而测试夹具的工厂返回的是**全测试共享**的同一个客户端，
    真关掉之后就没法再核验向量了。生产里工厂每次返回新客户端，关掉是对的，
    所以这里只屏蔽 close，不改变被测量的逻辑。
    """

    def __init__(self, inner):
        self._inner = inner

    def close(self) -> None:  # noqa: D102 - 故意不做任何事
        return None

    def __getattr__(self, item):
        return getattr(self._inner, item)


def test_real_pipeline_rebuild_keeps_vectors_and_records(
    admin_user, library, make_file, isolated_paths, library_setup_vector
):
    """**真实**跑一遍：真原件 → 真解析/真分段 → 真 Qdrant 写入 → 受控重置重建。

    前面那些用例把 ``retry_file`` 换成假的，是为了锁定"分类与清理口径"；
    这一条**不换任何东西**（离线适配器的确定性假向量 + 真 Qdrant），
    证明重置走的就是生产那条 ``retry_file(force=True)`` 路径。
    """
    from apps.knowledge import pipeline, vectorindex
    from apps.knowledge import pipeline as pipeline_module
    from apps.knowledge import testing_adapter as ta
    from apps.knowledge.embedding import active_profile

    client = library_setup_vector
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

    body = "# 重置索引真实用例\n\n质保期：设备验收合格之日起 12 个月。\n".encode()
    stored = storage.store_bytes(body)
    record = make_file(
        library=library,
        name="real-reset.md",
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
    record.refresh_from_db()
    assert step.ok is True, step.detail
    assert record.parse_status == ParseStatus.DONE
    assert record.vector_status == "indexed"
    points_before = record.chunk_count
    assert points_before > 0
    assert vectorindex.count_points(client, collection) == points_before
    chunks_before = DocumentChunk.objects.filter(file=record).count()
    raw_before = storage.read_blob(record.content_sha256)

    _prepare(admin_user, library)
    operation = OperationRecord.objects.get(intent="reset_library_index")
    original = pipeline_module.QDRANT_CLIENT_FACTORY
    pipeline_module.QDRANT_CLIENT_FACTORY = lambda: _KeepAliveClient(client)
    try:
        result = services.execute_operation(admin_user, operation)["result"]
    finally:
        pipeline_module.QDRANT_CLIENT_FACTORY = original

    assert result["complete"] is True, result
    assert result["rebuilt"] == 1, result
    record.refresh_from_db()
    assert record.parse_status == ParseStatus.DONE
    assert record.vector_status == "indexed"
    assert vectorindex.count_points(client, collection) == points_before, "点数变了"
    assert DocumentChunk.objects.filter(file=record).count() == chunks_before
    assert storage.read_blob(record.content_sha256) == raw_before, "原件字节被改"
    assert KnowledgeLibrary.objects.filter(pk=library.pk).exists()
