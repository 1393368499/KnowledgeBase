"""「重扫未处理反馈」必须**名副其实**（独立审查意见 4）。

旧实现是 ``call_command("kbprocess", "--retry-failed")`` —— 那是"重试**失败**资料"，
与这个意图的名字、界面标签（「重扫未处理反馈」）都不符，而且无论实际做了什么
都固定返回 ``{"ok": True, "detail": "已触发失败资料重扫"}``。

本组用例锁住现在的口径：
  * 目标是**净负分**（被反复判不准确）的资料 —— 与 ``list_feedback_targets`` 同一条查询；
  * 净正分、以及**没有** ``include_failed`` 时的解析失败资料，都不许被碰；
  * ``include_failed="true"`` 时才额外重试失败资料，且**两类分开计数**；
  * 逐份真的执行：成功/失败/租约被占/未完成如实分类，不再假报成功；
  * 上限、剩余、返回值形状都如实。
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.utils import timezone

from apps.knowledge.models import IngestedFile, ParseStatus, SearchFeedback
from apps.knowledge.pipeline import LeaseNotAcquired, PipelineError
from apps.planner import services
from apps.planner.models import Conversation, OperationRecord
from tests.planner_fixtures import require_spec as get_spec

INTENT = "reparse_failed"


def _armed(user, params):
    ref, label = services._describe_target(get_spec(INTENT), params)
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
        armed_expires_at=timezone.now() + timedelta(minutes=10),
    )


def _fake_step(ok=True, detail="重扫完成"):
    return SimpleNamespace(ok=ok, detail=detail)


def _patch_retry(monkeypatch, behaviour):
    """替换流水线的 ``retry_file``（服务层是从模块属性取的），返回调用记录。"""
    calls: list[int] = []

    def _fake(record, **kwargs):
        calls.append(record.pk)
        return behaviour(record)

    import apps.knowledge.pipeline as pipeline

    monkeypatch.setattr(pipeline, "retry_file", _fake)
    return calls


@pytest.fixture
def feedback(make_library, make_file, admin_user):
    """一个净负分资料 + 一个净正分资料。"""

    def _make():
        library = make_library(code="rescan", name="重扫库")
        bad = make_file(library, "老被说不准.txt")
        good = make_file(library, "老被说准.txt")
        for index in range(3):
            SearchFeedback.objects.create(
                user=admin_user, query=f"b{index}", verdict="inaccurate",
                file=bad, chunk_ordinal=index,
            )
        SearchFeedback.objects.create(
            user=admin_user, query="g0", verdict="accurate", file=good, chunk_ordinal=0
        )
        return library, bad, good

    return _make


# ------------------------------------------------------------------ 正例 ----


def test_net_negative_file_is_actually_rescanned(admin_user, feedback, monkeypatch):
    _library, bad, _good = feedback()
    calls = _patch_retry(monkeypatch, lambda row: _fake_step(True, "重建 9 段向量"))

    result = services.execute_operation(admin_user, _armed(admin_user, {}))["result"]

    assert calls == [bad.pk], "净负分资料必须真的重跑一次流水线"
    assert result["processed"] == 1 and result["count"] == 1
    assert result["feedback_targets"] == 1
    assert result["items"][0]["name"] == "老被说不准.txt"
    assert "净负分资料 1 份" in result["detail"]
    assert "实际重扫 1 份" in result["detail"]


def test_net_positive_file_is_left_alone(admin_user, feedback, monkeypatch):
    _library, _bad, good = feedback()
    calls = _patch_retry(monkeypatch, lambda row: _fake_step(True))

    services.execute_operation(admin_user, _armed(admin_user, {}))["result"]

    assert good.pk not in calls, "净正分的资料不该被重扫"


# ------------------------------------------------- 旧行为不符的那一处（核心） ----


def test_failed_files_are_not_touched_without_include_failed(
    admin_user, make_library, make_file, monkeypatch
):
    library = make_library(code="rescan", name="重扫库")
    broken = make_file(library, "解析失败.txt", status=ParseStatus.FAILED)
    calls = _patch_retry(monkeypatch, lambda row: _fake_step(True))

    result = services.execute_operation(admin_user, _armed(admin_user, {}))["result"]

    assert calls == [], "没有净负分资料时，不该顺手去重试失败资料（旧实现的错就在这里）"
    assert result["checked"] == 0
    assert result["failed_retried"] == 0
    assert "没有需要重扫的净负分资料" in result["detail"]
    assert "include_failed" in result["detail"], "要如实告诉用户怎么顺带重试失败资料"
    assert broken.pk not in calls


def test_include_failed_adds_failed_files_and_counts_separately(
    admin_user, feedback, make_file, monkeypatch
):
    library, bad, _good = feedback()
    broken = make_file(library, "解析失败.txt", status=ParseStatus.FAILED)
    calls = _patch_retry(monkeypatch, lambda row: _fake_step(True))

    result = services.execute_operation(
        admin_user, _armed(admin_user, {"include_failed": "true"})
    )["result"]

    assert sorted(calls) == sorted([bad.pk, broken.pk])
    assert result["feedback_targets"] == 1
    assert result["failed_retried"] == 1
    assert "净负分资料 1 份、解析失败资料 1 份" in result["detail"]


def test_budget_exhausted_failed_files_are_excluded(
    admin_user, make_library, make_file, monkeypatch
):
    library = make_library(code="rescan", name="重扫库")
    make_file(
        library,
        "重试次数用尽.txt",
        status=ParseStatus.FAILED,
        parse_attempts=IngestedFile.MAX_PARSE_ATTEMPTS,
    )
    calls = _patch_retry(monkeypatch, lambda row: _fake_step(True))

    result = services.execute_operation(
        admin_user, _armed(admin_user, {"include_failed": "true"})
    )["result"]

    assert calls == [], "尝试次数已用尽的资料要人工介入，不该再刷同一条失败"
    assert result["failed_retried"] == 0


# ------------------------------------------------------------ 如实分类 ----


def test_lease_busy_changes_nothing(admin_user, feedback, monkeypatch):
    _library, bad, _good = feedback()
    before = IngestedFile.objects.get(pk=bad.pk)
    before_state = (before.parse_status, before.parse_attempts, before.chunk_count)
    _patch_retry(
        monkeypatch,
        lambda row: (_ for _ in ()).throw(LeaseNotAcquired("租约被 #3 持有")),
    )

    result = services.execute_operation(admin_user, _armed(admin_user, {}))["result"]

    assert result["lease_busy"] == 1
    assert result["processed"] == 0 and result["count"] == 0
    assert result["items"][0]["status"] == "租约被占，未改动"
    after = IngestedFile.objects.get(pk=bad.pk)
    assert (after.parse_status, after.parse_attempts, after.chunk_count) == before_state


def test_pipeline_error_is_reported_as_failed(admin_user, feedback, monkeypatch):
    _library, _bad, _good = feedback()
    _patch_retry(
        monkeypatch,
        lambda row: (_ for _ in ()).throw(PipelineError("模型不可用：embedding 503")),
    )

    result = services.execute_operation(admin_user, _armed(admin_user, {}))["result"]

    assert result["failed"] == 1
    assert result["processed"] == 0
    assert "模型不可用" in result["items"][0]["detail"]
    assert "失败 1 份" in result["detail"]


# ------------------------------------------------------------ 上限与形状 ----


def test_limit_is_capped_and_remaining_is_reported(
    admin_user, make_library, make_file, monkeypatch
):
    library = make_library(code="rescan", name="重扫库")
    for index in range(services.REPARSE_LIMIT + 3):
        record = make_file(library, f"老被说不准-{index}.txt")
        SearchFeedback.objects.create(
            user=admin_user, query=f"q{index}", verdict="inaccurate",
            file=record, chunk_ordinal=0,
        )
    calls = _patch_retry(monkeypatch, lambda row: _fake_step(True))

    result = services.execute_operation(
        admin_user, _armed(admin_user, {"limit": 99})
    )["result"]

    assert len(calls) == services.REPARSE_LIMIT, "一次同步执行不许超过硬上限"
    assert result["feedback_targets"] == services.REPARSE_LIMIT
    assert result["remaining"] == 3
    assert "仍有 3 份净负分资料未重扫" in result["detail"]


def test_result_never_claims_a_bare_ok(admin_user, feedback, monkeypatch):
    feedback()
    _patch_retry(monkeypatch, lambda row: _fake_step(True))

    result = services.execute_operation(admin_user, _armed(admin_user, {}))["result"]

    assert "ok" not in result, "旧实现固定返回 {'ok': True} —— 无论实际做了什么"
    assert set(result) >= {
        "count", "detail", "checked", "processed", "failed", "lease_busy",
        "incomplete", "remaining", "items", "note", "feedback_targets", "failed_retried",
    }
    assert "只读原件" in result["note"]


def test_invalid_limit_is_rejected_at_execute(admin_user, feedback, monkeypatch):
    feedback()
    _patch_retry(monkeypatch, lambda row: _fake_step(True))

    with pytest.raises(services.PlannerError) as excinfo:
        services.execute_operation(admin_user, _armed(admin_user, {"limit": "abc"}))

    assert "整数" in str(excinfo.value)


def test_non_admin_cannot_propose(db, user, library):
    with pytest.raises(services.PlannerError):
        services.propose_operation(user, intent=INTENT, params={})
