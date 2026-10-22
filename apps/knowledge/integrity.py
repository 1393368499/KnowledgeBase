"""巡检留痕与**真实**完整性判定 —— 看门狗 / 结果校验 / 双阶段补扫的判定来源。

与 ``apps/knowledge/metrics.py`` 同一原则：

  * **只报能真正测到的值**（没有任何占位常量）；
  * 每个结论都带**来源与口径**（哪次巡检、检查了什么），界面照实显示；
  * 没有数据就说"还没有"，**不拿别的能力顶替**
    （例如不能拿"就绪检查"冒充"结果校验"，也不能拿"队列深度"冒充"补扫"）。

"看门狗"在本系统里的真实含义 = ``knowledge.patrol`` 这个**只读的周期巡检**：
它检查四项 —— 重试上限用尽 / 疑似停滞 / 向量不完整 / 原件缺失。
过去它的结论只写进 worker 日志（会滚动、界面无法复盘），于是三张状态卡长期只能
显示"暂无状态"。``PatrolRun`` 把每次结论**追加留痕**后，这三张卡就有了可复盘的
真实来源：

  * **解析看门狗** ← 最近一次巡检的时间、检查份数、问题总数；
  * **结果校验** ← 同一次巡检的**一致性**项（向量完整性 / 原件哈希 / 重试上限）；
  * **双阶段补扫** ← **实时**查询的两阶段候选（一阶段：待清扫 + 停滞；二阶段：向量不完整）。

注意"双阶段补扫"报的是**候选数量**（还有多少活要干），不是"已经补扫过" ——
真正执行补扫的入口是 ``sweep-pending-files``（一阶段）与重启停滞任务 / 重扫未处理反馈（二阶段）。
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

#: 巡检留痕最多保留多少条（超过就删最老的，避免无限增长）
PATROL_KEEP = 200
#: 每类问题明细最多留多少条
FINDINGS_KEEP = 20
#: 巡检认为"疑似停滞"的口径（分钟），与 config/celery.py 的 patrol 一致
STALE_AFTER_MINUTES = 30
#: 「看门狗心跳」判定阈值（分钟）：beat 的巡检周期是 900s（15 分钟），
#: 允许**连续两次**没跑再加一点余量；超过这个时长没有任何巡检留痕，
#: 就判定"心跳过期"（beat/scheduler 可能已经死了）——这是本系统里
#: 唯一能发现"调度器自己死了"的判据，不需要额外的看门狗进程。
WATCHDOG_STALE_MINUTES = 45


def net_negative_feedback_rows(
    allowed_library_ids: Sequence[int] | Iterable[int] | None = None,
) -> list[dict[str, Any]]:
    """净负分反馈的**唯一实现**（按资料聚合"准确/不准确"计数）。

    规则：净分 = 准确数 − 不准确数，**净分 < 0** 才算"被反复判不准确、值得重扫"。
    planner 的 ``list_feedback_targets``、``reparse_failed`` 与本模块的
    ``feedback_pending_count`` 共用它，避免两份口径漂移。
    """
    from django.db.models import Count, Q

    from .models import SearchFeedback

    queryset = SearchFeedback.objects.filter(file__isnull=False)
    if allowed_library_ids is not None:
        queryset = queryset.filter(file__library_id__in=list(allowed_library_ids))

    rows = queryset.values("file_id").annotate(
        accurate=Count("id", filter=Q(verdict=SearchFeedback.Verdict.ACCURATE)),
        inaccurate=Count("id", filter=Q(verdict=SearchFeedback.Verdict.INACCURATE)),
    )
    targets: list[dict[str, Any]] = []
    for row in rows:
        net = row["accurate"] - row["inaccurate"]
        if net < 0:
            targets.append(
                {
                    "file_id": row["file_id"],
                    "accurate": row["accurate"],
                    "inaccurate": row["inaccurate"],
                    "net": net,
                }
            )
    return targets


def feedback_pending_count(
    allowed_library_ids: Sequence[int] | Iterable[int] | None = None,
) -> int:
    """待处理反馈数 = 净负分资料的份数（真实查询，不是估算）。"""
    return len(net_negative_feedback_rows(allowed_library_ids))


def stalled_running_queryset():
    """**停滞候选**：``RUNNING`` 且租约为空或已过期（只读判断，不改任何字段）。

    与 planner 的 ``restart_stalled_task`` 用同一个口径 —— 那里是"可被真正重启的"，
    这里是"界面上的停滞任务数"。
    """
    from django.db.models import Q
    from django.utils import timezone

    from .models import IngestedFile, ParseStatus

    now = timezone.now()
    return IngestedFile._base_manager.filter(parse_status=ParseStatus.RUNNING).filter(
        Q(parse_lease_token="")
        | Q(parse_lease_expires_at__isnull=True)
        | Q(parse_lease_expires_at__lt=now)
    )


def stalled_running_count() -> int:
    return stalled_running_queryset().count()


def resweep_candidates() -> dict[str, Any]:
    """双阶段补扫的**实时候选**（两项都是真实查询）。

    一阶段（把还没被推进的资料捡起来）：``PENDING`` + 停滞的 ``RUNNING``；
    二阶段（把向量补齐）：``chunk_count > 0`` 但 ``embedded_chunk_count`` 落后。
    """
    from django.db.models import F

    from .models import IngestedFile, ParseStatus

    pending = IngestedFile._base_manager.filter(parse_status=ParseStatus.PENDING).count()
    stalled = stalled_running_count()
    vector_incomplete = IngestedFile._base_manager.filter(
        chunk_count__gt=0, embedded_chunk_count__lt=F("chunk_count")
    ).count()

    phase1 = pending + stalled
    return {
        "phase1": {"pending": pending, "stalled": stalled, "total": phase1},
        "phase2": {"vector_incomplete": vector_incomplete, "total": vector_incomplete},
        "total": phase1 + vector_incomplete,
        "note": "候选是**实时查询**：一阶段=待清扫+停滞，二阶段=向量未完成；"
        "真正的补扫入口是周期任务 sweep-pending-files 与「重启停滞任务 / 重扫未处理反馈」",
    }


def latest_patrol_run():
    """最近一次巡检留痕（没有就返回 None）。"""
    from .models import PatrolRun

    return PatrolRun.objects.order_by("-created_at", "-id").first()


def _trim_findings(findings: dict[str, Any]) -> dict[str, list]:
    """每类明细只留前 ``FINDINGS_KEEP`` 条，避免留痕行无限增长。"""
    trimmed: dict[str, list] = {}
    for name, items in (findings or {}).items():
        if isinstance(items, list):
            trimmed[str(name)] = items[:FINDINGS_KEEP]
        else:  # pragma: no cover - findings 一定是 list，防御性兜底
            trimmed[str(name)] = []
    return trimmed


def record_patrol_run(
    result: dict[str, Any], *, source: str = "beat", duration_ms: int = 0,
    keep: int = PATROL_KEEP,
):
    """把一次巡检结论**追加留痕**，并把超出 ``keep`` 的最老记录删掉。

    返回新建的 ``PatrolRun``。留痕失败**不影响**巡检本身的结论（调用方负责兜底）。
    """
    from .models import PatrolRun

    counts = {str(k): int(v or 0) for k, v in dict(result.get("counts") or {}).items()}
    issues = sum(counts.values())
    run = PatrolRun.objects.create(
        source=str(source or "beat")[:16],
        checked=int(result.get("checked") or 0),
        issues=issues,
        counts=counts,
        findings=_trim_findings(dict(result.get("findings") or {})),
        duration_ms=max(0, int(duration_ms or 0)),
    )
    if keep > 0:
        stale_ids = list(
            PatrolRun.objects.order_by("-created_at", "-id").values_list("id", flat=True)[keep:]
        )
        if stale_ids:
            PatrolRun.objects.filter(id__in=stale_ids).delete()
    return run


def patrol_snapshot() -> dict[str, Any]:
    """「解析看门狗」卡的判定来源：最近一次巡检留痕。

    除了"最近一次心跳距今多久"，这里还给出 ``overdue`` 判决：
    ``knowledge.patrol`` 由 beat 周期投递（900s），如果超过
    ``WATCHDOG_STALE_MINUTES`` 没有任何留痕，说明 beat/scheduler 很可能已经停了
    —— 这时**不能**把界面显示成"暂无状态"了事，必须报"心跳过期"。
    没有任何留痕时 ``overdue`` 为 ``None``（**无法判断**），而不是 ``False``：
    没有心跳不等于正常。
    """
    from django.utils import timezone

    run = latest_patrol_run()
    if run is None:
        return {
            "available": False,
            "implemented": True,
            "task": "knowledge.patrol",
            "overdue": None,
            "stale_after_minutes": WATCHDOG_STALE_MINUTES,
            "verdict": "无法判断：还没有任何巡检心跳；这不等于系统正常",
            "note": "还没有巡检记录：等待周期任务（knowledge.patrol）或手动执行 "
            "`manage.py kbpatrol`",
        }
    age = max(0, int((timezone.now() - run.created_at).total_seconds()))
    overdue = age > WATCHDOG_STALE_MINUTES * 60
    minutes = age // 60
    return {
        "available": True,
        "implemented": True,
        "task": "knowledge.patrol",
        "at": run.created_at.isoformat(),
        "age_seconds": age,
        "overdue": overdue,
        "source": run.source,
        "checked": run.checked,
        "issues": run.issues,
        "status": run.status,
        "counts": run.counts,
        "duration_ms": run.duration_ms,
        "stale_after_minutes": WATCHDOG_STALE_MINUTES,
        "verdict": (
            f"心跳过期：最近一次巡检是 {minutes} 分钟前（阈值 "
            f"{WATCHDOG_STALE_MINUTES} 分钟），beat/scheduler 可能已经停了"
            if overdue else
            f"心跳正常：最近一次巡检是 {minutes} 分钟前"
        ),
        "note": "来源：最近一次巡检留痕（knowledge.patrol，只读检查，未修改任何资料）",
    }


def verification_snapshot() -> dict[str, Any]:
    """「结果校验」卡的判定来源：巡检里的**一致性**项。

    只取能代表"结果与原件/向量是否一致"的三项：向量完整性、原件哈希、重试上限用尽。
    "疑似停滞"属于活性问题，不算结果不一致，因此不计入本卡。
    """
    run = latest_patrol_run()
    if run is None:
        return {
            "available": False,
            "note": "还没有校验记录：等待巡检（knowledge.patrol）或手动执行 "
            "`manage.py kbpatrol`",
        }
    counts = run.counts or {}
    detail = {
        "vector_incomplete": int(counts.get("vector_incomplete") or 0),
        "blob_missing": int(counts.get("blob_missing") or 0),
        "failed_exhausted": int(counts.get("failed_exhausted") or 0),
    }
    mismatches = sum(detail.values())
    return {
        "available": True,
        "ok": mismatches == 0,
        "at": run.created_at.isoformat(),
        "checked": run.checked,
        "mismatches": mismatches,
        "detail": detail,
        "note": "来源：最近一次巡检的一致性检查项（向量完整性 / 原件哈希 / 重试上限）",
    }
