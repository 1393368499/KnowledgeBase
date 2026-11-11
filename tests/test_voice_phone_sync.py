import uuid
import pytest
from apps.planner.models import Conversation, Message, DerivedDocument
from apps.voice.models import VoiceTurn
from tests.test_voice_task_sessions import added

pytestmark = pytest.mark.django_db


def task(user, data):
    conversation = Conversation.objects.create(user=user, title="原任务")
    VoiceTurn.objects.create(device_id=data["id"], client_turn_id=uuid.uuid4(),
        conversation=conversation, config_version=1, scope_mode="all_authorized",
        state="answer_ready", answer="旧回答")
    return conversation


def test_phone_sees_web_title_messages_documents_and_deletion(user, other_user):
    data, api = added(user)
    conversation = task(user, data)
    conversation.title = "电脑端重命名"
    conversation.save()
    Message.objects.create(conversation=conversation, role="assistant", content="网页后来追加的回复")
    own = DerivedDocument.objects.create(created_by=user, conversation=conversation, title="已保存报告")
    DerivedDocument.objects.create(created_by=other_user, conversation=conversation, title="不可见文档")
    feed = api.get("/api/v1/voice/device/tasks/").data
    assert feed["device_id"] == data["id"] and feed["server_time"]
    assert feed["results"][0]["title"] == "电脑端重命名"
    result = api.get(f"/api/v1/voice/device/tasks/{conversation.pk}/")
    assert result.status_code == 200
    assert result.data["messages"][0]["content"] == "网页后来追加的回复"
    assert [d["id"] for d in result.data["documents"]] == [own.pk]
    old_id = conversation.pk
    conversation.delete()
    assert api.get("/api/v1/voice/device/tasks/").data["results"] == []
    assert api.get(f"/api/v1/voice/device/tasks/{old_id}/").status_code == 404


def test_task_sync_is_scoped_and_payload_is_bounded(user):
    data, api = added(user)
    _, second = added(user)
    conversation = task(user, data)
    for _ in range(8):
        Message.objects.create(conversation=conversation, role="assistant", content="内容" * 20000)
    result = api.get(f"/api/v1/voice/device/tasks/{conversation.pk}/")
    assert len(result.data["messages"]) == 6
    assert all(m["truncated"] and len(m["content"]) == 2000 for m in result.data["messages"])
    assert len(result.content) <= 120000
    assert second.get(f"/api/v1/voice/device/tasks/{conversation.pk}/").status_code == 404
