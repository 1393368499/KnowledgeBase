"""反馈处理（重扫的定位步骤）必须可由 AI 提案到达。

`list_feedback_targets` 是**只读**意图：找出"被反复判不准确"（净负分）的资料，
让人或 AI 决定是否重扫/重建。它**不修改源资料**、也不自动重排。
"""

from __future__ import annotations

import pytest

from apps.knowledge.models import IngestedFile, SearchFeedback


def _armed_readonly(user, intent, params):
    """造一条 ARMED 的**只读**操作（身份取自执行端同一个解析函数）。"""
    from django.utils import timezone

    from apps.planner.models import Conversation, OperationRecord
    from apps.planner.operations import get as get_spec
    from apps.planner.services import _describe_target

    ref, label = _describe_target(get_spec(intent), params)
    conversation = Conversation.objects.create(user=user)
    return OperationRecord.objects.create(
        user=user,
        conversation=conversation,
        intent=intent,
        params=params,
        target_ref=ref,
        target_label=label,
        status=OperationRecord.Status.ARMED,
        armed_at=timezone.now(),
        armed_expires_at=timezone.now() + timezone.timedelta(minutes=10),
    )


@pytest.fixture
def files_with_feedback(make_library, make_file, grant_member, user):
    library = make_library(code="rescan", name="重扫库")
    grant_member(library, user)
    bad = make_file(library=library, name="老被说不准.txt")
    good = make_file(library=library, name="老被说准.txt")
    for index in range(3):
        SearchFeedback.objects.create(
            user=user, query=f"b{index}", verdict="inaccurate", file=bad, chunk_ordinal=index
        )
    SearchFeedback.objects.create(
        user=user, query="g0", verdict="accurate", file=good, chunk_ordinal=0
    )
    return bad, good


def test_intent_registered_as_readonly():
    from apps.planner.operations import get, is_mutation

    assert get("list_feedback_targets") is not None, "意图未登记 → AI 无法提案"
    assert is_mutation("list_feedback_targets") is False, "定位步骤是只读，不该算写操作"


def test_lists_only_net_negative_files(db, user, files_with_feedback):
    from apps.planner import services

    bad, good = files_with_feedback
    operation = _armed_readonly(user, "list_feedback_targets", {"limit": 10})
    result = services.execute_operation(user, operation)

    payload = result["result"]
    assert payload["ok"] is True, payload
    ids = [item["file_id"] for item in payload["items"]]
    assert bad.pk in ids, "净负分的资料应被列出（重扫候选）"
    assert good.pk not in ids, "净正分的资料不该出现在重扫候选里"
    assert payload["items"][0]["net"] < 0


def test_does_not_modify_source_material(db, user, files_with_feedback):
    from apps.planner import services

    bad, _good = files_with_feedback
    before = IngestedFile.objects.get(pk=bad.pk)
    before_state = (before.created_at, before.parse_status, before.parsed_at)

    operation = _armed_readonly(user, "list_feedback_targets", {})
    services.execute_operation(user, operation)

    after = IngestedFile.objects.get(pk=bad.pk)
    assert (after.created_at, after.parse_status, after.parsed_at) == before_state, (
        "**硬约束**：定位步骤不得修改源资料"
    )
