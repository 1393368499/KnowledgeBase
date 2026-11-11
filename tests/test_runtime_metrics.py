"""真实运行指标（目标 ③）——只报**能真正测到**的值，未实现的如实列出。

锁死三条：
  * 数字来自真实数据源（查库/真实快照），不是占位常量；
  * 熔断器**明确标注进程内口径**（多 worker 下不是全局聚合）；
  * **未实现的**能力必须出现在 `unavailable` 里（当前已无未实现项 → 空列表）；
    看门狗/结果校验的读数**只能来自真的跑过巡检**，没跑过就如实
    `available=False`，绝不填假数字；业务队列（DB0/DB1）**拒绝探测**。
"""

from __future__ import annotations


from apps.knowledge.models import IngestedFile, KnowledgeLibrary
from apps.planner.operations import get


def _armed(admin_user, params):
    from django.utils import timezone

    from apps.planner.models import Conversation, OperationRecord
    from apps.planner.operations import get as get_spec
    from apps.planner.services import _describe_target

    ref, label = _describe_target(get_spec("runtime_metrics"), params)
    conversation = Conversation.objects.create(user=admin_user)
    return OperationRecord.objects.create(
        user=admin_user,
        conversation=conversation,
        intent="runtime_metrics",
        params=params,
        target_ref=ref,
        target_label=label,
        status=OperationRecord.Status.ARMED,
        armed_at=timezone.now(),
        armed_expires_at=timezone.now() + timezone.timedelta(minutes=10),
    )


def _fetch(admin_user, params=None):
    from apps.planner import services

    operation = _armed(admin_user, params or {})
    return services.execute_operation(admin_user, operation)["result"]


def test_intent_is_registered_readonly():
    assert get("runtime_metrics") is not None, "意图未登记 → AI 无法提案"


def test_counts_come_from_real_data(db, admin_user, make_library, make_file, grant_member):
    library = make_library(code="metrics", name="指标库")
    grant_member(library, admin_user)
    make_file(library=library, name="m1.txt")
    make_file(library=library, name="m2.txt")

    payload = _fetch(admin_user)

    assert payload["libraries"] == KnowledgeLibrary.objects.count()
    assert payload["files"] == IngestedFile._base_manager.count() == 2
    assert sum(payload["files_by_status"].values()) == 2


def test_breakers_are_labelled_honestly(db, admin_user, monkeypatch):
    """熔断器口径必须**如实**：

    * 显式用进程后端（离线回归的默认口径）→ ``scope=process`` + ``redis_available=false``；
    * 共享存储（Redis）可用时 → ``scope=global`` + ``backend=redis`` + ``redis_available=true``。

    独立审查要求"普通离线回归明确用进程后端"，所以这里**强制**进程后端断言第一段，
    绝不因为"本机碰巧有 Redis"而让测试结果随环境漂移。
    """
    monkeypatch.setenv("KB_BREAKER_STORE", "process")
    from apps.knowledge import breaker_store

    breaker_store.reset_breaker_store()
    try:
        payload = _fetch(admin_user)
        assert payload["breakers"]["scope"] == "process", payload["breakers"]
        assert payload["breakers"]["redis_available"] is False, payload["breakers"]
        assert isinstance(payload["breakers"]["items"], list)
    finally:
        breaker_store.reset_breaker_store()

    # Redis 可用时必须是全局口径（不可用就如实降级，不伪报）
    breaker_store.reset_breaker_store()
    snapshot = breaker_store.breaker_store_snapshot()
    if snapshot.get("redis_available"):
        assert snapshot["scope"] == "global", snapshot
        assert snapshot["backend"] == "redis", snapshot
    else:
        assert snapshot["scope"] == "process", snapshot


def test_unimplemented_capabilities_are_listed_not_faked(db, admin_user):
    """未实现的能力必须出现在 `unavailable`；看门狗/结果校验现在有**真实判定来源**。

    这一条以前断言"看门狗未实现" —— 它现在已经实现了：巡检每次运行都留痕，
    所以 `unavailable` 必须是**空**（没有未实现项）；而看门狗/校验的读数
    只能在**真跑过巡检**之后出现，没跑过就如实 `available=False` + 说明原因。
    """
    payload = _fetch(admin_user)

    assert payload["unavailable"] == [], payload["unavailable"]
    assert payload["patrol"]["implemented"] is True
    assert payload["patrol"]["task"] == "knowledge.patrol"
    # 还没跑过巡检 → 两张卡没有读数（灰点），但能力本身是"已实现"
    assert payload["watchdog"]["available"] is False, payload["watchdog"]
    assert payload["watchdog"]["implemented"] is True
    assert payload["watchdog"]["note"], "没读数也必须说明为什么"
    assert payload["verification"]["available"] is False, payload["verification"]
    assert payload["verification"]["note"], "没读数也必须说明为什么"
    # 双阶段补扫是**实时查询**，任何时刻都该给得出候选数（0 也是真数）
    assert isinstance(payload["resweep"]["total"], int), payload["resweep"]
    assert isinstance(payload["resweep"]["phase1"], dict)
    assert isinstance(payload["resweep"]["phase2"], dict)
    assert isinstance(payload["stalled_tasks"], int)
    assert isinstance(payload["feedback_pending"], int)
    # 不得出现"看起来很满"的伪造字段
    for fake in ("uptime", "cpu", "memory", "qps", "watchdog_status", "patrol_status"):
        assert fake not in payload, f"不得伪造指标：{fake}"


def test_business_broker_is_never_probed(db, admin_user, settings):
    """业务队列（DB0/DB1）**拒绝探测**，且必须说明理由。"""
    settings.CELERY_BROKER_URL = "redis://127.0.0.1:56379/0"
    payload = _fetch(admin_user)

    queue = payload["broker_queue"]
    assert queue["depth"] is None, "业务库不得被探测"
    assert queue["note"], "必须说明为什么没探测"
    assert "业务库" in queue["note"]


URL = "/api/v1/knowledge/runtime-metrics/"


def test_rest_endpoint_requires_system_admin(db, auth_api, admin_user, user):
    """REST 端点：管理员可见真实指标；普通用户 403（指标含队列深度与熔断状态）。"""
    admin_client = auth_api(admin_user)
    ok = admin_client.get(URL)
    assert ok.status_code == 200, ok.content
    body = ok.json()
    assert "libraries" in body and "unavailable" in body and "broker_queue" in body
    unavail = {item["name"] for item in body["unavailable"]}
    assert unavail == set(), unavail
    assert body["watchdog"]["implemented"] is True
    assert body["patrol"]["implemented"] is True

    plain_client = auth_api(user)
    denied = plain_client.get(URL)
    assert denied.status_code == 403, denied.content

def test_broker_queue_uses_real_queue_name_and_reports_backlog(db, admin_user, monkeypatch, settings):
    """**核心**：必须按**真实队列名**读积压，否则积压永远显示 0（假绿灯）。

    旧实现写死 `llen("celery")`，而本系统用的是专用队列 `kb_worker_v2` ——
    队列里堆着任务也会报 0。这里用一个假 Redis 客户端记录被查询的 key，
    并断言：查询的 key == 配置的队列名，且返回的 depth 就是该队列的真实长度。
    """
    import sys as _sys
    import types as _types

    from apps.knowledge import metrics as metrics_mod

    settings.CELERY_BROKER_URL = "redis://127.0.0.1:56379/14"
    monkeypatch.setenv("KB_CELERY_QUEUE", "kb_worker_v2")

    seen_keys = []

    class FakeRedis:
        def llen(self, key):
            seen_keys.append(key)
            # 只有真实队列名才有积压；写死的 "celery" 会读到 0
            return 7 if key == "kb_worker_v2" else 0

    fake_module = _types.SimpleNamespace(
        Redis=_types.SimpleNamespace(from_url=lambda url, socket_timeout=None: FakeRedis())
    )
    monkeypatch.setitem(_sys.modules, "redis", fake_module)

    payload = metrics_mod.collect()
    queue = payload["broker_queue"]

    assert queue["queue_name"] == "kb_worker_v2", queue
    assert seen_keys == ["kb_worker_v2"], f"查询了错误的队列名：{seen_keys}"
    assert queue["depth"] == 7, queue