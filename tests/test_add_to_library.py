"""`add_to_library` 受控复用（审查第 8 条）。

口径：在**目标库新增记录**、复用同一份内容寻址只读原件，
**不改源记录**（源归属/字节/状态不变）、**不产生第二份字节**。
明确负例（不伪造成功）：
  * 源/目标不存在、无权读源、无权写目标 → 明确报错；
  * 缺原件（历史记录 raw_path 为空）→ 明确报错，**不冒充可添加**；
  * 同库同内容指纹 → **幂等**返回既有记录（并发唯一约束冲突也不得 500）；
  * 返回值带**真实派发结果**（`dispatched`），不谎称已入队。
"""

from __future__ import annotations

import pytest

from apps.knowledge.models import IngestedFile, KnowledgeLibrary


def _armed(user, params):
    from django.utils import timezone

    from apps.planner.models import Conversation, OperationRecord
    from tests.planner_fixtures import require_spec as get_spec
    from apps.planner.services import _describe_target

    ref, label = _describe_target(get_spec("add_to_library"), params)
    conversation = Conversation.objects.create(user=user)
    return OperationRecord.objects.create(
        user=user,
        conversation=conversation,
        intent="add_to_library",
        params=params,
        target_ref=ref,
        target_label=label,
        status=OperationRecord.Status.ARMED,
        armed_at=timezone.now(),
        armed_expires_at=timezone.now() + timezone.timedelta(minutes=10),
    )


def _run(user, params):
    from apps.planner import services

    return services.execute_operation(user, _armed(user, params))["result"]


@pytest.fixture
def setup(db, user, make_library, make_file, grant_member):
    src_lib = make_library(code="src-lib", name="源库")
    dst_lib = make_library(code="dst-lib", name="目标库")
    grant_member(src_lib, user, role="admin")
    grant_member(dst_lib, user, role="admin")
    # 源资料：**没有原件**（模拟 id 14/15/16 这类历史记录）
    src = make_file(library=src_lib, name="历史资料.pdf")
    return src, src_lib, dst_lib


def test_missing_source_is_rejected(db, user, setup):
    from apps.planner.services import PlannerError

    _src, _src_lib, dst = setup
    with pytest.raises(PlannerError) as exc:
        _run(user, {"file_id": 999999, "library": dst.code})
    assert "999999" in str(exc.value)  # 语义：明确指出不存在的那一份


def test_missing_target_library_is_rejected(db, user, setup):
    from apps.planner.services import PlannerError

    src, _src_lib, _dst = setup
    with pytest.raises(PlannerError) as exc:
        _run(user, {"file_id": src.pk, "library": "no-such-lib"})
    assert "no-such-lib" in str(exc.value)


def test_unreadable_source_is_rejected(db, user, make_library, make_file):
    """普通用户读不到别人的资料 → 明确报错（不是静默跳过）。"""
    from apps.planner.services import PlannerError

    other = make_library(code="other-src", name="他人库")
    secret = make_file(library=other, name="secret.pdf")
    mine = make_library(code="my-dst", name="我的库")
    with pytest.raises(PlannerError) as exc:
        _run(user, {"file_id": secret.pk, "library": mine.code})
    assert "权限" in str(exc.value)  # 语义：越权被拒（通用守卫先给出明确报文）


def test_write_permission_required(db, user, make_library, make_file, grant_member):
    """可读源、但对目标库只有 reader 权限 → 必须拒绝写入。"""
    from apps.planner.services import PlannerError

    src_lib = make_library(code="w-src", name="源")
    grant_member(src_lib, user, role="admin")
    src = make_file(library=src_lib, name="a.pdf")
    dst_lib = make_library(code="w-dst", name="只读目标")
    grant_member(dst_lib, user, role="reader")
    with pytest.raises(PlannerError) as exc:
        _run(user, {"file_id": src.pk, "library": dst_lib.code})
    assert "权限" in str(exc.value)


def test_missing_blob_is_reported_not_faked(db, user, setup):
    """缺原件（历史记录）必须**明确报告**，不能假装添加成功。"""
    from apps.planner.services import PlannerError

    src, _src_lib, dst = setup
    with pytest.raises(PlannerError) as exc:
        _run(user, {"file_id": src.pk, "library": dst.code})
    assert "没有原件路径" in str(exc.value) or "原件" in str(exc.value)
    assert not IngestedFile._base_manager.filter(library=dst).exists(), "失败时不得留下记录"


def test_idempotent_when_target_already_has_same_hash(db, user, setup):
    """目标库已有同内容指纹 → 幂等返回既有记录，**不新增**。"""
    src, _src_lib, dst = setup
    _run(user, {"file_id": src.pk, "library": dst.code}) if False else None
    # 直接造一份"目标库已有同 sha"的记录（模拟并发冲突后的状态）
    existing = IngestedFile.objects.create(
        library=dst,
        original_name=src.original_name,
        content_sha256=src.content_sha256,
        size_bytes=src.size_bytes,
        parse_status="pending",
    )
    # 源缺原件会在"幂等检查"之前被拦吗？顺序是：幂等检查 → 原件检查。
    # 因此这里应先命中幂等分支，返回既有记录而不是报原件缺失。
    result = _run(user, {"file_id": src.pk, "library": dst.code})
    assert result["reused"] is True and result["idempotent"] is True, result
    assert result["file_id"] == existing.pk
    # **源记录未被改动**（归属与指纹保持）
    src.refresh_from_db()
    assert src.library_id != dst.pk, "源记录归属被改动，违反不可篡改约束"
    assert IngestedFile._base_manager.filter(library=dst).count() == 1, "不得新增第二份记录"


def test_source_record_and_bytes_unchanged(db, user, setup):
    """无论成功或失败，源记录的归属/指纹/状态都不得变化。"""
    src, _src_lib, dst = setup
    before = IngestedFile._base_manager.get(pk=src.pk)
    before_state = (before.library_id, before.content_sha256, before.parse_status)
    try:
        _run(user, {"file_id": src.pk, "library": dst.code})
    except Exception:  # noqa: BLE001 - 本用例只关心"源没被改"
        pass
    after = IngestedFile._base_manager.get(pk=src.pk)
    assert (after.library_id, after.content_sha256, after.parse_status) == before_state
    assert KnowledgeLibrary.objects.filter(pk=src.library_id).exists()
