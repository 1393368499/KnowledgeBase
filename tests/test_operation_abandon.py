"""放弃待办操作（工具栏"待办事项"的删除按钮）的回归。

验收点：
  * 放弃 = 状态推进到既有终态 ``rejected``（界面"已放弃"），**不是** REST 删除；
  * **不销毁审计**：``OperationRecord`` 本身必须还在（否则等于抹掉"曾提案要删资料"的历史）；
  * 放弃后**执行被拒**（执行端只认 ARMED），旧确认凭证不能再推进；
  * 放弃后不再出现在默认操作列表（待办列表里"消失"），但 ``?status=rejected`` 仍可查历史；
  * **只能放弃自己的**：别人的操作连存在性都探不到（404）；
  * 不能放弃已结束的、也不能放弃正在执行的；
  * 没有不受控 destroy 端点（DELETE/PUT/PATCH 均非 2xx）；
  * 留审计事件 ``planner.operation_abandoned``。
"""

from __future__ import annotations

import pytest

from apps.audit.models import AuditEvent
from apps.knowledge.models import MemberRole
from apps.planner import services
from apps.planner.models import OperationRecord

pytestmark = pytest.mark.django_db


@pytest.fixture
def pending_op(library, make_file, grant_member):
    """登记一张真实的待办（删除资料：破坏性、要三次确认，最像用户会想放弃的卡）。

    授予库管理员是为了让**普通用户**（非系统管理员）也能提案删除资料——
    "放弃自己的待办"不该只有超管能做。
    """

    def _make(user, name="准备放弃的资料.md"):
        grant_member(library, user, role=MemberRole.ADMIN)
        record = make_file(library, name=name)
        proposal = services.propose_operation(
            user, intent="delete_file", params={"file_id": record.pk}
        )
        operation = OperationRecord.objects.get(pk=proposal["operation_id"])
        return proposal, operation

    return _make


def _abandon_url(pk: int) -> str:
    return f"/api/v1/planner/operations/{pk}/abandon/"


def test_abandon_marks_rejected_and_keeps_record(user, pending_op):
    """放弃后状态是 rejected，且**操作记录仍在**（审计不销毁）。"""
    _, operation = pending_op(user)
    payload = services.abandon_operation(user, operation)

    assert payload["status"] == OperationRecord.Status.REJECTED
    assert payload["previous_status"] == OperationRecord.Status.PROPOSED
    operation.refresh_from_db()
    assert operation.status == OperationRecord.Status.REJECTED
    assert OperationRecord.objects.filter(pk=operation.pk).exists(), "操作记录被删掉了（审计被销毁）"
    assert operation.result["abandoned"] is True
    assert operation.result["previous_status"] == "proposed"


def test_abandon_clears_armed_window(user, pending_op):
    """放弃一张已确认待执行的卡：执行窗口必须被清掉，不留"待执行"残留。"""
    proposal, operation = pending_op(user)
    token = proposal["confirm_token"]
    for _ in range(3):
        payload = services.confirm_operation(
            user, operation, token=token, typed_name=operation.target_label
        )
        token = payload.get("next_confirm_token", token)
        operation.refresh_from_db()
    assert operation.status == OperationRecord.Status.ARMED

    services.abandon_operation(user, operation)
    operation.refresh_from_db()
    assert operation.status == OperationRecord.Status.REJECTED
    assert operation.armed_at is None
    assert operation.armed_expires_at is None


def test_abandon_writes_audit_event(user, pending_op):
    """留审计事件，payload 说明从哪个状态放弃的。"""
    _, operation = pending_op(user)
    services.abandon_operation(user, operation)

    event = AuditEvent.objects.filter(action="planner.operation_abandoned").first()
    assert event is not None, "放弃操作没有留审计事件"
    assert event.resource_key == str(operation.pk)
    assert event.actor_id == user.pk
    assert event.payload["previous_status"] == "proposed"
    assert event.payload["intent"] == "delete_file"


def test_execute_after_abandon_is_refused(user, pending_op):
    """放弃后不能再执行（用户放弃的必须真的停住，不能旧凭证继续跑）。"""
    proposal, operation = pending_op(user)
    services.confirm_operation(user, operation, token=proposal["confirm_token"])
    operation.refresh_from_db()
    services.abandon_operation(user, operation)
    operation.refresh_from_db()

    with pytest.raises(services.PlannerError):
        services.execute_operation(user, operation)


def test_confirm_after_abandon_is_refused(user, pending_op):
    """放弃是终态，不能再继续确认。"""
    proposal, operation = pending_op(user)
    services.abandon_operation(user, operation)
    operation.refresh_from_db()

    with pytest.raises(services.PlannerError):
        services.confirm_operation(user, operation, token=proposal["confirm_token"])


def test_double_abandon_is_refused(user, pending_op):
    """重复放弃被拒（终态不可再推进）。"""
    _, operation = pending_op(user)
    services.abandon_operation(user, operation)
    operation.refresh_from_db()
    with pytest.raises(services.PlannerError):
        services.abandon_operation(user, operation)


def test_other_user_cannot_abandon(user, other_user, pending_op):
    """别人的操作连存在性都探不到（服务层直接拒）。"""
    _, operation = pending_op(user)
    with pytest.raises(services.PlannerError):
        services.abandon_operation(other_user, operation)
    operation.refresh_from_db()
    assert operation.status == OperationRecord.Status.PROPOSED, "被别人放弃了"


def test_abandon_endpoint_requires_ownership(auth_api, user, other_user, pending_op):
    """HTTP 层：拿别人的操作 id 打放弃接口 → 404（不是 200，也不泄露存在性）。"""
    _, operation = pending_op(user)
    client = auth_api(other_user)
    response = client.post(_abandon_url(operation.pk), {}, format="json")
    assert response.status_code == 404
    operation.refresh_from_db()
    assert operation.status == OperationRecord.Status.PROPOSED


def test_abandon_endpoint_works_for_owner(auth_api, user, pending_op):
    """HTTP 层：本人放弃成功，返回体说清"未执行任何改动"。"""
    _, operation = pending_op(user)
    response = auth_api(user).post(_abandon_url(operation.pk), {}, format="json")
    assert response.status_code == 200, response.content
    assert response.data["status"] == "rejected"
    assert "未执行任何改动" in response.data["detail"]
    operation.refresh_from_db()
    assert operation.status == OperationRecord.Status.REJECTED


def test_endpoint_rejects_rest_destroy_verbs(auth_api, user, pending_op):
    """没有不受控 destroy 端点：DELETE/PUT/PATCH 一律非 2xx。"""
    _, operation = pending_op(user)
    client = auth_api(user)
    for method in ("delete", "put", "patch"):
        response = getattr(client, method)(_abandon_url(operation.pk), {}, format="json")
        assert response.status_code >= 400, f"{method.upper()} 竟然成功了：{response.status_code}"


def test_endpoint_requires_authentication(api, user, pending_op):
    """未登录不能放弃。"""
    _, operation = pending_op(user)
    response = api.post(_abandon_url(operation.pk), {}, format="json")
    assert response.status_code in (401, 403)


def test_abandoned_operation_leaves_pending_list_but_stays_in_history(auth_api, user, pending_op):
    """放弃后：默认列表（待办）不再返回它，`?status=rejected` 仍能查到（历史可查）。"""
    _, operation = pending_op(user)
    client = auth_api(user)
    services.abandon_operation(user, operation)

    listing = client.get("/api/v1/planner/operations/")
    ids = [row["id"] for row in listing.data["results"]]
    assert operation.pk not in ids, "已放弃的操作仍出现在默认列表里（界面上的待办不会消失）"

    history = client.get("/api/v1/planner/operations/?status=rejected")
    history_ids = [row["id"] for row in history.data["results"]]
    assert operation.pk in history_ids, "已放弃的操作查不到了（历史丢失）"


def test_executing_operation_cannot_be_abandoned(user, pending_op):
    """正在执行的操作不能放弃（否则与执行中的写入竞态）。"""
    _, operation = pending_op(user)
    OperationRecord.objects.filter(pk=operation.pk).update(status="executing")
    operation.refresh_from_db()
    with pytest.raises(services.PlannerError):
        services.abandon_operation(user, operation)


def test_terminal_operation_cannot_be_abandoned(user, pending_op):
    """已执行的操作不能再"放弃"（它是终态）。"""
    _, operation = pending_op(user)
    OperationRecord.objects.filter(pk=operation.pk).update(status="executed")
    operation.refresh_from_db()
    with pytest.raises(services.PlannerError):
        services.abandon_operation(user, operation)


# ---------------------------------------------------------------------------
# "执行中"卡死：进程被 SIGKILL / 容器重启会跳过"写回结果"那一步。
# executing 不是终态、再执行又被 CAS（要求 ARMED）挡住 —— 没有超时收尾，
# 这条待办会**永久**显示"执行中"，用户既不能放弃也不能重试。
# ---------------------------------------------------------------------------


def _make_executing(operation: OperationRecord, *, age_seconds: int) -> None:
    from datetime import timedelta

    from django.utils import timezone

    OperationRecord.objects.filter(pk=operation.pk).update(
        status="executing",
        updated_at=timezone.now() - timedelta(seconds=age_seconds),
    )
    operation.refresh_from_db()


def test_stale_executing_operation_can_be_abandoned(user, pending_op):
    """僵死的"执行中"必须能放弃（新的"执行中"仍然不能）。"""
    _, operation = pending_op(user)
    _make_executing(operation, age_seconds=OperationRecord.EXECUTING_STALE_SECONDS + 60)
    assert operation.executing_is_stale() is True

    result = services.abandon_operation(user, operation)

    assert result["status"] == OperationRecord.Status.REJECTED
    assert result["stale_executing"] is True
    assert "没有写回结果" in result["detail"]
    operation.refresh_from_db()
    assert operation.result["stale_executing"] is True
    assert operation.armed_at is None and operation.armed_expires_at is None
    event = AuditEvent.objects.filter(action="planner.operation_abandoned").latest("id")
    assert event.payload["stale_executing"] is True


def test_sweep_converges_stale_executing_but_not_the_live_one(
    user, library, make_file, pending_op
):
    """周期扫尾把僵死的 executing 收敛为 failed；真正在执行的那条不动。"""
    from knowledgebase.celery import converge_stale_executing_operations

    _, stale = pending_op(user, name="僵死.md")
    # 第二张待办直接自己提案：pending_op 会再授一次库成员（同一 user+library 唯一约束）
    other = make_file(library, "还在跑.md")
    proposal = services.propose_operation(
        user, intent="delete_file", params={"file_id": other.pk}
    )
    live = OperationRecord.objects.get(pk=proposal["operation_id"])

    _make_executing(stale, age_seconds=OperationRecord.EXECUTING_STALE_SECONDS + 60)
    _make_executing(live, age_seconds=5)

    recovery = converge_stale_executing_operations()

    assert recovery["operation_ids"] == [stale.pk], recovery
    stale.refresh_from_db()
    live.refresh_from_db()
    assert stale.status == OperationRecord.Status.FAILED
    assert "重新发起提案" in stale.error
    assert live.status == OperationRecord.Status.EXECUTING, "把正在执行的也收敛了"
    assert AuditEvent.objects.filter(action="planner.operation_converged_stale").count() == 1

    # 收敛后它出现在失败历史里，不再占着"待办"位置
    assert OperationRecord.objects.filter(pk=stale.pk, status="failed").exists()
