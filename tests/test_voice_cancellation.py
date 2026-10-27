import threading
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.db import close_old_connections, connection
from rest_framework.test import APIClient

from apps.planner.models import Message
from apps.voice import services
from apps.voice.models import VoiceEvent, VoiceTurn
from tests.test_voice_devices import added, upload

pytestmark = pytest.mark.django_db


def cancel(api, client_id):
    return api.post(f'/api/v1/voice/device/turns/{client_id}/cancel/', {}, format='json')


def test_cancel_before_slow_upload_is_durable_and_idempotent(user):
    d, api = added(user); client_id = uuid.uuid4()
    first = cancel(api, client_id)
    assert first.status_code == 200 and first.data['state'] == 'canceled'
    assert cancel(api, client_id).data['task_id'] == first.data['task_id']
    late_upload = upload(api, d, client_id)
    assert late_upload.status_code == 200 and late_upload.data['state'] == 'canceled'
    turn = VoiceTurn.objects.get(pk=first.data['task_id'])
    assert turn.audio_path == '' and not turn.audio_sha256
    with patch.object(services, 'transcribe') as transcribe:
        services.process_turn(turn.pk)
        transcribe.assert_not_called()
    assert VoiceEvent.objects.filter(kind='turn.canceled').count() == 1
    assert not Message.objects.exists()
    assert upload(api, d).status_code == 202  # Cancellation does not block the next recording.


def test_cancel_during_asr_prevents_late_result_and_ai_submission(user):
    d, api = added(user); result = upload(api, d)
    turn = VoiceTurn.objects.get(pk=result.data['task_id'])
    def recognize(_):
        assert cancel(api, turn.client_turn_id).status_code == 200
        return '这段识别结果不应提交'
    with patch.object(services, 'transcribe', side_effect=recognize), patch('apps.planner.services.reply_to_message') as reply:
        services.process_turn(turn.pk)
        reply.assert_not_called()
    turn.refresh_from_db()
    assert turn.state == 'canceled' and not Message.objects.exists()


def test_canceled_asr_failure_cannot_overwrite_cancellation(user):
    d, api = added(user); result = upload(api, d)
    turn = VoiceTurn.objects.get(pk=result.data['task_id'])
    def recognize(_):
        cancel(api, turn.client_turn_id)
        raise RuntimeError('ASR ended after cancellation')
    with patch.object(services, 'transcribe', side_effect=recognize):
        services.process_turn(turn.pk)
    turn.refresh_from_db()
    assert turn.state == 'canceled'


@pytest.mark.parametrize('state', ['answering', 'rendering', 'answer_ready', 'uncertain'])
def test_started_ai_is_never_reported_as_canceled(user, state):
    d, api = added(user); result = upload(api, d)
    turn = VoiceTurn.objects.get(pk=result.data['task_id']); turn.state = state; turn.save()
    response = cancel(api, turn.client_turn_id)
    assert response.status_code == 409
    turn.refresh_from_db()
    assert turn.state == state
    assert not VoiceEvent.objects.filter(kind='turn.canceled').exists()


def test_cancellation_is_scoped_to_device_and_requires_device_auth(user):
    d, api = added(user); other, other_api = added(user)
    result = upload(other_api, other); uid = result.data['turn_id']
    response = cancel(api, uid)
    assert response.status_code == 200 and response.data['task_id'] != result.data['task_id']
    assert VoiceTurn.objects.get(pk=result.data['task_id']).state == 'queued'
    assert cancel(APIClient(), uid).status_code == 401


def test_ai_start_fence_wins_before_model_execution(user):
    d, api = added(user); result = upload(api, d)
    turn = VoiceTurn.objects.get(pk=result.data['task_id'])
    def model(*args, **kwargs):
        assert cancel(api, turn.client_turn_id).status_code == 409
        return SimpleNamespace(content='正常回答', model='test', usage={}, tool_calls=[])
    with patch.object(services, 'transcribe', return_value='已经开始的请求'), patch('apps.planner.services.LlmClient') as client:
        client.return_value.chat.side_effect = model
        services.process_turn(turn.pk)
    turn.refresh_from_db()
    assert turn.state == 'answer_ready'
    assert Message.objects.count() == 2


@pytest.mark.django_db(transaction=True)
def test_real_pg_cancel_wins_while_asr_runs_on_another_connection(user):
    if connection.vendor != 'postgresql':
        pytest.skip('Real PostgreSQL concurrency test')
    d, api = added(user); result = upload(api, d)
    turn = VoiceTurn.objects.get(pk=result.data['task_id'])
    entered, released = threading.Event(), threading.Event(); errors = []
    def recognize(_):
        entered.set()
        assert released.wait(8)
        return '取消后的迟到结果'
    def worker():
        close_old_connections()
        try:
            services.process_turn(turn.pk)
        except Exception as error:
            errors.append(error)
        finally:
            close_old_connections()
    with patch.object(services, 'transcribe', side_effect=recognize), patch('apps.planner.services.reply_to_message') as reply:
        thread = threading.Thread(target=worker); thread.start()
        try:
            assert entered.wait(8)
            assert cancel(api, turn.client_turn_id).status_code == 200
        finally:
            released.set(); thread.join(10)
        assert not thread.is_alive() and not errors
        reply.assert_not_called()
    turn.refresh_from_db()
    assert turn.state == 'canceled' and not Message.objects.exists()
