import hashlib
import io
import uuid
import wave
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from tests.api_client import APIClient
from apps.planner.models import Conversation, Message
from apps.voice.models import VoiceDevice, VoiceTurn
from apps.voice import services

pytestmark = pytest.mark.django_db


def manager(user):
    api = APIClient()
    api.force_authenticate(user)
    return api


def added(user, name="书桌 Note 4"):
    response = manager(user).post("/api/v1/voice/devices/", {"name": name, "conversation_mode": "fixed"}, format="json")
    assert response.status_code == 201, response.data
    data = response.data
    api = APIClient()
    api.credentials(HTTP_AUTHORIZATION="Bearer " + data["device_token"])
    return data, api


def wav():
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(b"\x01\x00" * 8000)
    return out.getvalue()


def upload(api, d, turn_id=None, audio=None, route_version=None):
    from django.core.files.uploadedfile import SimpleUploadedFile
    raw = audio or wav()
    payload = {
        "file": SimpleUploadedFile("audio.wav", raw, "audio/wav"),
        "turn_id": str(turn_id or uuid.uuid4()), "config_version": d["config_version"],
        "audio_sha256": hashlib.sha256(raw).hexdigest(),
    }
    if route_version is not None:
        payload["route_version"] = route_version
    return api.post("/api/v1/voice/device/turns/", payload, format="multipart")


def test_add_multiple_devices_keeps_independent_identity(user):
    a, _ = added(user, "一号")
    b, _ = added(user, "二号")
    assert a["id"] != b["id"] and a["device_token"] != b["device_token"]
    assert a["conversation_id"] != b["conversation_id"]
    rows = manager(user).get("/api/v1/voice/devices/").data["results"]
    assert len(rows) == 2 and all("token_hash" not in r and "device_token" not in r for r in rows)


def test_device_token_cannot_access_management_or_general_chat(user):
    d, api = added(user)
    assert api.get("/api/v1/voice/devices/").status_code in (401, 403)
    assert api.post("/api/v1/planner/chat/", {"message": "hello"}).status_code in (401, 403)
    assert APIClient().get("/api/v1/voice/device/config/").status_code == 401


def test_other_user_cannot_change_or_read_device(user, other_user):
    d, _ = added(user)
    api = manager(other_user)
    assert api.get("/api/v1/voice/devices/").data["results"] == []
    assert api.patch(f'/api/v1/voice/devices/{d["id"]}/', {"name": "hijack"}, format="json").status_code == 404
    assert api.get("/api/v1/voice/events/?after=0").data["results"] == []


def test_disable_remove_and_rotate_do_not_affect_second_device(user):
    a, aa = added(user); b, bb = added(user)
    api = manager(user)
    response = api.patch(f'/api/v1/voice/devices/{a["id"]}/', {"enabled": False}, format="json")
    assert response.status_code == 200
    assert aa.get("/api/v1/voice/device/config/").status_code == 401
    assert bb.get("/api/v1/voice/device/config/").status_code == 200
    api.delete(f'/api/v1/voice/devices/{a["id"]}/')
    assert Conversation.objects.filter(pk=a["conversation_id"]).exists()
    rotated = api.post(f'/api/v1/voice/devices/{b["id"]}/rotate-token/').data["device_token"]
    assert bb.get("/api/v1/voice/device/config/").status_code == 401
    bb.credentials(HTTP_AUTHORIZATION="Bearer " + rotated)
    assert bb.get("/api/v1/voice/device/config/").status_code == 200


def test_audio_deduplication_busy_and_isolation(user):
    d, api = added(user)
    client_id = uuid.uuid4()
    first = upload(api, d, client_id)
    assert first.status_code == 202
    assert upload(api, d, client_id).data["task_id"] == first.data["task_id"]
    assert upload(api, d).status_code == 409
    raw = bytearray(wav()); raw[-1] = 2
    assert upload(api, d, client_id, bytes(raw)).status_code == 409
    other, b = added(user)
    assert b.get(f"/api/v1/voice/device/turns/{client_id}/").status_code == 404
    assert VoiceTurn.objects.count() == 1


def test_invalid_audio_and_changed_config_do_not_queue(user):
    d, api = added(user)
    assert upload(api, d, audio=b"bad data").status_code == 400
    manager(user).patch(f'/api/v1/voice/devices/{d["id"]}/', {"name": "新名字"}, format="json")
    assert upload(api, d).status_code == 409
    assert VoiceTurn.objects.count() == 0


def test_scope_rejection_never_falls_back(user):
    result = manager(user).post("/api/v1/voice/devices/", {"name": "test", "scope_mode": "selected", "library_codes": ["missing"]}, format="json")
    assert result.status_code == 400 and VoiceDevice.objects.count() == 0


def test_real_pipeline_links_one_user_and_one_answer_on_replayed_job(user):
    d, api = added(user)
    response = upload(api, d)
    turn = VoiceTurn.objects.get(pk=response.data["task_id"])
    reply = SimpleNamespace(content="这是真实模型接口的替身测试回答。", model="test", usage={}, tool_calls=[])
    with patch("apps.voice.services.transcribe", return_value="查询资料"), patch("apps.planner.services.LlmClient") as llm, patch("apps.voice.services.frame", return_value=(b"", "", 1)):
        llm.return_value.chat.return_value = reply
        services.process_turn(turn.pk)
        services.process_turn(turn.pk)
    turn.refresh_from_db()
    assert turn.state == "answer_ready", turn.error
    assert Message.objects.filter(conversation_id=d["conversation_id"], role="user").count() == 1
    assert Message.objects.filter(conversation_id=d["conversation_id"], role="assistant").count() == 1
    assert turn.user_message is not None and turn.user_message.payload["device_id"] == d["id"]


def test_asr_failure_retry_is_stage_limited(user):
    d, api = added(user)
    r = upload(api, d); t = VoiceTurn.objects.get(pk=r.data["task_id"])
    with patch("apps.voice.services.transcribe", side_effect=RuntimeError("ASR offline")):
        services.process_turn(t.pk)
    t.refresh_from_db()
    assert t.state == "failed" and not t.user_message_id
    assert api.post(f'/api/v1/voice/device/turns/{t.client_turn_id}/retry/').status_code == 200
    t.refresh_from_db(); assert t.state == "queued"


def test_unknown_model_result_not_replayed(user):
    d, api = added(user)
    r = upload(api, d); t = VoiceTurn.objects.get(pk=r.data["task_id"])
    with patch("apps.voice.services.transcribe", return_value="hello"), patch("apps.planner.services.reply_to_message", side_effect=RuntimeError("connection lost")):
        services.process_turn(t.pk)
    t.refresh_from_db()
    assert t.state == "uncertain"
    assert api.post(f'/api/v1/voice/device/turns/{t.client_turn_id}/retry/').status_code == 409


def test_active_turn_prevents_route_change_but_allows_disable(user):
    d, api = added(user); upload(api, d)
    m = manager(user)
    url = f'/api/v1/voice/devices/{d["id"]}/'
    assert m.patch(url, {"name": "later"}, format="json").status_code == 409
    assert m.patch(url, {"enabled": False}, format="json").status_code == 200


def test_bitmap_format_and_access_checks(user):
    d, api = added(user); r = upload(api, d)
    t = VoiceTurn.objects.get(pk=r.data["task_id"])
    t.transcript = "你好，知识库。"; t.save()
    result = api.get(f'/api/v1/voice/device/turns/{t.client_turn_id}/pages/transcript/0/')
    assert result.status_code == 200
    assert len(result.content) == 15000
    assert result["X-Content-SHA256"] == hashlib.sha256(result.content).hexdigest()
    assert result["Cache-Control"] == "no-store"
    assert api.get(f'/api/v1/voice/device/turns/{t.client_turn_id}/pages/transcript/999/').status_code == 404


def test_page_batch_matches_single_pages_and_keeps_device_isolation(user):
    from apps.voice.rendering import frame
    d, api = added(user); r = upload(api, d)
    t = VoiceTurn.objects.get(pk=r.data["task_id"])
    t.answer = "\n".join(f"第 {i} 行：用于核对多页内容顺序。" for i in range(100))
    t.save()
    url = f'/api/v1/voice/device/turns/{t.client_turn_id}/pages/answer/batch/'
    result = api.get(url + '0/')
    assert result.status_code == 200
    assert result['X-Frame-Count'] == '8'
    assert result['X-Page-Count'] == str(frame(t.answer, 'AI 回复')[2])
    assert len(result.content) == 8 * 15000
    assert result.content == b''.join(frame(t.answer, 'AI 回复', i)[0] for i in range(8))
    assert result['X-Content-SHA256'] == hashlib.sha256(result.content).hexdigest()
    assert result['Cache-Control'] == 'no-store'
    tail = api.get(url + '8/')
    assert tail.status_code == 200
    assert tail.content[:15000] == frame(t.answer, 'AI 回复', 8)[0]
    assert 1 <= int(tail['X-Frame-Count']) <= 8
    assert api.get(url + '999/').status_code == 404
    _, other_api = added(user)
    assert other_api.get(url + '0/').status_code == 404
    assert APIClient().get(url + '0/').status_code == 401
    manager(user).patch(f'/api/v1/voice/devices/{d["id"]}/', {'enabled': False}, format='json')
    assert api.get(url + '0/').status_code == 401


def test_devices_page_requires_login(user):
    from django.test import Client
    assert APIClient().get("/app/devices/").status_code == 302
    client = Client()
    client.force_login(user)
    result = client.get("/app/devices/")
    assert result.status_code == 200 and "添加设备" in result.content.decode()


def test_empty_model_reply_returns_explanation_instead_of_infinite_page_retry(user):
    d, api = added(user); r = upload(api, d)
    t = VoiceTurn.objects.get(pk=r.data['task_id'])
    t.state = 'answer_ready'
    t.assistant_message = Message.objects.create(conversation_id=d['conversation_id'], role='assistant', content='',
        payload={'rejected_proposals': [{'reason': '信息库不存在：demo'}]})
    t.save()
    assert '信息库不存在：demo' in services.screen_answer(t)
    response = api.get(f'/api/v1/voice/device/turns/{t.client_turn_id}/pages/answer/batch/0/')
    assert response.status_code == 200 and len(response.content) == 15000
