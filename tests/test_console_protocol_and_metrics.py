from datetime import timedelta

import pytest
from django.utils import timezone

from apps.knowledge import integrity
from apps.planner.models import Conversation, OperationRecord

pytestmark = pytest.mark.django_db


def test_manual_patrol_does_not_hide_stale_periodic_heartbeat():
    from unittest.mock import patch

    with patch("django.utils.timezone.now", return_value=timezone.now() - timedelta(hours=2)):
        integrity.record_patrol_run({"checked": 3}, source="beat")
    integrity.record_patrol_run({"checked": 5}, source="manual")
    snapshot = integrity.patrol_snapshot()
    assert snapshot["source"] == "manual"
    assert snapshot["checked"] == 5
    assert snapshot["overdue"] is True
    assert snapshot["heartbeat_age_seconds"] >= 7200


def test_manual_patrol_cannot_prove_scheduler_is_running():
    integrity.record_patrol_run({"checked": 1}, source="manual")
    snapshot = integrity.patrol_snapshot()
    assert snapshot["available"]
    assert snapshot["overdue"] is None
    assert not snapshot["heartbeat_available"]
    assert "手动巡检不能证明" in snapshot["verdict"]


def test_operation_summary_covers_all_history_not_latest_hundred(api, admin_user, user):
    conversation = Conversation.objects.create(user=admin_user)
    OperationRecord.objects.bulk_create([
        OperationRecord(user=admin_user, conversation=conversation, intent="run_patrol", status="failed")
        for _ in range(120)
    ] + [OperationRecord(user=admin_user, conversation=conversation, intent="run_patrol", status="executed")])
    other = Conversation.objects.create(user=user)
    OperationRecord.objects.create(user=user, conversation=other, intent="run_patrol", status="executed")
    api.force_authenticate(admin_user)
    response = api.get("/api/v1/planner/operations/")
    assert response.status_code == 200
    assert len(response.data["results"]) == 100
    assert response.data["summary"]["settled"] == 121
    assert response.data["summary"]["executed"] == 1


@pytest.mark.parametrize("intent", ["run_patrol", "restart_stalled_task", "reset_breaker", "feedback_learning_report", "reparse_failed"])
def test_console_actions_execute_without_confirmation_and_cannot_replay(api, admin_user, intent):
    """控制台动作按现行策略**提案即执行**：没有确认卡，也不能被重复执行。

    ``docs/delete-only-confirmation-2026-10-27.md``：只有删除保留人工确认；其它
    非删除动作参数完整且权限通过后直接执行。所以一次提案请求就拿到 executed 结果，
    不再有 ``confirm``/``execute`` 的两次点击；旧入口对已完成操作必须明确拒绝。
    """
    api.force_authenticate(admin_user)
    proposed = api.post("/api/v1/planner/operations/propose/", {"intent": intent, "params": {}}, format="json")
    assert proposed.status_code in (200, 201), proposed.data
    assert proposed.data["status"] == "executed", proposed.data
    assert proposed.data["required_confirmations"] == 0, proposed.data
    assert proposed.data["auto_executed"] is True, proposed.data
    assert proposed.data["confirm_token"] == "", "非删除动作不应签发确认凭证"
    assert isinstance(proposed.data["result"], dict), proposed.data
    operation_id = proposed.data["operation_id"]
    base = f"/api/v1/planner/operations/{operation_id}/"
    # 旧入口（确认 / 执行）对已完成的操作必须明确拒绝，而不是再执行一次
    assert api.post(base + "confirm/", {}, format="json").status_code == 400
    assert api.post(base + "execute/", {}, format="json").status_code == 409
    # 再执行一遍仍然拒绝（没有第二次副作用）
    assert api.post(base + "execute/", {}, format="json").status_code == 409
    row = OperationRecord.objects.get(pk=operation_id)
    assert row.status == "executed"
    assert row.confirmations == 0
    assert row.error == ""


@pytest.mark.parametrize("intent", ["run_patrol", "restart_stalled_task", "reset_breaker", "feedback_learning_report", "reparse_failed"])
def test_console_admin_actions_reject_plain_accounts(api, user, intent):
    api.force_authenticate(user)
    response = api.post("/api/v1/planner/operations/propose/", {"intent": intent, "params": {}}, format="json")
    assert response.status_code in (400, 403)
    assert not OperationRecord.objects.exists()


def test_learning_report_samples_distinct_files_before_feedback_limit(api, admin_user, library, make_file):
    from apps.knowledge.models import SearchFeedback
    from apps.planner import services

    first, second = make_file(library), make_file(library)
    SearchFeedback.objects.create(user=admin_user, file=first, query="old", verdict="inaccurate")
    SearchFeedback.objects.bulk_create([
        SearchFeedback(user=admin_user, file=second, query=f"q{i}", verdict="inaccurate")
        for i in range(201)
    ])
    payload = services.propose_operation(admin_user, intent="feedback_learning_report", params={})
    assert payload["auto_executed"] is True, payload
    assert payload["confirm_token"] == ""
    result = payload["result"]
    assert {row["file_id"] for row in result["items"]} == {first.pk, second.pk}


def test_audit_verification_reports_all_mismatches_without_changing_records(monkeypatch):
    from apps.audit.models import AuditEvent
    from apps.audit.services import append_event, verify_chain

    rows = [append_event(action="audit.console.probe") for _ in range(3)]
    original = AuditEvent.compute_entry_hash
    bad_ids = {rows[0].pk, rows[2].pk}
    monkeypatch.setattr(AuditEvent, "compute_entry_hash", lambda row: "f" * 64 if row.pk in bad_ids else original(row))
    result = verify_chain()
    assert not result["ok"]
    assert result["checked"] == 3
    assert result["mismatch_count"] == result["hash_mismatch_count"] == 2
    assert result["link_mismatch_count"] == 0
    assert result["broken_at"] == rows[0].pk
    assert [item["id"] for item in result["mismatches"]] == [rows[0].pk, rows[2].pk]
    assert list(AuditEvent.objects.order_by("id").values_list("entry_hash", flat=True)) == [row.entry_hash for row in rows]
    limited = verify_chain(limit=2)
    assert limited["checked"] == 2 and limited["mismatch_count"] == 1
    assert limited["scope"] == "prefix"
