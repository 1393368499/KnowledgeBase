"""真实运行指标的**唯一实现**（REST 端点与 AI 意图共用，避免两份逻辑漂移）。

原则（目标 ③）：
  * **只报能真正测到的值** —— 数字全部来自真实数据源；
  * **口径诚实**：熔断器（``breakers``）的状态在 Redis 里**跨进程共享**
    （见 ``apps/knowledge/breaker_store.py``：web 与 worker 共用一份），因此
    共享可用时标 ``scope=global`` + ``backend=redis``；Redis 不可用时**如实**
    降为 ``scope=process`` + ``redis_available=false`` + 原因，绝不把
    "每个进程各算一份"说成全局。模型并发闸门（``model_concurrency``）同样跨进程，
    但口径字段是它自己的 ``scope=redis``，两者不混为一谈；
  * **未实现的如实列出**（当前没有未实现项 → 空列表），绝不填假数字冒充满分；
  * 业务队列（broker DB0/DB1）**拒绝探测**（那是共享生产队列，只读也不碰）。

「解析看门狗 / 结果校验 / 双阶段补扫」三张状态卡的判定来源见
``apps/knowledge/integrity.py``：前两者来自巡检留痕（``PatrolRun``），
后者是实时查询的候选数量。
"""

from __future__ import annotations

import os
import re
from typing import Any

from django.conf import settings
from django.db.models import Count


def collect() -> dict[str, Any]:
    """采集一次真实指标。**没有任何占位常量。**"""
    from .models import DocumentChunk, IngestedFile, KnowledgeLibrary

    payload: dict[str, Any] = {
        "libraries": KnowledgeLibrary.objects.count(),
        "files": IngestedFile._base_manager.count(),
        "chunks": DocumentChunk.objects.count(),
        "files_by_status": {
            row["parse_status"]: row["n"]
            for row in IngestedFile._base_manager.values("parse_status").annotate(
                n=Count("id")
            )
        },
    }

    # 熔断器：真实快照 + **如实口径**。
    # 共享存储（Redis）可用 → scope=global/backend=redis，`shared` 是跨进程全量状态；
    # 不可用 → scope=process/redis_available=false + redis_error（原因），
    # `items` 只是本进程注册表视图。**两手都不许说反**。
    from .resilience import breaker_state_snapshot

    payload["breakers"] = breaker_state_snapshot()

    # 模型并发闸门：也是**跨进程**的（额度在 Redis 里共享），但它报的是自己的口径
    # （`scope=redis`），与熔断器的 `scope=global/process` 是两套字段，不得混为一谈。
    from .ratelimit import limiter_snapshot

    payload["model_concurrency"] = limiter_snapshot()

    # 队列深度：只读；业务库一律不探测
    broker = str(getattr(settings, "CELERY_BROKER_URL", "") or "")
    # **必须用真实队列名**：本系统用的是专用队列（如 `kb_worker_v2`），
    # 写死 `llen("celery")` 会永远读到 0 —— 积压被静默隐藏，是最坏的假绿灯。
    queue_name = str(
        os.environ.get("KB_CELERY_QUEUE", "")
        or getattr(settings, "CELERY_TASK_DEFAULT_QUEUE", "")
        or "celery"
    )
    queue: dict[str, Any] = {"url_db": "", "depth": None, "note": "", "queue_name": queue_name}
    match = re.search(r"/(\d+)\s*$", broker)
    db_index = match.group(1) if match else ""
    queue["url_db"] = db_index
    if db_index in ("", "0", "1"):
        queue["note"] = (
            "broker 指向业务库或未配置 → 拒绝探测队列深度（不影响共享队列）"
        )
    else:
        try:
            import redis as _redis

            client = _redis.Redis.from_url(broker, socket_timeout=2)
            queue["depth"] = int(client.llen(queue_name) or 0)
        except Exception as exc:  # noqa: BLE001 - 探测失败就报失败
            queue["note"] = f"探测失败：{type(exc).__name__}: {exc}"
    payload["broker_queue"] = queue

    # 真实判定来源：看门狗 / 结果校验 / 双阶段补扫 / 两项指标
    # **只报能测到的值**：没有留痕就返回 available=False（界面显示"暂无状态"），
    # 绝不用别的能力顶替，也不编造时间戳。
    from .integrity import (
        feedback_pending_count,
        patrol_snapshot,
        resweep_candidates,
        stalled_running_count,
        verification_snapshot,
    )

    payload["watchdog"] = patrol_snapshot()
    payload["verification"] = verification_snapshot()
    payload["resweep"] = resweep_candidates()
    payload["stalled_tasks"] = stalled_running_count()
    payload["feedback_pending"] = feedback_pending_count()

    # 未实现的能力**必须如实列出**：现在看门狗/结果校验/双阶段补扫都有真实判定来源
    #（巡检留痕 + 实时查询），因此这里是**空列表** —— 如实为空，不留过期的"未实现"声明。
    payload["unavailable"] = []
    # 巡检（patrol）**已实现**：作为 beat 周期任务真实运行，并把每次结论留痕。
    payload["patrol"] = {
        "implemented": True,
        "task": "knowledge.patrol",
        "interval_seconds": int(os.environ.get("KB_CELERY_BEAT_PATROL_SECONDS", "900")),
        "checks": [
            "failed_exhausted",
            "stalled",
            "vector_incomplete",
            "blob_missing",
        ],
        "recording": "每次结论追加留痕到 knowledge.PatrolRun（source=beat/manual）",
        "note": "巡检已作为 beat 周期任务真实运行（只读）；留痕是「解析看门狗 / 结果校验」两张卡的判定来源",
    }
    return payload
