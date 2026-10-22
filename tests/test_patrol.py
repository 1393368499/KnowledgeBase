"""真实周期巡检（不得用"暂无状态"或假绿灯交付）。

`knowledge.patrol` 是**只读**巡检：检查资料与索引的一致性并返回可复盘清单。
本用例用真实数据构造四类异常，证明巡检**真的能发现它们**，且不修改任何资料。
"""

from __future__ import annotations

from apps.knowledge.models import IngestedFile


def _patrol():
    from config.celery import patrol

    # 直接调用任务函数体（不经过 broker），得到真实返回
    return patrol.run(limit=500)


def test_patrol_registered_and_scheduled():
    from config.celery import app

    assert "knowledge.patrol" in app.tasks, "任务未注册 → beat 调度会失败"
    from django.conf import settings

    schedule = getattr(settings, "CELERY_BEAT_SCHEDULE", {})
    assert "patrol-integrity" in schedule, "巡检未进入 beat 调度"
    assert schedule["patrol-integrity"]["task"] == "knowledge.patrol"


def test_patrol_reports_failed_exhausted(db, make_library, make_file, user):
    library = make_library(code="patrol-lib", name="巡检库")
    row = make_file(library=library, name="bad.txt")
    IngestedFile._base_manager.filter(pk=row.pk).update(
        parse_status="failed", parse_attempts=row.MAX_PARSE_ATTEMPTS, parse_error="坏文件"
    )
    result = _patrol()
    ids = [item["file_id"] for item in result["findings"]["failed_exhausted"]]
    assert row.pk in ids, result["counts"]
    assert result["counts"]["failed_exhausted"] >= 1


def test_patrol_reports_vector_incomplete(db, make_library, make_file):
    library = make_library(code="patrol-vec", name="向量不完整库")
    row = make_file(library=library, name="partial.txt")
    IngestedFile._base_manager.filter(pk=row.pk).update(chunk_count=5, embedded_chunk_count=2)
    result = _patrol()
    ids = [item["file_id"] for item in result["findings"]["vector_incomplete"]]
    assert row.pk in ids, result["counts"]
    assert result["counts"]["vector_incomplete"] >= 1


def test_patrol_does_not_modify_material(db, make_library, make_file):
    """巡检必须**只读**：不得改动任何资料字段。"""
    library = make_library(code="patrol-ro", name="只读库")
    row = make_file(library=library, name="ro.txt")
    IngestedFile._base_manager.filter(pk=row.pk).update(chunk_count=3, embedded_chunk_count=1)
    before = IngestedFile._base_manager.get(pk=row.pk)
    before_state = (before.parse_status, before.chunk_count, before.embedded_chunk_count,
                    before.library_id, before.content_sha256)
    _patrol()
    after = IngestedFile._base_manager.get(pk=row.pk)
    assert (after.parse_status, after.chunk_count, after.embedded_chunk_count,
            after.library_id, after.content_sha256) == before_state


def test_patrol_clean_when_nothing_wrong(db, make_library, make_file):
    library = make_library(code="patrol-ok", name="正常库")
    row = make_file(library=library, name="ok.txt")
    IngestedFile._base_manager.filter(pk=row.pk).update(
        parse_status="done", chunk_count=2, embedded_chunk_count=2
    )
    result = _patrol()
    assert result["counts"]["vector_incomplete"] == 0
    assert result["counts"]["failed_exhausted"] == 0


# ------------------------------------------------- 停滞判据取哪个时间字段 ----
#
# 回归背景：这里曾经读 `parse_started_at` —— **模型里没有这个字段**，
# `getattr(..., None)` 永远为 None，于是永远回落到 `created_at`。
# 结果是一份"创建于 3 小时前、1 分钟前才真正开始重跑"的 RUNNING 资料
# 会被误报停滞（判据用错时间维度）。


def test_patrol_stalled_uses_lease_started_at_not_created_at(db, make_library, make_file):
    """RUNNING 的"未推进"必须看 `parse_lease_started_at`，不看创建时间。"""
    from datetime import timedelta
    from unittest import mock

    from django.utils import timezone

    library = make_library(code="patrol-stall-fresh", name="刚重跑库")
    now = timezone.now()
    # created_at 是 auto_now_add 且受不可变保护（queryset.update 会被拒），
    # 只能用"创建当时的时钟"造出"3 小时前创建、现在才重新处理"的资料。
    with mock.patch("django.utils.timezone.now", return_value=now - timedelta(hours=3)):
        row = make_file(
            library=library,
            name="old-but-running.txt",
            status="running",
            parse_lease_token="alive",
            parse_lease_started_at=now - timedelta(minutes=1),
            parse_lease_expires_at=now - timedelta(seconds=5),  # 租约刚过期，但确实刚在跑
        )
    assert IngestedFile._base_manager.get(pk=row.pk).created_at < now - timedelta(hours=2)

    stalled = [item["file_id"] for item in _patrol()["findings"]["stalled"]]
    assert row.pk not in stalled, "推进时间才 1 分钟，不能因为创建于 3 小时前就报停滞"


def test_patrol_reports_real_stalls_with_the_right_since(db, make_library, make_file):
    """真停滞要报，且报出的 `since` 是**开始推进的时刻**（不是创建时刻）。

    PENDING 从未开始，"等待时间"就是 created_at —— 两套参照点都必须正确。
    """
    from datetime import timedelta
    from unittest import mock

    from django.utils import timezone

    library = make_library(code="patrol-stall-dead", name="真停滞库")
    now = timezone.now()
    started = now - timedelta(minutes=90)
    dead = make_file(
        library=library,
        name="really-stalled.txt",
        status="running",
        parse_lease_token="dead",
        parse_lease_started_at=started,
        parse_lease_expires_at=now - timedelta(minutes=30),
    )
    with mock.patch("django.utils.timezone.now", return_value=now - timedelta(hours=2)):
        waiting = make_file(library=library, name="waiting-long.txt", status="pending")
    fresh = make_file(library=library, name="just-queued.txt", status="pending")

    result = _patrol()
    stalled = {item["file_id"]: item for item in result["findings"]["stalled"]}

    assert dead.pk in stalled, "租约已死 30 分钟且推进停在 90 分钟前，必须报"
    assert waiting.pk in stalled, "PENDING 从未开始，等待时间就是 created_at"
    assert fresh.pk not in stalled, "刚入队的 PENDING 不是停滞"
    assert set(stalled) == {dead.pk, waiting.pk}, result["counts"]

    since = timezone.datetime.fromisoformat(stalled[dead.pk]["since"])
    assert abs((since - started).total_seconds()) < 5, (
        f"since 必须是租约开始时刻（{started.isoformat()}），实际 {since.isoformat()}"
    )
    assert stalled[dead.pk]["status"] == "running"
