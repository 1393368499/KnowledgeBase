"""④ 检索反馈必须能由 AI 工具提案到达（并在执行时真的落库）。

两层证明：
  ① **可被提案**：意图已登记、且被识别为写操作（mutation）；
  ② **可被执行**：走 `execute_operation`（与首页 AI 确认后同一条路径）真的写入，
     且**不修改源资料**。

目标身份（`target_ref`/`target_label`）**直接调用执行端重核用的同一个函数**
取，不自造魔法字符串 —— 否则测的就不是真实那条路径。
"""

from __future__ import annotations

import pytest
from django.utils import timezone

from apps.knowledge.models import IngestedFile, SearchFeedback

INTENT = "submit_search_feedback"


@pytest.fixture
def readable_file(make_library, make_file, grant_member, user):
    library = make_library(code="agent-fb", name="反馈库")
    grant_member(library, user)
    return make_file(library=library, name="反馈资料.txt")


def _armed(user, params):
    """按执行端的要求造一条 ARMED 操作（身份取自同一个解析函数）。"""
    from apps.planner.models import Conversation, OperationRecord
    from apps.planner.operations import get as get_spec
    from apps.planner.services import _describe_target

    ref, label = _describe_target(get_spec(INTENT), params)
    conversation = Conversation.objects.create(user=user)
    return OperationRecord.objects.create(
        user=user,
        conversation=conversation,
        intent=INTENT,
        params=params,
        target_ref=ref,
        target_label=label,
        status=OperationRecord.Status.ARMED,
        armed_at=timezone.now(),
        armed_expires_at=timezone.now() + timezone.timedelta(minutes=10),
    )


def test_intent_is_registered_as_a_mutation():
    from apps.planner.operations import all_specs, get, is_mutation

    spec = get(INTENT)
    assert spec is not None, "意图未登记 → AI 无法提案"
    assert is_mutation(INTENT) is True, "写操作必须归类为 mutation"
    assert INTENT in {item.intent for item in all_specs()}


def test_agent_execution_persists_feedback_without_touching_source(db, user, readable_file):
    from apps.planner import services

    before = IngestedFile.objects.get(pk=readable_file.pk)
    before_state = (before.created_at, before.parse_status, before.parsed_at)

    operation = _armed(user, {
        "query": "质保期", "verdict": "accurate",
        "file_id": readable_file.pk, "chunk_ordinal": 0,
    })
    result = services.execute_operation(user, operation)

    # execute_operation 返回**信封**：{operation_id, status, result}；
    # 真正的执行结果在 result["result"] 里。
    assert result["status"] == "executed", result
    assert result["result"]["ok"] is True, result
    row = SearchFeedback.objects.get(user=user)
    assert (row.verdict, row.query, row.file_id) == ("accurate", "质保期", readable_file.pk)

    after = IngestedFile.objects.get(pk=readable_file.pk)
    assert (after.created_at, after.parse_status, after.parsed_at) == before_state, (
        "**硬约束**：AI 执行反馈也不能修改源资料"
    )


def test_agent_execution_rejects_bad_verdict(db, user):
    from apps.planner import services

    operation = _armed(user, {"query": "质保期", "verdict": "maybe"})
    result = services.execute_operation(user, operation)

    assert result["result"]["ok"] is False, result
    assert not SearchFeedback.objects.exists(), "非法 verdict 不得落库"
