import uuid
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from rest_framework.test import APIClient
from apps.planner import services as planner
from apps.planner.models import Conversation, Message, OperationRecord, DerivedDocument
from apps.voice.models import VoiceTurn
from apps.voice import services
from tests.test_voice_devices import manager, upload

pytestmark = pytest.mark.django_db


def added(user):
    result = manager(user).post("/api/v1/voice/devices/", {"name": "任务选择板"}, format="json")
    assert result.status_code == 201
    api = APIClient()
    api.credentials(HTTP_AUTHORIZATION="Bearer " + result.data["device_token"])
    return result.data, api


def select(api, conversation=None):
    result = api.post("/api/v1/voice/device/task-selection/", {"conversation_id": conversation}, format="json")
    assert result.status_code == 200, result.data
    return result.data["route_version"]


def test_two_recordings_in_one_task_keep_context_and_order(user):
    d, api = added(user)
    a = upload(api, d, route_version=1)
    b = upload(api, d, route_version=1)
    assert a.status_code == b.status_code == 202
    assert a.data["conversation_id"] == b.data["conversation_id"]
    second = VoiceTurn.objects.get(pk=b.data["task_id"])
    with patch.object(services, "transcribe") as transcribe:
        services.process_turn(second.pk)
        transcribe.assert_not_called()
    seen = []
    def model(messages, **kwargs):
        seen.append(messages)
        return SimpleNamespace(content="已处理补充", model="test", usage={}, tool_calls=[])
    with patch.object(services, "transcribe", side_effect=["第一条任务内容", "第二条补充"]), patch("apps.planner.services.LlmClient") as llm:
        llm.return_value.chat.side_effect = model
        services.process_turn(a.data["task_id"])
        services.process_turn(b.data["task_id"])
    assert len(seen) == 2
    assert any(m["role"] == "assistant" and m["content"] == "已处理补充" for m in seen[1])
    assert Conversation.objects.count() == 1
    assert Message.objects.filter(conversation_id=a.data["conversation_id"], role="user").count() == 2
    tasks = api.get("/api/v1/voice/device/tasks/").data
    assert len(tasks["results"]) == 1 and tasks["results"][0]["recording_count"] == 2


def test_switch_during_capture_keeps_the_recording_in_its_original_task(user):
    d, api = added(user)
    a = upload(api, d, route_version=1)
    next_route = select(api)
    old_capture = upload(api, d, route_version=1)
    b = upload(api, d, route_version=next_route)
    assert old_capture.data["conversation_id"] == a.data["conversation_id"]
    assert b.data["conversation_id"] != a.data["conversation_id"]
    route = select(api, a.data["conversation_id"])
    again = upload(api, d, route_version=route)
    assert again.data["conversation_id"] == a.data["conversation_id"]
    assert Conversation.objects.count() == 2
    assert api.get("/api/v1/voice/device/tasks/").data["active_count"] == 2


def test_four_limit_counts_tasks_not_recordings(user):
    d, api = added(user)
    for _ in range(4):
        assert upload(api, d, route_version=1).status_code == 202
    assert api.get("/api/v1/voice/device/tasks/").data["active_count"] == 1
    for _ in range(3):
        route = select(api)
        assert upload(api, d, route_version=route).status_code == 202
    route = select(api)
    assert upload(api, d, route_version=route).status_code == 429
    assert Conversation.objects.count() == 4


def test_menu_and_selection_are_device_scoped(user):
    d, api = added(user)
    other, second = added(user)
    a = upload(api, d, route_version=1)
    conversation = a.data["conversation_id"]
    assert second.post("/api/v1/voice/device/task-selection/", {"conversation_id": conversation}, format="json").status_code == 400
    assert second.get(f"/api/v1/voice/device/tasks/{conversation}/").status_code == 404
    frame = api.get("/api/v1/voice/device/task-menu/1/")
    assert frame.status_code == 200 and len(frame.content) == 15000
    assert int(frame["X-Menu-Conversation"]) == conversation
    assert int(frame["X-Menu-Count"]) == 2
    assert api.get("/api/v1/voice/device/task-home/").status_code == 200
    assert api.get("/api/v1/voice/device/task-menu/999/").status_code == 404


def test_rename_is_immediate_and_delete_requires_confirmation_and_preserves_documents(user):
    d, api = added(user)
    response = upload(api, d, route_version=1)
    turn = VoiceTurn.objects.get(pk=response.data["task_id"])
    conversation = turn.conversation_id
    root = f"/api/v1/voice/device/tasks/{conversation}/"
    assert api.post(root + "delete/", {"action": "prepare"}, format="json").status_code == 400
    assert not OperationRecord.objects.exists()
    result = api.post(root + "rename/", {"title": "出差报告补充"}, format="json")
    assert result.status_code == 200 and not OperationRecord.objects.exists()
    assert Conversation.objects.get(pk=conversation).title == "出差报告补充"
    turn.state = "answer_ready"
    turn.transcript = "测试问题"
    turn.answer = "测试回答"
    turn.save()
    doc = DerivedDocument.objects.create(created_by=user, conversation_id=conversation, title="保留的文档")
    proposal = api.post(root + "delete/", {"action": "prepare"}, format="json")
    assert proposal.status_code == 200 and proposal.data["confirm_token"]
    assert Conversation.objects.filter(pk=conversation).exists()
    params = {"action": "confirm", "operation_id": proposal.data["operation_id"], "confirm_token": "wrong"}
    assert api.post(root + "delete/", params, format="json").status_code == 400
    params["confirm_token"] = proposal.data["confirm_token"]
    result = api.post(root + "delete/", params, format="json")
    assert result.status_code == 200, result.data
    assert not Conversation.objects.filter(pk=conversation).exists()
    turn.refresh_from_db()
    assert turn.state == "removed" and not turn.transcript and not turn.answer
    assert api.get(f"/api/v1/voice/device/turns/{turn.client_turn_id}/").status_code == 404
    assert upload(api, d, turn.client_turn_id, route_version=1).status_code == 410
    assert upload(api, d, route_version=1).status_code == 400
    doc.refresh_from_db()
    assert doc.conversation_id is None
    assert OperationRecord.objects.get(pk=proposal.data["operation_id"]).status == "executed"
    assert api.post(root + "delete/", params, format="json").status_code == 200  # Lost response is safe to repeat.


def test_abandoning_delete_keeps_task_and_rename_cannot_target_other_device(user):
    d, api = added(user)
    other, second = added(user)
    response = upload(api, d, route_version=1)
    turn = VoiceTurn.objects.get(pk=response.data["task_id"])
    turn.state = "canceled"
    turn.save()
    root = f"/api/v1/voice/device/tasks/{turn.conversation_id}/"
    assert second.post(root + "rename/", {"title": "篡改"}, format="json").status_code == 404
    p = api.post(root + "delete/", {"action": "prepare"}, format="json").data
    VoiceTurn.objects.create(
        device_id=other["id"], client_turn_id=uuid.uuid4(), conversation=turn.conversation,
        config_version=1, scope_mode="all_authorized", state="answer_ready",
    )
    assert second.get(root).status_code == 200
    assert second.post(root + "delete/", {
        "action": "abandon", "operation_id": p["operation_id"],
    }, format="json").status_code == 400
    web_proposal = planner.propose_operation(
        user, intent="delete_conversation", params={"conversation": turn.conversation_id},
        conversation=turn.conversation,
    )
    assert api.post(root + "delete/", {
        "action": "abandon", "operation_id": web_proposal["operation_id"],
    }, format="json").status_code == 400
    assert OperationRecord.objects.get(pk=web_proposal["operation_id"]).status == "proposed"
    result = api.post(root + "delete/", {"action": "abandon", "operation_id": p["operation_id"]}, format="json")
    assert result.status_code == 200 and Conversation.objects.filter(pk=turn.conversation_id).exists()
