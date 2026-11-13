"""巡检留痕 + "真实判定来源"（看门狗 / 结果校验 / 双阶段补扫 / 立即巡检）。

背景（这组测试要锁死的口径）：界面的 Agent 控制台曾有 3 张状态卡与 3 个指标是
「暂无状态 / —」占位，文档里的规则是"**每项给出真实判定来源；没有就保持灰点，
不得伪造**"。现在判定来源真的有了：

  * 「解析看门狗」「结果校验」= 最近一次巡检的**留痕行**（knowledge.PatrolRun）；
  * 「双阶段补扫」「停滞任务」「待处理反馈」= **实时查询**；
  * 「立即巡检」= 立刻跑一次与周期任务**同一实现**的只读巡检，并留痕。

因此这里断言：有读数就必须有真实来源；没跑过巡检就 ``available=False`` + 说明原因。
"""

from __future__ import annotations

import io
from datetime import timedelta
from unittest import mock

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.knowledge import integrity
from apps.knowledge.models import ParseStatus, PatrolRun, SearchFeedback
from apps.planner import services
from apps.planner.operations import get

# --------------------------------------------------------------- 巡检留痕 -------


def test_patrol_snapshot_is_unavailable_until_a_run_exists(db):
    """没跑过巡检 → 卡片**没有**读数（灰点），但能力已实现且说明原因。"""
    snapshot = integrity.patrol_snapshot()

    assert snapshot["available"] is False
    assert snapshot["implemented"] is True
    assert snapshot["task"] == "knowledge.patrol"
    assert "kbpatrol" in snapshot["note"], snapshot["note"]


def test_record_patrol_run_makes_cards_available(db):
    """留痕之后，看门狗/结果校验两张卡才**真的有读数**。"""
    integrity.record_patrol_run(
        {"checked": 12, "counts": {"vector_incomplete": 2, "stalled": 1}, "findings": {}},
        source="manual",
        duration_ms=42,
    )

    watchdog = integrity.patrol_snapshot()
    assert watchdog["available"] is True
    assert watchdog["source"] == "manual"
    assert watchdog["checked"] == 12
    assert watchdog["issues"] == 3
    assert watchdog["status"] == "issues"
    assert watchdog["duration_ms"] == 42
    assert watchdog["age_seconds"] >= 0

    verification = integrity.verification_snapshot()
    assert verification["available"] is True
    assert verification["ok"] is False
    assert verification["mismatches"] == 2, "只有一致性项算不一致"
    assert verification["detail"] == {
        "vector_incomplete": 2,
        "blob_missing": 0,
        "failed_exhausted": 0,
    }, verification["detail"]


def test_watchdog_heartbeat_reports_overdue_when_beat_is_dead(db):
    """心跳过期必须能判定：这是本系统里唯一能发现"调度器自己死了"的判据。"""
    stale = timezone.now() - timedelta(
        minutes=integrity.WATCHDOG_STALE_MINUTES + 5
    )
    with mock.patch("django.utils.timezone.now", return_value=stale):
        integrity.record_patrol_run(
            {"checked": 1, "counts": {}, "findings": {}}, source="beat"
        )

    watchdog = integrity.patrol_snapshot()

    assert watchdog["available"] is True
    assert watchdog["overdue"] is True
    assert "过期" in watchdog["verdict"], watchdog["verdict"]
    assert watchdog["stale_after_minutes"] == integrity.WATCHDOG_STALE_MINUTES


def test_watchdog_heartbeat_is_not_overdue_right_after_a_run(db):
    integrity.record_patrol_run(
        {"checked": 1, "counts": {}, "findings": {}}, source="beat"
    )

    watchdog = integrity.patrol_snapshot()

    assert watchdog["overdue"] is False
    assert "正常" in watchdog["verdict"], watchdog["verdict"]


def test_watchdog_without_any_heartbeat_is_unknown_not_healthy(db):
    """没有任何留痕时 ``overdue`` 是 ``None``（无法判断），**不是** False（正常）。"""
    watchdog = integrity.patrol_snapshot()

    assert watchdog["available"] is False
    assert watchdog["overdue"] is None
    assert "无法判断" in watchdog["verdict"], watchdog["verdict"]


def test_stalled_alone_is_not_a_result_mismatch(db):
    """"疑似停滞"是活性问题，**不算结果不一致**（否则结果校验会被误报）。"""
    integrity.record_patrol_run({"checked": 3, "counts": {"stalled": 5}, "findings": {}})

    verification = integrity.verification_snapshot()
    assert verification["available"] is True
    assert verification["ok"] is True, verification
    assert verification["mismatches"] == 0


def test_record_patrol_run_trims_old_rows(db):
    """留痕行不能无限增长：超过 keep 的最老记录被删掉。"""
    for index in range(4):
        integrity.record_patrol_run({"checked": index, "counts": {}, "findings": {}}, keep=3)

    assert PatrolRun.objects.count() == 3
    assert [run.checked for run in PatrolRun.objects.order_by("id")] == [1, 2, 3]


def test_findings_are_trimmed_per_category(db):
    """每类问题明细只留 20 条，避免一行 JSON 无限膨胀。"""
    findings = {"vector_incomplete": list(range(25)), "blob_missing": [1, 2]}
    run = integrity.record_patrol_run({"checked": 1, "counts": {}, "findings": findings})

    assert len(run.findings["vector_incomplete"]) == integrity.FINDINGS_KEEP == 20
    assert run.findings["blob_missing"] == [1, 2]


# --------------------------------------------------- 净负分口径（唯一实现） ----


def _feedback(user, record, verdict, count=1, start=0):
    """同一 (user, query, file, chunk) 只能有一条反馈（库里有唯一约束），
    所以多份计数用不同的 chunk_ordinal —— 这也符合真实语义：命中分片不同。"""
    return [
        SearchFeedback.objects.create(
            user=user,
            query="质保期",
            file=record,
            chunk_ordinal=start + index,
            verdict=verdict,
        )
        for index in range(count)
    ]


def test_net_negative_rule_is_the_single_source(db, user, library, make_file):
    """净分 < 0 才算"值得重扫"；净分 >= 0 的一律不收。"""
    bad = make_file(library, "老被说不准.txt")
    good = make_file(library, "老被说准.txt")
    _feedback(user, bad, SearchFeedback.Verdict.INACCURATE, count=3)
    _feedback(user, bad, SearchFeedback.Verdict.ACCURATE, count=1, start=10)
    _feedback(user, good, SearchFeedback.Verdict.ACCURATE, count=2)

    rows = integrity.net_negative_feedback_rows()

    assert [row["file_id"] for row in rows] == [bad.pk]
    assert rows[0] == {"file_id": bad.pk, "accurate": 1, "inaccurate": 3, "net": -2}
    assert integrity.feedback_pending_count() == 1


def test_feedback_pending_honours_library_scope(db, user, library, make_file):
    """按可读库过滤时不能把别的库的净负分算进来。"""
    record = make_file(library, "范围外.txt")
    _feedback(user, record, SearchFeedback.Verdict.INACCURATE, count=2)

    assert integrity.feedback_pending_count([library.pk]) == 1
    assert integrity.feedback_pending_count([library.pk + 999]) == 0


# ------------------------------------------------- 巡检候选 / 停滞 实时查询 ----


def test_resweep_candidates_counts_real_rows(db, library, make_file):
    """一阶段=待清扫+停滞；二阶段=向量未完成；两项都来自真实查询。"""
    make_file(library, "待清扫.txt", status=ParseStatus.PENDING)
    make_file(
        library,
        "停滞.txt",
        status=ParseStatus.RUNNING,
        parse_lease_token="dead",
        parse_lease_started_at=timezone.now() - timezone.timedelta(minutes=90),
        parse_lease_expires_at=timezone.now() - timezone.timedelta(minutes=30),
    )
    make_file(library, "向量落后.txt", chunk_count=3, embedded_chunk_count=1)
    make_file(library, "已处理.txt", status=ParseStatus.DONE)

    candidates = integrity.resweep_candidates()

    assert candidates["phase1"] == {"pending": 1, "stalled": 1, "total": 2}, candidates
    assert candidates["phase2"]["vector_incomplete"] == 1
    assert candidates["total"] == 3
    assert candidates["note"], "必须说明候选口径与真正的补扫入口"


def test_stalled_running_count_requires_dead_lease(db, library, make_file):
    """租约仍然有效的 RUNNING **不算**停滞（不能把正在跑的算成停滞）。"""
    make_file(
        library,
        "正在跑.txt",
        status=ParseStatus.RUNNING,
        parse_lease_token="alive",
        parse_lease_started_at=timezone.now() - timezone.timedelta(minutes=1),
        parse_lease_expires_at=timezone.now() + timezone.timedelta(minutes=10),
    )
    assert integrity.stalled_running_count() == 0

    make_file(library, "死租约.txt", status=ParseStatus.RUNNING, parse_lease_token="")
    assert integrity.stalled_running_count() == 1


# --------------------------------------------------------- 立即巡检（意图） ----


def _armed(user, intent, params):
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


def _execute(user, intent, params=None):
    return services.execute_operation(user, _armed(user, intent, params or {}))["result"]


def test_run_patrol_is_registered_as_readonly_operation():
    """「立即巡检」必须登记，且是只读低风险、无需确认（它不修改任何资料）。"""
    spec = get("run_patrol")
    assert spec is not None, "意图未登记 → 界面点「立即巡检」会提案失败"
    assert spec.requires_confirmation is False
    assert spec.requires_destructive_flow is False
    assert spec.permission == "system:operate"


def test_run_patrol_requires_system_operator(db, user, library, grant_member):
    """普通用户不能触发系统级巡检。"""
    grant_member(library, user)

    with pytest.raises(services.PlannerError):
        services.propose_operation(user, intent="run_patrol", params={})


def test_run_patrol_calls_the_same_task_and_reports_truthfully(db, admin_user, monkeypatch):
    """「立即巡检」复用 knowledge.patrol（不另写一套检查），并如实转述结论。"""
    calls = {}

    def fake_patrol(limit=200, source="beat"):
        calls["limit"] = limit
        calls["source"] = source
        return {
            "ok": True,
            "checked": 9,
            "counts": {"stalled": 1, "blob_missing": 2},
            "findings": {"blob_missing": [{"file_id": 1}]},
        }

    monkeypatch.setattr("knowledgebase.celery.patrol", fake_patrol)

    payload = _execute(admin_user, "run_patrol", {"limit": 25})

    assert calls == {"limit": 25, "source": "manual"}, calls
    assert payload["checked"] == 9
    assert payload["issues"] == 3
    assert payload["counts"] == {"stalled": 1, "blob_missing": 2}
    assert "3 项待处理" in payload["detail"], payload["detail"]
    assert "只读" in payload["note"], payload["note"]


def test_run_patrol_leaves_a_trace_so_the_cards_have_data(db, admin_user):
    """真跑一次（不 mock）：必须留下 PatrolRun，卡片立刻有真实读数。"""
    payload = _execute(admin_user, "run_patrol", {})

    run = PatrolRun.objects.get()
    assert run.source == "manual"
    assert payload["watchdog"]["available"] is True
    assert payload["watchdog"]["source"] == "manual"
    assert payload["watchdog"]["checked"] == run.checked
    assert payload["verification"]["available"] is True
    # 空库巡检：四项都是 0，如实说"未发现异常"
    assert payload["counts"] == {
        "failed_exhausted": 0,
        "stalled": 0,
        "vector_incomplete": 0,
        "blob_missing": 0,
    }, payload["counts"]


def test_run_patrol_rejects_bad_limit(db, admin_user):
    with pytest.raises(services.PlannerError):
        _execute(admin_user, "run_patrol", {"limit": "abc"})


def test_kbpatrol_command_runs_the_same_task_and_records(db):
    """管理命令走**同一个**巡检实现，并且同样留痕（否则卡片还是没读数）。"""
    stdout = io.StringIO()

    call_command("kbpatrol", "--limit", "10", stdout=stdout)

    output = stdout.getvalue()
    assert "巡检" in output, output
    run = PatrolRun.objects.get()
    assert run.source == "manual"
    assert run.checked <= 10


# ------------------------------------------------------ 指标里的真实读数 -------


def test_metrics_expose_real_readings(db, user, library, make_file):
    """运行指标里的停滞/待反馈/补扫候选都必须是真实查询结果。"""
    record = make_file(library, "被投诉.txt")
    _feedback(user, record, SearchFeedback.Verdict.INACCURATE, count=2)
    make_file(library, "待清扫.txt", status=ParseStatus.PENDING)
    integrity.record_patrol_run({"checked": 2, "counts": {}, "findings": {}})

    from apps.knowledge.metrics import collect

    payload = collect()

    assert payload["feedback_pending"] == 1
    assert payload["resweep"]["phase1"]["pending"] == 1
    assert payload["resweep"]["total"] >= 1
    assert isinstance(payload["stalled_tasks"], int)
    assert payload["watchdog"]["available"] is True
    assert payload["verification"]["available"] is True
    assert payload["unavailable"] == [], payload["unavailable"]


def test_list_feedback_targets_uses_the_same_rule(db, admin_user, library, make_file, grant_member):
    """planner 的「查看待重扫资料」与 integrity 的净负分口径**必须一致**。"""
    grant_member(library, admin_user)
    bad = make_file(library, "老被说不准.txt")
    good = make_file(library, "老被说准.txt")
    _feedback(admin_user, bad, SearchFeedback.Verdict.INACCURATE, count=3)
    _feedback(admin_user, good, SearchFeedback.Verdict.ACCURATE, count=1)

    payload = _execute(admin_user, "list_feedback_targets", {})

    expected = sorted(row["file_id"] for row in integrity.net_negative_feedback_rows())
    assert sorted(item["file_id"] for item in payload["items"]) == expected == [bad.pk]
    assert payload["items"][0]["net"] == -3
