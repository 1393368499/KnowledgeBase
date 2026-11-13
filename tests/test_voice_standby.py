import hashlib
from unittest.mock import patch

import pytest
from tests.api_client import APIClient
from apps.knowledge.models import IngestedFile, LibraryMember, MemberRole
from apps.planner.models import OperationRecord
from apps.voice import services
from apps.voice.models import VoiceTurn
from apps.voice.standby import tasks, render

pytestmark = pytest.mark.django_db


def device(user):
    d, token = services.add_device(user, {"name": "测试板"})
    api = APIClient()
    api.credentials(HTTP_AUTHORIZATION="Bearer " + token)
    return d, api


def test_standby_authentication_and_exact_verified_frame(user):
    d, api = device(user)
    assert APIClient().get("/api/v1/voice/device/standby/").status_code == 401
    r = api.get("/api/v1/voice/device/standby/")
    assert r.status_code == 200
    assert len(r.content) == 15000
    assert r["X-Content-SHA256"] == hashlib.sha256(r.content).hexdigest()
    assert r["Cache-Control"] == "no-store"
    d.enabled = False
    d.save(update_fields=["enabled"])
    assert api.get("/api/v1/voice/device/standby/").status_code == 401


def test_work_is_read_only_scoped_and_permissions_are_rechecked(user, other_user, make_library):
    a, b = make_library(code="own-a"), make_library(code="hidden-b")
    LibraryMember.objects.create(library=a, user=user, role=MemberRole.READER)
    for lib, name in [(a, "允许资料.pdf"), (b, "机密资料.pdf")]:
        IngestedFile.objects.create(library=lib, original_name=name, content_sha256=hashlib.sha256(name.encode()).hexdigest(), parse_status="running")
    d, api = device(user)
    assert tasks(d) == ("解析：允许资料.pdf",)
    d.scope_mode = "selected"
    d.library_codes = [b.code]
    d.save()
    assert tasks(d) == ()
    d.library_codes = [a.code]
    assert tasks(d) == ("解析：允许资料.pdf",)
    LibraryMember.objects.filter(library=a, user=user).delete()
    assert tasks(d) == ()
    # Building the screen must never start a worker, model or operation.
    with patch("apps.voice.services.answer_turn") as answer:
        assert api.get("/api/v1/voice/device/standby/").status_code == 200
        answer.assert_not_called()
    assert IngestedFile.objects.filter(parse_status="running").count() == 2


def test_only_own_device_voice_and_own_operations_are_shown(user, other_user):
    d, api = device(user)
    another, _ = services.add_device(other_user, {"name": "别人的板"})
    import uuid
    for dev in [d, another]:
        VoiceTurn.objects.create(device=dev, client_turn_id=uuid.uuid4(), conversation=dev.conversation,  # pyright: ignore[reportArgumentType]
            config_version=dev.config_version, scope_mode="all_authorized", audio_sha256="", audio_path="", state="answering")
    OperationRecord.objects.create(user=other_user, intent="compose_document", status="executing", target_label="别人的私密任务")
    OperationRecord.objects.create(user=user, intent="compose_document", status="executed", target_label="已经结束")
    assert tasks(d) == ("AI 正在回答",)


@pytest.mark.parametrize("minute", ["2026-10-28T09:12:00+08:00", "2026-03-31T23:59:00+08:00", "2028-02-29T00:00:00+08:00"])
def test_calendar_handles_six_weeks_leap_year_and_long_tasks(minute):
    raw, digest = render(minute, ("解析：" + "长文件名" * 25,) * 4)
    assert len(raw) == 15000
    assert digest == hashlib.sha256(raw).hexdigest()
