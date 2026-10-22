"""Celery 应用与入库任务。

上传接口的上传模式（``KB_UPLOAD_PROCESS_MODE``）：

- ``inline``：请求内同步跑完整流水线（老行为；本地验收方便，但远端服务掉线时会
  长时间阻塞请求，与并发要求冲突）；
- ``queue``：**只落盘 + 建记录**，立刻返回 202 + 待处理状态，由 worker 处理
  （失败不丢原件，服务恢复后重试）；
- ``auto``（默认）：能派发就派发，派发不了（没装/没配 broker）就**明确返回待处理**，
  调用方可以随时用 ``manage.py kbprocess`` 或重试接口推进——绝不假装已完成。

队列化不等于"不处理"：待处理状态在库里是可查的，缺 worker 时界面显示"待处理"。
"""

from __future__ import annotations

import logging
import os
import time

from celery import Celery
from django.conf import settings

logger = logging.getLogger(__name__)

app = Celery("knowledgebase")
app.config_from_object("django.conf:settings", namespace="CELERY")
# 兜底：即使没走到 config_from_object，也要保证序列化与 eager 口径正确。
# 注意 eager 用 settings 的当前值（不是 import 期快照），这样测试里
# override_settings 能真正影响行为。
app.conf.update(
    broker_url=getattr(settings, "CELERY_BROKER_URL", ""),
    result_backend=getattr(settings, "CELERY_RESULT_BACKEND", ""),
    task_always_eager=bool(getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False)),
    broker_transport_options=dict(
        getattr(settings, "CELERY_BROKER_TRANSPORT_OPTIONS", {}) or {}
    ),
    task_serializer="json",
    accept_content=["json"],
    timezone=getattr(settings, "TIME_ZONE", "Asia/Shanghai"),
    enable_utc=True,
)

# 专用队列路由（**条件启用**，默认行为完全不变）。
#
# 为什么要专用队列：业务 Redis 里还有历史遗留消息，新 worker 用 ``-Q`` 只消费
# 自己的队列，才不会把别人的消息消费/清空。设置 ``KB_CELERY_QUEUE`` 时才启用；
# 不设置时一行路由都不加，现有部署与测试不受影响。
_DEDICATED_QUEUE = os.environ.get("KB_CELERY_QUEUE", "").strip()
if _DEDICATED_QUEUE:
    app.conf.update(
        task_queues={
            _DEDICATED_QUEUE: {
                "exchange": _DEDICATED_QUEUE,
                "routing_key": _DEDICATED_QUEUE,
            }
        },
        task_default_queue=_DEDICATED_QUEUE,
        # 路由键必须匹配**真实注册的任务名**：本应用的任务是
        # "knowledge.process_ingested_file" / "knowledge.sweep_pending"
        # （见下方 @app.task(name=...)）。写成 "apps.*" 会让任务
        # **根本进不了**专用队列 —— 这是独立审查抓到的真实缺陷。
        task_routes={"knowledge.*": {"queue": _DEDICATED_QUEUE}},
        # ---- 崩溃恢复（审查第 12 条）----
        # 为什么必须开 acks_late：默认"收到即确认"——worker 在任务执行中途被杀，
        # 消息已经确认、任务**永久丢失**（容器重启/被杀就是这种场景）。
        # 开了之后消息在**任务成功返回后**才确认，配合 reject_on_worker_lost，
        # worker 进程意外死亡时该消息会被重新投递。
        #
        # 之所以安全：本系统的任务处理是**幂等**的 ——
        #   * 处理前用数据库租约（带 token 的 CAS）抢占用，重复投递只会拿到 busy；
        #   * 原件内容寻址，重复写命中同一路径并校验复用，不会产生第二份字节；
        #   * 向量写入按 generation + 分段序号续写，不推倒重来。
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        # 失败也要 ack（否则会无限重投）；重试由任务内的**有限重试**控制。
        task_acks_on_failure_or_timeout=True,
    )


@app.task(name="knowledge.process_ingested_file", bind=True, max_retries=3)
def process_ingested_file(
    self,
    file_id: int,
    *,
    content_sha256: str = "",
    library_code: str = "",
) -> dict:
    """队列任务：处理一份已落盘的资料（解析 → 分段 → 嵌入 → 写索引）。

    ``content_sha256`` / ``library_code`` 是**必填的绑定信息**（审查意见）。

    为什么必填而不是可选：队列里可能积压着**很久以前**的消息（例如早期版本
    发出去的、只带 ``file_id`` 的消息）。如果绑定可选，这些旧消息就会被当成
    合法任务、照 ``file_id`` 执行——而 ``file_id`` 会被重建/复用，结果就是
    "按主库的 id 处理了一份完全不相干的资料"。

    所以口径是：**缺绑定信息一律拒绝执行**，并把原因返回（明确"需重新发起"）。
    我们**不 purge 队列**——删别人的消息风险更大；正确做法是让它自然出队并被
    安全拒绝。
    """
    from apps.knowledge.models import IngestedFile, KnowledgeLibrary
    from apps.knowledge.pipeline import (
        LeaseNotAcquired,
        PipelineError,
        process_file,
    )

    if not content_sha256 or not library_code:
        logger.warning(
            "拒绝执行缺少绑定信息的旧消息（file_id=%s hash=%s library=%s）："
            "请用当前接口重新发起处理",
            file_id,
            bool(content_sha256),
            library_code or "(空)",
        )
        return {
            "ok": False,
            "reason": "missing_binding",
            "detail": "消息缺少 content_sha256/library_code 绑定信息，需重新发起处理",
            "file_id": file_id,
        }

    record = IngestedFile._base_manager.filter(pk=file_id).first()
    if record is None:
        return {"ok": False, "reason": "not_found", "file_id": file_id}
    # 绑定校验：消息里的指纹必须与当前记录一致
    if record.content_sha256 != content_sha256:
        logger.warning(
            "拒绝消费：消息绑定指纹 %s… 与库内 %s… 不一致（file_id=%s 可能已被重建）",
            content_sha256[:12],
            (record.content_sha256 or "")[:12],
            file_id,
        )
        return {"ok": False, "reason": "payload_mismatch", "file_id": file_id}
    actual = KnowledgeLibrary.objects.filter(pk=record.library_id).values_list(
        "code", flat=True
    ).first()
    if actual != library_code:
        logger.warning(
            "拒绝消费：消息声明库 %s，但 file_id=%s 属于库 %s", library_code, file_id, actual
        )
        return {"ok": False, "reason": "library_mismatch", "file_id": file_id}

    try:
        step = process_file(record)
    except LeaseNotAcquired as exc:
        # 另一个 worker 正在处理同一份资料：这是**正常**结果，不是失败
        return {"ok": False, "reason": "busy", "detail": str(exc), "file_id": file_id}
    except PipelineError as exc:
        logger.warning("队列处理失败 file=%s：%s", file_id, exc)
        # **有限重试**（审查第 12 条）：解析/嵌入类失败多为瞬时（模型服务抖动、临时 IO）。
        # 关键点：
        #   * `LeaseNotAcquired`（上面已单独处理）**不重试** —— 那不是失败，是别人在做；
        #   * 重试次数有上限（max_retries=3），退避递增且封顶，避免把 AS711 打爆；
        #   * 用尽后返回**明确失败**（带 retries_exhausted），由资料状态与巡检接管，
        #     绝不伪装成功。
        if self.request.retries < self.max_retries:
            countdown = min(30 * (self.request.retries + 1), 120)
            logger.warning(
                "有限重试 file=%s：第 %s/%s 次，%s 秒后再试",
                file_id,
                self.request.retries + 1,
                self.max_retries,
                countdown,
            )
            raise self.retry(exc=exc, countdown=countdown)
        return {
            "ok": False,
            "reason": str(exc),
            "file_id": file_id,
            "retries_exhausted": True,
        }
    return {"ok": step.ok, "detail": step.detail, "metrics": step.metrics, "file_id": file_id}


def converge_stale_executing_operations(limit: int = 50) -> dict:
    """把**僵死**的 ``executing`` 操作收敛为 ``failed``（自动收尾，全程留审计）。

    为什么需要它：执行端把状态 CAS 到 ``executing`` 之后就写回结果，
    但进程被 SIGKILL / 容器重启会**跳过**那一步；而 ``executing`` 不是终态，
    再执行要求状态是 ``ARMED``（会被 CAS 挡住）—— 没有这道收尾，那条待办
    会永久显示"执行中"。这里**只改状态 + 记审计事件**，绝不重放任何动作：
    重新执行同一件事必须由用户重新提案、重新确认。

    返回 ``{"converged": n, "operation_ids": [...]}``，供 beat 日志与界面复盘。
    """
    from datetime import timedelta

    from django.utils import timezone

    from apps.audit.services import append_event
    from apps.planner.models import OperationRecord

    now = timezone.now()
    deadline = now - timedelta(seconds=OperationRecord.EXECUTING_STALE_SECONDS)
    stale = OperationRecord.objects.filter(
        status=OperationRecord.Status.EXECUTING, updated_at__lte=deadline
    ).order_by("pk")[:limit]

    converged: list[int] = []
    for operation in stale:
        # CAS 到自己这一条：只有仍处于 executing 的那一行会被改（并发安全）
        changed = OperationRecord.objects.filter(
            pk=operation.pk, status=OperationRecord.Status.EXECUTING
        ).update(
            status=OperationRecord.Status.FAILED,
            error=(
                "执行进程超过 "
                f"{OperationRecord.EXECUTING_STALE_SECONDS // 60} 分钟没有写回结果"
                "（疑似被杀/容器重启），已由周期扫尾收敛为失败；"
                "如需继续请重新发起提案并重新确认。"
            ),
            updated_at=now,
        )
        if not changed:
            continue
        append_event(
            action="planner.operation_converged_stale",
            actor_id=None,
            resource_kind="operation",
            resource_key=str(operation.pk),
            payload={
                "operation_id": operation.pk,
                "intent": operation.intent,
                "previous_status": OperationRecord.Status.EXECUTING,
                "stale_seconds": OperationRecord.EXECUTING_STALE_SECONDS,
                "target_ref": operation.target_ref,
                "target_label": operation.target_label,
            },
        )
        converged.append(operation.pk)

    if converged:
        logger.warning("周期扫尾把 %s 条僵死操作收敛为失败：%s", len(converged), converged)
    return {"converged": len(converged), "operation_ids": converged}


@app.task(name="knowledge.sweep_pending")
def sweep_pending(limit: int = 20, retry_failed: bool = True) -> dict:
    """周期任务：扫掉"待处理/失败"的资料（复用 ``manage.py kbprocess``）。

    为什么用它当 beat 的真实调度：项目里本来就有一个成型的扫尾命令
    （缺 worker 时靠它在进程内推进），把它挂到 beat 上既真实又不必新写逻辑。
    它**不会**碰业务队列，也不会消费/清空任何旧消息。

    除了资料扫尾，它还负责**收尾僵死的受控操作**（见
    ``converge_stale_executing_operations``）—— 这是"执行中"唯一会自动退出的路径。
    """
    import io as _io

    from django.core.management import call_command

    # 先收敛僵死操作：它不依赖 kbprocess，且必须在资料扫尾之前独立完成
    try:
        stale_recovery = converge_stale_executing_operations()
    except Exception as exc:  # noqa: BLE001 - 收尾失败不该让资料扫尾也不跑
        logger.warning("僵死操作收尾失败：%s", exc)
        stale_recovery = {"converged": 0, "operation_ids": [], "error": str(exc)}

    buffer = _io.StringIO()
    options: dict = {"limit": limit, "stdout": buffer}
    if retry_failed:
        options["retry_failed"] = True
    try:
        call_command("kbprocess", **options)
    except SystemExit as exc:
        # **必须单独接 SystemExit**：BaseCommand 出错时会 sys.exit()，
        # 它继承自 BaseException，`except Exception` **抓不到** ——
        # 那会直接把 worker 子进程杀掉（审查指出）。这里转成结构化结果。
        code = getattr(exc, "code", 1)
        logger.warning("周期扫尾被 SystemExit 中断（code=%s）", code)
        return {
            "ok": False,
            "reason": "system_exit",
            "code": code,
            "stale_operations": stale_recovery,
        }
    except Exception as exc:  # noqa: BLE001 - 周期任务失败不该把 beat 打挂
        logger.warning("周期扫尾失败：%s", exc)
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "stale_operations": stale_recovery,
        }
    return {
        "ok": True,
        "output": buffer.getvalue()[-2000:],
        "stale_operations": stale_recovery,
    }


def dispatch_processing(file_id: int, *, queue: str = "", payload_extra: dict | None = None) -> dict:
    """把处理任务派发出去。返回"派发结果"，**不代表处理结果**。

    返回值里有 ``dispatched``：False 表示只能等人工/后续重试触发，
    调用方必须据此把资料标成"待处理"，不能当成已完成。

    queue 模式下**禁止 eager**：eager 会在上传请求里同步跑完整流水线，
    那就不是队列了（审查意见 7）。这里显式挡掉，避免"以为排了队其实没有"。
    """
    mode = str(getattr(settings, "KB_UPLOAD_PROCESS_MODE", "auto")).lower()
    eager = bool(getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False))
    # **禁止派发**（负载测试实例用）：直接不派发。
    #
    # 为什么要在"派发"这一层挡：压测会产生大量上传请求，任务一旦真进队列就会
    # 流进 broker（业务用 Redis DB0），既干扰业务队列，也让"压的到底是 HTTP
    # 还是 worker"说不清。压测实例设 KB_DISPATCH_DISABLED=1，这里 fail-closed。
    if bool(getattr(settings, "KB_DISPATCH_DISABLED", False)):
        return {
            "dispatched": False,
            "mode": "dispatch_disabled",
            "task_id": "",
            "eager": eager,
            "reason": "dispatch_disabled",
        }
    if mode == "inline" or eager:
        return {
            "dispatched": False,
            "mode": "inline" if mode == "inline" else mode,
            "task_id": "",
            "eager": eager,
            "reason": "inline" if mode == "inline" else "eager_configured",
        }

    # 消息里带上"这份资料当时是什么"的指纹，消费前核对，避免误处理别的库/别的内容
    options: dict = {"args": [file_id]}
    if queue:
        options["queue"] = queue
    bound = _payload_binding(file_id)
    if bound:
        options["kwargs"] = bound
    if payload_extra:
        options.setdefault("kwargs", {}).update(payload_extra)
    try:
        result = process_ingested_file.apply_async(**options)
    except Exception as exc:  # noqa: BLE001 - broker 不可用不该让上传失败
        logger.warning("任务派发失败（broker 不可用？）file=%s：%s", file_id, exc)
        return {"dispatched": False, "mode": mode, "task_id": "", "error": str(exc)}
    return {
        "dispatched": True,
        "mode": mode,
        "task_id": getattr(result, "id", "") or "",
        "eager": False,
        "queue": queue,
        "binding": bound,
    }


def _payload_binding(file_id: int) -> dict:
    """取"这份资料当时的指纹"：内容哈希 + 库 code。"""
    from apps.knowledge.models import IngestedFile, KnowledgeLibrary

    row = (
        IngestedFile._base_manager.filter(pk=file_id)
        .values("content_sha256", "library_id")
        .first()
    )
    if not row:
        return {}
    code = (
        KnowledgeLibrary.objects.filter(pk=row["library_id"])
        .values_list("code", flat=True)
        .first()
        or ""
    )
    return {"content_sha256": row["content_sha256"] or "", "library_code": code}


@app.task(name="knowledge.patrol")
def patrol(limit: int = 200, source: str = "beat") -> dict:
    """周期巡检：**真实检查**资料与索引的一致性，返回可复盘的问题清单。

    为什么需要它：队列只保证"投递过的会被处理"，但**处理不完/写一半/原件丢失**
    这些状态不会自己暴露。巡检把这些状态显式列出来，作为运维依据。

    检查项（全部基于数据库与原件，不做任何推测）：
      1. `failed` 且已达重试上限 → 需人工介入；
      2. `pending/running` 超过 30 分钟未推进 → 疑似停滞（进程被杀/租约未释放）；
         RUNNING 用 `parse_lease_started_at`（本次真正开始处理的时刻）判"未推进"，
         PENDING 用 `created_at`（从未开始 = 等待时间）；且租约必须为空或已过期；
      3. `chunk_count > 0` 但 `embedded_chunk_count < chunk_count` → 向量不完整；
      4. `raw_path` 非空但原件缺失或哈希不符 → 原件损坏/丢失。

    **不修改任何资料**（只读巡检），因此可以安全地周期性执行。

    每次结论还会**留痕**到 ``knowledge.PatrolRun``（追加写）：这是界面上
    「解析看门狗 / 结果校验」两张状态卡的判定来源。留痕失败不影响巡检结论。
    """
    from datetime import timedelta

    from django.utils import timezone

    from apps.knowledge.integrity import STALE_AFTER_MINUTES
    from apps.knowledge.models import IngestedFile, ParseStatus
    from apps.knowledge.storage import verify_blob

    started = time.monotonic()
    now = timezone.now()
    # 阈值只有一个来源：apps/knowledge/integrity.py 的 STALE_AFTER_MINUTES
    # （此前这里硬编码 30，与留痕/界面口径分开维护，存在漂移风险）。
    stale_before = now - timedelta(minutes=STALE_AFTER_MINUTES)
    base = IngestedFile._base_manager.select_related("library")
    findings: dict[str, list] = {
        "failed_exhausted": [],
        "stalled": [],
        "vector_incomplete": [],
        "blob_missing": [],
    }

    for row in base.all()[:limit]:
        if (
            row.parse_status == ParseStatus.FAILED
            and row.parse_attempts >= row.MAX_PARSE_ATTEMPTS
        ):
            findings["failed_exhausted"].append(
                {"file_id": row.pk, "attempts": row.parse_attempts,
                 "reason": (row.parse_error or "")[:200]}
            )
        if row.parse_status in (ParseStatus.PENDING, ParseStatus.RUNNING):
            lease = getattr(row, "parse_lease_expires_at", None)
            # **"开始推进"的参照点必须按状态取**：
            #   * RUNNING → 本次处理真正开始的时刻 `parse_lease_started_at`
            #     （找不到就退回 created_at，只作为历史数据的兜底）；
            #   * PENDING → 从未开始，等待时间就是 `created_at`。
            # 这里此前写成不存在的 `parse_started_at`，于是**永远**回落到
            # created_at：一份创建于 2 小时前、5 分钟前才真正开始重跑的资料
            # 会被误报"停滞"（判据用了创建时间，不是推进时间）。
            if row.parse_status == ParseStatus.RUNNING:
                started = getattr(row, "parse_lease_started_at", None) or row.created_at
            else:
                started = row.created_at
            overdue = (started is not None and started < stale_before) and (
                lease is None or lease < now
            )
            if overdue:
                findings["stalled"].append(
                    {"file_id": row.pk, "status": row.parse_status,
                     "since": started.isoformat() if started else ""}
                )
        if row.chunk_count and row.embedded_chunk_count < row.chunk_count:
            findings["vector_incomplete"].append(
                {"file_id": row.pk, "chunks": row.chunk_count,
                 "embedded": row.embedded_chunk_count}
            )
        if row.raw_path and row.content_sha256 and not verify_blob(row.content_sha256):
            findings["blob_missing"].append(
                {"file_id": row.pk, "sha256": row.content_sha256[:12]}
            )

    summary = {name: len(items) for name, items in findings.items()}
    total = sum(summary.values())
    if total:
        logger.warning("巡检发现 %s 项待处理：%s", total, summary)
    else:
        logger.info("巡检通过：未发现异常")
    result = {"ok": True, "checked": min(limit, base.count()), "counts": summary,
              "findings": findings}
    # **留痕**：让「解析看门狗 / 结果校验」有可复盘的判定来源。
    # 留痕失败不影响巡检结论（巡检只读，结论照样返回）。
    try:
        from apps.knowledge.integrity import record_patrol_run

        record_patrol_run(
            result, source=source, duration_ms=int((time.monotonic() - started) * 1000)
        )
    except Exception:  # noqa: BLE001 - 留痕失败绝不能影响巡检
        logger.exception("巡检留痕失败（巡检结论仍然有效）")
    return result


__all__ = [
    "app",
    "sweep_pending",
    "patrol",
    "dispatch_processing",
    "process_ingested_file",
]
