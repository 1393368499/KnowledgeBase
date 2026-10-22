"""审计哈希链测试：不可篡改底座的"证据"部分。"""

from __future__ import annotations

import json

import pytest
from django.db import connection

from apps.audit.models import AuditEvent
from apps.audit.services import append_event, verify_chain
from governance.immutable import ImmutableRecordError

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def empty_chain():
    """每个用例从空链开始：哈希链是全局状态，测试之间不能互相污染。

    清空要走"受控摘链"：数据库触发器禁止删审计事件，这正是产品要求的行为，
    所以测试也把触发器临时摘掉再删——正好顺带验证了 ``drop_guards`` 可用。
    """
    from governance import triggers

    triggers.drop_guards()
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM audit_auditevent")
    triggers.create_guards()
    yield


def test_chain_links_and_verifies(user):
    first = append_event(action="a.one", actor_id=user.pk, actor_label=user.get_username())
    second = append_event(action="a.two", actor_id=user.pk)
    third = append_event(action="a.three", resource_kind="ingested_file", resource_key="7")

    assert first.prev_hash == "0" * 64
    assert second.prev_hash == first.entry_hash
    assert third.prev_hash == second.entry_hash
    assert first.entry_hash != second.entry_hash

    report = verify_chain()
    assert report["ok"] is True
    assert report["checked"] == 3
    assert report["broken_at"] is None


def test_payload_is_stored_verbatim(user):
    event = append_event(
        action="file.ingested",
        actor_id=user.pk,
        actor_label="alice",
        resource_kind="ingested_file",
        resource_key="42",
        conversation_key="conv-1",
        payload={"sha256": "abc", "size_bytes": 2048, "中文键": "值"},
    )
    event.refresh_from_db()
    assert event.payload == {"sha256": "abc", "size_bytes": 2048, "中文键": "值"}
    assert event.summary == ""
    assert event.source == "api"


def test_verify_chain_detects_tampering(user):
    from governance import triggers

    append_event(action="a.one", actor_id=user.pk)
    middle = append_event(action="a.two", actor_id=user.pk)
    append_event(action="a.three", actor_id=user.pk)

    # 绕过模型 save 守卫，直接改库——模拟"有人拿到数据库权限"。
    # 触发器也是守卫的一部分，篡改者要得手必须先摘掉它，用受控通道模拟这一步。
    triggers.drop_guards()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE audit_auditevent SET payload = %s WHERE id = %s",
                [json.dumps({"tampered": True}), middle.pk],
            )
    finally:
        triggers.create_guards()

    report = verify_chain()
    assert report["ok"] is False
    assert report["broken_at"] == middle.pk
    assert "entry_hash" in report["reason"]


def test_verify_chain_detects_deletion(user):
    from governance import triggers

    first = append_event(action="a.one", actor_id=user.pk)
    append_event(action="a.two", actor_id=user.pk)

    triggers.drop_guards()
    try:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM audit_auditevent WHERE id = %s", [first.pk])
    finally:
        triggers.create_guards()

    report = verify_chain()
    assert report["ok"] is False
    assert report["broken_at"] is not None


def test_event_cannot_be_updated(user):
    event = append_event(action="a.one", actor_id=user.pk)
    event.action = "a.changed"
    with pytest.raises(RuntimeError, match="append-only"):
        event.save()


def test_event_cannot_be_deleted(user):
    event = append_event(action="a.one", actor_id=user.pk)
    with pytest.raises(RuntimeError, match="append-only"):
        event.delete()
    assert ImmutableRecordError is not None  # 导入语义：守卫异常类型可供上层捕获


def test_queryset_update_and_delete_are_blocked(user):
    append_event(action="a.one", actor_id=user.pk)
    with pytest.raises(ImmutableRecordError):
        AuditEvent.objects.all().update(action="x")
    with pytest.raises(ImmutableRecordError):
        AuditEvent.objects.all().delete()


def test_entry_hash_is_reproducible(user):
    event = append_event(action="a.one", actor_id=user.pk, payload={"k": [1, 2, 3]})
    event.refresh_from_db()
    assert event.compute_entry_hash() == event.entry_hash
    assert len(event.entry_hash) == 64
