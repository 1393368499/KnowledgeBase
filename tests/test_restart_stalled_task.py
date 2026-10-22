"""「重启停滞任务」必须**如实报告**（独立审查意见 6）。

旧实现把"挑出来的 ID"直接当成 ``restarted`` 返回 —— 那是假绿灯：它既没有真正推进，
也不区分"失败"与"租约被别的 worker 正常持有"。

本组用例锁住新口径：
  * 只有**真正取得租约并推进过**的才计入 ``processed`` / ``count``；
  * ``LeaseNotAcquired`` → ``lease_busy``，并且**一个字节都不改**（状态与尝试次数不变）；
  * ``PipelineError`` → ``failed``，错误文本如实带回；
  * ``step.ok is False`` → ``incomplete``（不冒充成功）；
  * 租约**仍然有效**的 RUNNING 资料根本不在候选里，连碰都不碰；
  * 一次同步执行有上限，超出的如实计入 ``remaining``；
  * 返回值里**不再有** ``restarted`` 字段，也不拿 ``parse_attempts`` 冒充"重启次数"。
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.utils import timezone

from apps.knowledge.models import ParseStatus
from apps.knowledge.pipeline import LeaseNotAcquired, PipelineError
from apps.planner import services
from apps.planner.models import Conversation, OperationRecord
from apps.planner.operations import get as get_spec

INTENT = "restart_stalled_task"


def _armed(user, params):
    """直接造一张已进入执行窗口的操作记录（省掉确认流程）。"""
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


@pytest.fixture
def stalled(make_file, library):
    """造一份"卡在 RUNNING、租约已过期"的资料。"""

    def _make(name="停滞的资料.md", *, lease_minutes=-30, status=ParseStatus.RUNNING):
        now = timezone.now()
        return make_file(
            library,
            name,
            status=status,
            parse_lease_token="tok-dead",
            parse_lease_started_at=now - timedelta(minutes=90),
            parse_lease_expires_at=now + timedelta(minutes=lease_minutes),
        )

    return _make


def _fake_step(ok=True, detail="处理完成"):
    return SimpleNamespace(ok=ok, detail=detail)


def _patch_process_file(monkeypatch, behaviour):
    """把流水线的 ``process_file`` 换成受控替身；返回调用记录列表。"""
    calls: list[int] = []

    def _fake(record):
        calls.append(record.pk)
        return behaviour(record)

    import apps.knowledge.pipeline as pipeline

    monkeypatch.setattr(pipeline, "process_file", _fake)
    return calls


# ------------------------------------------------------------------ 正例 ----


def test_expired_lease_is_actually_advanced(admin_user, stalled, monkeypatch):
    record = stalled()
    calls = _patch_process_file(monkeypatch, lambda row: _fake_step(True, "解析完成"))

    result = services.execute_operation(admin_user, _armed(admin_user, {}))["result"]

    assert calls == [record.pk], "必须真的调用流水线推进，而不是只挑出 ID"
    assert result["count"] == 1
    assert result["processed"] == 1
    assert result["checked"] == 1
    assert result["failed"] == 0 and result["lease_busy"] == 0 and result["incomplete"] == 0
    assert result["items"][0]["status"].startswith("已推进")
    assert "实际推进 1 份" in result["detail"]


def test_lease_busy_is_reported_and_changes_nothing(admin_user, stalled, monkeypatch):
    record = stalled()
    _patch_process_file(
        monkeypatch,
        lambda row: (_ for _ in ()).throw(LeaseNotAcquired("租约被 #7 持有")),
    )

    result = services.execute_operation(admin_user, _armed(admin_user, {}))["result"]

    assert result["lease_busy"] == 1
    assert result["processed"] == 0 and result["count"] == 0
    assert result["items"][0]["status"] == "租约被占，未改动"
    assert "租约被占跳过 1 份" in result["detail"]

    # **未取得租约的资料一个字节都不改**
    record.refresh_from_db()
    assert record.parse_status == ParseStatus.RUNNING
    assert record.parse_attempts == 0
    assert (record.parse_error or "") == ""


def test_pipeline_error_is_reported_as_failed(admin_user, stalled, monkeypatch):
    stalled()
    _patch_process_file(
        monkeypatch,
        lambda row: (_ for _ in ()).throw(PipelineError("模型不可用：embedding 503")),
    )

    result = services.execute_operation(admin_user, _armed(admin_user, {}))["result"]

    assert result["failed"] == 1
    assert result["processed"] == 0
    assert result["items"][0]["status"].startswith("失败")
    assert "模型不可用" in result["items"][0]["detail"]
    assert "失败 1 份" in result["detail"]


def test_incomplete_step_is_not_a_success(admin_user, stalled, monkeypatch):
    stalled()
    _patch_process_file(monkeypatch, lambda row: _fake_step(False, "向量不完整：3/9"))

    result = services.execute_operation(admin_user, _armed(admin_user, {}))["result"]

    assert result["incomplete"] == 1
    assert result["processed"] == 0 and result["count"] == 0
    assert result["items"][0]["status"].startswith("未完成")
    assert "未完成 1 份" in result["detail"]


# ------------------------------------------------------------------ 边界 ----


def test_valid_lease_is_never_a_candidate(admin_user, stalled, monkeypatch):
    stalled(lease_minutes=+30)  # 租约仍然有效：别的 worker 正在正常处理
    calls = _patch_process_file(monkeypatch, lambda row: _fake_step(True))

    result = services.execute_operation(admin_user, _armed(admin_user, {}))["result"]

    assert calls == [], "租约有效的资料不许动"
    assert result["checked"] == 0 and result["count"] == 0
    assert "没有租约已过期的停滞资料" in result["detail"]


def test_pending_files_are_not_touched(admin_user, stalled, monkeypatch):
    stalled("排队中.md", status=ParseStatus.PENDING)
    calls = _patch_process_file(monkeypatch, lambda row: _fake_step(True))

    result = services.execute_operation(admin_user, _armed(admin_user, {}))["result"]

    assert calls == []
    assert result["checked"] == 0


def test_task_id_targets_one_file_only(admin_user, stalled, monkeypatch):
    first = stalled("A.md")
    stalled("B.md")
    calls = _patch_process_file(monkeypatch, lambda row: _fake_step(True))

    result = services.execute_operation(
        admin_user, _armed(admin_user, {"task_id": first.pk})
    )["result"]

    assert calls == [first.pk]
    assert result["checked"] == 1
    assert result["remaining"] == 0, "指定单份时不该报「还有别的没检查」"


def test_limit_is_reported_with_remaining(admin_user, stalled, monkeypatch):
    for index in range(services.STALLED_RESTART_LIMIT + 2):
        stalled(f"停滞-{index}.md")
    _patch_process_file(monkeypatch, lambda row: _fake_step(True))

    result = services.execute_operation(admin_user, _armed(admin_user, {}))["result"]

    assert result["checked"] == services.STALLED_RESTART_LIMIT
    assert result["remaining"] == 2
    assert "仍有 2 份未检查" in result["detail"]


# ------------------------------------------------------------ 形状与权限 ----


def test_result_shape_never_claims_restarted(admin_user, stalled, monkeypatch):
    stalled()
    _patch_process_file(monkeypatch, lambda row: _fake_step(True))

    result = services.execute_operation(admin_user, _armed(admin_user, {}))["result"]

    assert "restarted" not in result, "旧的假绿灯字段必须消失"
    assert set(result) >= {
        "count", "detail", "checked", "processed", "failed",
        "lease_busy", "incomplete", "remaining", "items", "note",
    }
    assert "实际推进" in result["note"]


def test_non_admin_cannot_propose(db, user, library):
    with pytest.raises(services.PlannerError):
        services.propose_operation(user, intent=INTENT, params={})
