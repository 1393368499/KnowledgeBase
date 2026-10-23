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
def test_console_actions_use_real_propose_confirm_execute_protocol(api, admin_user, intent):
    api.force_authenticate(admin_user)
    proposed = api.post("/api/v1/planner/operations/propose/", {"intent": intent, "params": {}}, format="json")
    assert proposed.status_code in (200, 201), proposed.data
    operation_id = proposed.data["operation_id"]
    base = f"/api/v1/planner/operations/{operation_id}/"
    early = api.post(base + "execute/", {}, format="json")
    assert early.status_code == 409
    token = proposed.data["confirm_token"]
    for _ in range(proposed.data["required_confirmations"]):
        confirmed = api.post(base + "confirm/", {"token": token}, format="json")
        assert confirmed.status_code == 200, confirmed.data
        token = confirmed.data.get("next_confirm_token", "")
    done = api.post(base + "execute/", {}, format="json")
    assert done.status_code == 200, done.data
    assert done.data["status"] == "executed"
    assert isinstance(done.data["result"], dict)
    assert api.post(base + "execute/", {}, format="json").status_code == 409


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
    operation = OperationRecord.objects.get(pk=payload["operation_id"])
    services.confirm_operation(admin_user, operation, token=payload["confirm_token"])
    result = services.execute_operation(admin_user, operation)["result"]
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
