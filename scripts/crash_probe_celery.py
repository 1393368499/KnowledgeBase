"""崩溃恢复实测的 Celery 侧工具（**只在独立队列上操作**）。

为什么要它：`docs/delivery-status-2026-10-15.md` §5.2 里「worker 崩溃恢复未实测」
是本项目**唯一**没有真实证据的可靠性缺口 —— 之前只能说"配置了 acks_late"，
从没杀过 worker 看消息到底会不会回来。

安全边界（很重要）：
  * 只用 `KB_CRASH_QUEUE`（默认 `kb_crash_probe`）这个**专用队列**，
    主队列 `kb_worker_v2` 的消息一条都不碰；
  * 只读 Redis DB0 的存在性，**绝不消费/清空**；
  * 任务用 `knowledge.patrol`（只读巡检 + 写一行巡检留痕），
    不写业务数据、不发外呼。

用法（在容器里跑，宿主编排见 `scripts/crash_recovery_probe.ps1`）：
  python - enqueue 40          # 投递 40 条到专用队列，打印 ids + 队列/未确认状态
  python - status id1 id2 ...   # 报告这些任务 id 的状态分布
  python - unacked              # 报告 Redis 未确认集合（含"何时可见"的时间戳）
  python - rows                 # 报告 source=crash-probe 的巡检留痕行数
"""

from __future__ import annotations

import json
import os
import sys

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

import redis  # noqa: E402

from celery.result import AsyncResult  # noqa: E402
from django.conf import settings  # noqa: E402

from apps.knowledge.integrity import latest_patrol_run  # noqa: E402
from config.celery import patrol  # noqa: E402

QUEUE = os.environ.get("KB_CRASH_QUEUE", "kb_crash_probe")
SOURCE = "crash-probe"


def _redis() -> redis.Redis:
    return redis.Redis.from_url(settings.CELERY_BROKER_URL, socket_timeout=5)


def _queue_state() -> dict:
    """Redis 未确认消息的真实状态（语义按 kombu 源码，不猜）。

    kombu `transport/redis.py`：
      * L369 `zadd(unacked_index_key, {delivery_tag: time()})` —— **score 是"变为未确认的时刻"**，
        不是"何时可见"；
      * L415/419 `ceil = time() - visibility_timeout`，再 `zrevrangebyscore(index, ceil, 0)`
        —— 只有 age > visibility_timeout 的消息才会被重新投递；
      * L644 默认 `visibility_timeout = 3600`（1 小时）。
    所以硬杀 worker 后，在飞消息的恢复延迟≈visibility_timeout。

    口径注意：**真正起作用的是「消费这条消息的那个 worker」的 visibility_timeout**，
    不是本工具所在进程的。实测时会用 `KB_CELERY_VISIBILITY_TIMEOUT=30` 启动探针
    worker，所以这里同时报两个值，避免把"web 容器看到的默认值"当成"worker 实际用的值"
    （第一版就混淆过，导致证据里的数字与恢复时间对不上）：
      * `visibility_timeout_effective` —— 探针 worker 实际用的值（`KB_CRASH_VISIBILITY_TIMEOUT`
        环境变量，由 crash_recovery_probe.ps1 与探针 worker 用同一个变量名传入）；
      * `visibility_timeout_default` —— 本进程 settings 里的生产默认值（= 任务硬超时 + 300）。
    """
    import time as _time

    client = _redis()
    now = _time.time()
    default_timeout = float(
        getattr(settings, "CELERY_BROKER_TRANSPORT_OPTIONS", {}).get("visibility_timeout") or 3600
    )
    timeout = float(os.environ.get("KB_CRASH_VISIBILITY_TIMEOUT") or default_timeout)
    index = client.zrange("unacked_index", 0, -1, withscores=True)
    return {
        "queue_len": int(client.llen(QUEUE)),
        "visibility_timeout_effective": timeout,
        "visibility_timeout_default": default_timeout,
        "unacked_index": [
            {
                "id": str(key, "utf-8"),
                "unacked_since": float(score),
                "age_seconds": round(now - float(score), 1),
                # 还要等这么久才会被重新投递
                "eligible_in_seconds": round(float(score) + timeout - now, 1),
            }
            for key, score in index
        ],
        "unacked_count": len(client.hgetall("unacked")),
    }


def cmd_enqueue(count: int) -> dict:
    ids = []
    for _ in range(count):
        result = patrol.apply_async(kwargs={"limit": 1, "source": SOURCE}, queue=QUEUE)
        ids.append(result.id)
    return {"queue": QUEUE, "enqueued": len(ids), "ids": ids, **_queue_state()}


def cmd_status(ids: list[str]) -> dict:
    states: dict[str, int] = {}
    per_id = {}
    for task_id in ids:
        state = AsyncResult(task_id).state
        per_id[task_id] = state
        states[state] = states.get(state, 0) + 1
    return {"states": states, "per_id": per_id, **_queue_state()}


def cmd_unacked() -> dict:
    return _queue_state()


def cmd_rows() -> dict:
    rows = latest_patrol_run()
    return {"latest_source": rows.get("source") if rows else None, "latest_at": rows.get("created_at") if rows else None}


def main() -> int:
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        return 2
    command, rest = argv[0], argv[1:]
    if command == "enqueue":
        payload = cmd_enqueue(int(rest[0]) if rest else 20)
    elif command == "status":
        payload = cmd_status(rest)
    elif command == "unacked":
        payload = cmd_unacked()
    elif command == "rows":
        payload = cmd_rows()
    else:
        print(f"未知子命令：{command}")
        return 2
    print("CRASH_PROBE " + json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
