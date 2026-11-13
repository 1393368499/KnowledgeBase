from unittest.mock import patch

import pytest
from apps.planner.models import Message
from apps.voice import services
from apps.voice.models import VoiceTurn
from tests.test_voice_devices import added, upload, manager

pytestmark = pytest.mark.django_db


def test_model_timeout_is_durable_system_feedback_and_readable_on_board(user):
    device, api = added(user)
    result = upload(api, device)
    turn = VoiceTurn.objects.get(pk=result.data["task_id"])
    # Exercise the real planner's persisted user link, then fail the model call.
    with patch("apps.voice.services.transcribe", return_value="查找资料"), patch("apps.planner.services.LlmClient") as llm:
        llm.return_value.chat.side_effect = RuntimeError("timed out")
        services.process_turn(turn.pk)
    turn.refresh_from_db()
    assert turn.state == "uncertain" and turn.user_message_id
    conversation = turn.conversation
    assert conversation is not None
    rows = Message.objects.filter(conversation=conversation, role="system")
    assert rows.count() == 1
    assert "AI 模型服务超时" in rows.get().content
    assert not Message.objects.filter(conversation=conversation, role="assistant").exists()
    services.record_failure_notice(turn)
    services.process_turn(turn.pk)
    assert rows.count() == 1
    history = manager(user).get(f"/api/v1/planner/conversations/{turn.conversation_id}/")
    assert any(m["role"] == "system" and "超时" in m["content"] for m in history.data["messages"])
    state = api.get(f"/api/v1/voice/device/turns/{turn.client_turn_id}/")
    assert state.data["answer_page_count"] > 0
    page = api.get(f"/api/v1/voice/device/turns/{turn.client_turn_id}/pages/answer/batch/0/")
    assert page.status_code == 200 and len(page.content) >= 15000
    assert api.post(f"/api/v1/voice/device/turns/{turn.client_turn_id}/retry/").status_code == 409


def test_canceled_turn_never_gets_failure_notice(user):
    device, api = added(user)
    result = upload(api, device)
    turn = VoiceTurn.objects.get(pk=result.data["task_id"])
    services.cancel_turn(turn.device, turn.client_turn_id)
    services.record_failure_notice(turn)
    conversation = turn.conversation
    assert conversation is not None
    assert not Message.objects.filter(conversation=conversation).exists()
