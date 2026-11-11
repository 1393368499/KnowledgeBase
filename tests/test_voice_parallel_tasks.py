import uuid
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.db import connection
from rest_framework.test import APIClient
from apps.planner.models import Conversation, Message
from apps.voice.models import VoiceTurn
from apps.voice import services
from apps.voice.management.commands.voiceworker import run_turn
from tests.test_voice_devices import manager, upload

pytestmark = pytest.mark.django_db


def added(user):
    result = manager(user).post("/api/v1/voice/devices/", {"name": "并行任务板", "conversation_mode": "per_turn"}, format="json")
    assert result.status_code == 201
    api = APIClient()
    api.credentials(HTTP_AUTHORIZATION="Bearer " + result.data["device_token"])
    return result.data, api


def test_each_audio_creates_one_distinct_conversation_and_replay_never_duplicates(user):
    d, api = added(user)
    assert d["conversation_mode"] == "per_turn"
    baseline = Conversation.objects.count()
    id1, id2 = uuid.uuid4(), uuid.uuid4()
    a, b = upload(api, d, id1), upload(api, d, id2)
    assert a.status_code == b.status_code == 202
    assert a.data["conversation_id"] != b.data["conversation_id"]
    assert upload(api, d, id1).data["conversation_id"] == a.data["conversation_id"]
    assert Conversation.objects.count() == baseline + 2
    assert VoiceTurn.objects.count() == 2
    config = api.get("/api/v1/voice/device/config/").data
    assert len(config["active_turn_ids"]) == 2 and config["max_parallel_tasks"] == 4


def test_capacity_four_and_canceling_one_frees_a_slot_without_resending(user):
    d, api = added(user)
    accepted = [upload(api, d) for _ in range(4)]
    assert all(x.status_code == 202 for x in accepted)
    next_id = uuid.uuid4()
    fifth = upload(api, d, next_id)
    assert fifth.status_code == 429 and fifth.data["code"] == "capacity"
    assert Conversation.objects.count() == 4
    assert api.post(f'/api/v1/voice/device/turns/{accepted[0].data["turn_id"]}/cancel/').status_code == 200
    assert upload(api, d, next_id).status_code == 202
    assert VoiceTurn.objects.filter(state__in=services.ACTIVE).count() == 4


def test_tasks_are_device_scoped_active_first_and_have_separate_session_links(user):
    d, api = added(user)
    other, second = added(user)
    first, _ = upload(api, d), upload(second, other)
    result = api.get("/api/v1/voice/device/tasks/")
    assert result.status_code == 200 and result.data["active_count"] == 1
    rows = result.data["results"]
    assert len(rows) == 1 and rows[0]["conversation_id"] == first.data["conversation_id"]
    assert rows[0]["can_cancel"] is True
    assert APIClient().get("/api/v1/voice/device/tasks/").status_code == 401
    assert second.get(f'/api/v1/voice/device/turns/{first.data["turn_id"]}/').status_code == 404


@pytest.mark.django_db(transaction=True)
def test_two_real_worker_threads_reach_ai_concurrently_in_distinct_sessions(user):
    if connection.vendor != "postgresql":
        pytest.skip("Real concurrent row locks require PostgreSQL")
    d, api = added(user)
    ids = [upload(api, d).data["task_id"] for _ in range(2)]
    gate = threading.Barrier(2)
    entered = []
    def model(messages, **kwargs):
        entered.append(threading.get_ident())
        gate.wait(timeout=20)
        return SimpleNamespace(content="并行处理完成", tool_calls=[], model="test", usage={})
    with patch.object(services, "transcribe", side_effect=lambda t: "独立任务 " + str(t.client_turn_id)), patch("apps.planner.services.LlmClient") as llm:
        llm.return_value.chat.side_effect = model
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run_turn, pk) for pk in ids]
            for future in futures:
                future.result(timeout=40)
    turns = list(VoiceTurn.objects.filter(pk__in=ids))
    assert len(set(entered)) == 2
    assert all(t.state == "answer_ready" for t in turns)
    assert len({t.conversation_id for t in turns}) == 2
    for t in turns:
        assert Message.objects.filter(conversation=t.conversation, role="user").count() == 1
        assert Message.objects.filter(conversation=t.conversation, role="assistant").count() == 1
        assert t.conversation.title.startswith("独立任务")
