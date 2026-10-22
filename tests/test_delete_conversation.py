"""删除会话（受控操作）的回归。

验收点：
  * **只能删自己的**：别人不能凭会话 id 删你的会话；系统管理员可以；
  * 走 planner 受控流程（提案 → 确认 → 执行），**没有**不受控的 destroy 端点；
  * 删除会话时聊天消息一并清除；
  * **绝不销毁审计**：`OperationRecord` / `ActionTicket` 是 CASCADE 到会话的，
    删会话前必须**解除关联并保留记录**（否则审计链断裂）；
  * 派生文档（SET_NULL）不受影响；
  * 重复执行被拒；删除留审计事件；
  * 目标身份含创建时间戳（防同号串号）。
"""

from __future__ import annotations

import pytest

from apps.audit.models import AuditEvent
from apps.planner import services
from apps.planner.models import ActionTicket, Conversation, Message, OperationRecord

pytestmark = pytest.mark.django_db


def _conversation(user, title="测试会话", messages=2):
    conversation = Conversation.objects.create(user=user, title=title)
    for index in range(messages):
        Message.objects.create(
            conversation=conversation, role="user", content=f"消息 {index}"
        )
    return conversation


def _propose(user, conversation):
    proposal = services.propose_operation(
        user, intent="delete_conversation", params={"conversation": conversation.pk}
    )
    operation = OperationRecord.objects.get(pk=proposal["operation_id"])
    return proposal, operation


def _arm_and_execute(user, proposal, operation):
    services.confirm_operation(user, operation, token=proposal["confirm_token"])
    operation.refresh_from_db()
    return services.execute_operation(user, operation)["result"]


def test_owner_deletes_own_conversation_with_messages(admin_user):
    conversation = _conversation(admin_user, title="要删的会话", messages=3)
    proposal, operation = _propose(admin_user, conversation)
    assert proposal["required_confirmations"] == 1, "会话删除是一次确认，不是三次"
    result = _arm_and_execute(admin_user, proposal, operation)

    assert result["deleted"] == "conversation"
    assert result["message_count"] == 3
    assert result["complete"] is True
    assert not Conversation.objects.filter(pk=conversation.pk).exists()
    assert Message.objects.filter(conversation_id=conversation.pk).count() == 0, "聊天消息残留"


def test_other_user_cannot_delete_someone_elses_conversation(user, other_user):
    conversation = _conversation(other_user, title="别人的会话")
    with pytest.raises(services.PlannerError) as exc:
        services.propose_operation(
            user, intent="delete_conversation", params={"conversation": conversation.pk}
        )
    assert "自己" in str(exc.value)
    assert Conversation.objects.filter(pk=conversation.pk).exists()


def test_system_admin_can_delete_any_conversation(admin_user, user):
    conversation = _conversation(user, title="普通用户的会话")
    proposal, operation = _propose(admin_user, conversation)
    result = _arm_and_execute(admin_user, proposal, operation)
    assert result["deleted"] == "conversation"
    assert not Conversation.objects.filter(pk=conversation.pk).exists()


def test_deleting_conversation_preserves_operation_audit(admin_user):
    """**核心保护**：会话删除不得级联销毁受控操作记录与凭证。"""
    conversation = _conversation(admin_user, title="含操作的会话", messages=1)
    # 造一条"该会话下的操作记录"（模拟用户此前在这个会话里确认/执行过操作）
    kept = OperationRecord.objects.create(
        conversation=conversation,
        user=admin_user,
        intent="list_files",
        status=OperationRecord.Status.EXECUTED,
        params={},
        target_ref="x",
        target_label="x",
    )
    from django.utils import timezone

    kept_ticket = ActionTicket.objects.create(
        conversation=conversation,
        user=admin_user,
        scope=ActionTicket.Scope.CONFIRM_STEP_1,
        intent="list_files",
        object_ref="x",
        token_hash="deadbeef",
        expires_at=timezone.now() + timezone.timedelta(minutes=10),
    )

    proposal, operation = _propose(admin_user, conversation)
    result = _arm_and_execute(admin_user, proposal, operation)

    assert result["detached_operations"] == 1
    assert result["detached_tickets"] == 1
    # **记录本身必须还在**（只是解除了与会话的关联）
    kept.refresh_from_db()
    assert kept.conversation_id is None, "操作记录未解除关联"
    assert OperationRecord.objects.filter(pk=kept.pk).exists(), "操作记录被级联删除了！"
    kept_ticket.refresh_from_db()
    assert kept_ticket.conversation_id is None
    assert ActionTicket.objects.filter(pk=kept_ticket.pk).exists(), "凭证被级联删除了！"
    assert "保留" in result["note"]


def test_derived_documents_survive_conversation_deletion(admin_user):
    """派生文档是 SET_NULL：会话删除后文献仍在。"""
    from apps.planner.models import DerivedDocument

    conversation = _conversation(admin_user, title="有文档的会话")
    document = DerivedDocument.objects.create(
        conversation=conversation, created_by=admin_user, title="派生文档",
        content_markdown="正文",
    )
    proposal, operation = _propose(admin_user, conversation)
    _arm_and_execute(admin_user, proposal, operation)
    document.refresh_from_db()
    assert document.conversation_id is None
    assert DerivedDocument.objects.filter(pk=document.pk).exists(), "派生文档被误删"


def test_repeat_execute_is_refused(admin_user):
    conversation = _conversation(admin_user)
    proposal, operation = _propose(admin_user, conversation)
    _arm_and_execute(admin_user, proposal, operation)
    operation.refresh_from_db()
    with pytest.raises(services.PlannerError):
        services.execute_operation(admin_user, operation)


def test_deletion_is_audited(admin_user):
    conversation = _conversation(admin_user, title="审计会话", messages=2)
    proposal, operation = _propose(admin_user, conversation)
    _arm_and_execute(admin_user, proposal, operation)
    event = AuditEvent.objects.filter(action="conversation.deleted").order_by("-id").first()
    assert event is not None, "删除会话没有审计事件"
    assert event.actor_id == admin_user.pk
    assert event.resource_key == f"conversation:{conversation.pk}"
    assert event.payload.get("message_count") == 2


def test_target_identity_includes_creation_stamp(admin_user):
    """目标身份带创建时间戳：同号会话不会串号（防"删错会话"）。"""
    conversation = _conversation(admin_user)
    proposal = services.propose_operation(
        admin_user, intent="delete_conversation", params={"conversation": conversation.pk}
    )
    assert proposal["target_ref"].startswith(f"conversation:{conversation.pk}:")
    assert proposal["target_label"] == conversation.title


def test_missing_conversation_is_reported(admin_user):
    with pytest.raises(services.PlannerError) as exc:
        services.propose_operation(
            admin_user, intent="delete_conversation", params={"conversation": 999999}
        )
    assert "会话不存在" in str(exc.value)


def test_no_uncontrolled_destroy_endpoint(auth_api, admin_user):
    """**没有**不受控的会话 destroy 端点（必须走受控流程）。"""
    client = auth_api(admin_user)
    conversation = _conversation(admin_user)
    url = f"/api/v1/planner/conversations/{conversation.pk}/"
    for method in ("delete", "put", "patch"):
        response = getattr(client, method)(url, {}, content_type="application/json")
        assert response.status_code in (403, 404, 405), (
            f"{method.upper()} {url} → {response.status_code}（不应存在不受控删除入口）"
        )
    assert Conversation.objects.filter(pk=conversation.pk).exists()
