"""熔断状态的**跨进程一致性**与"重置熔断器"的如实口径（用户 2026-10-16 第 7 项）。

三条必须锁死的事
----------------
1. **状态真的共享**：两个**独立的** ``CircuitBreaker`` 实例（等价于两个进程）经
   共享存储互相看得见 —— Redis 可用时用真 Redis 验，默认时用注入的 stub 也验，
   所以这一条**不依赖**外部基础设施（``test_two_breakers_share_state_through_stub_store``）；
2. **真实跨进程**：用 ``subprocess`` 起独立 Python 进程，A 打开熔断、B 必须被挡、
   再由另一个进程 ``reset_all()`` 之后 B 必须恢复（``test_cross_process_state_and_reset``）。
   这一条只在**显式配置了独占测试 Redis** 时才跑，连不上就 skip 并写明原因 —— 绝不假装测过。
3. **Redis 故障必须如实**：``reset_all()`` 返回 ``redis_available=false`` + 可读原因 +
   ``global_reset=false``，**绝不**在共享存储不可用时说"全局重置完成"
   （``test_reset_all_with_redis_down_never_claims_global``，**无条件可跑**）。

安全边界（用户与独立审查的硬约束）
----------------------------------
* **绝不碰 DB0**（共享生产业务队列，有别人的业务消息）：测试 Redis 必须由环境变量
  显式给出（``KB_BREAKER_TEST_REDIS_URL``，其次 ``KB_TEST_REDIS_URL`` / ``KB_LIMIT_TEST_REDIS_URL``），
  **不写死主机端口**；URL 必须显式带一个**独占** DB 序号，指向 DB0/DB1/DB14 会被
  大声拒绝（``test_test_redis_url_must_be_exclusive_when_configured`` 会失败，不是 skip）；
* 所有键都在**测试专用前缀** ``kb:test:breaker:<uuid>:`` 下，用例结束（含失败/异常）
  一律按前缀清理自己创建的键；
* **注入出来的故障绝不写进真实的共享熔断状态**：单测里用
  ``CircuitBreaker(store=<stub>)`` 或 ``breaker_for(name, store=False)``（进程内后端），
  共享 Redis 上的真实服务名键不会因为 MockTransport 造的假故障被打开。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from typing import Any
from urllib.parse import urlsplit

import pytest

from apps.knowledge.breaker_store import BreakerStore
from apps.knowledge.resilience import (
    BREAKERS,
    CircuitBreaker,
    ServiceUnavailable,
    breaker_for,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: 测试专用键前缀（**独占**，与生产前缀 kb:breaker: 完全分开）。
PREFIX = f"kb:test:breaker:{uuid.uuid4().hex[:8]}:"
#: 独占测试 Redis：必须显式配置，绝不写死主机/端口。
TEST_REDIS_URL = (
    os.environ.get("KB_BREAKER_TEST_REDIS_URL", "").strip()
    or os.environ.get("KB_TEST_REDIS_URL", "").strip()
    or os.environ.get("KB_LIMIT_TEST_REDIS_URL", "").strip()
)
#: 生产/业务库：测试**绝对**不允许落在这里（DB0 是共享生产队列，DB14 是主 broker）。
FORBIDDEN_TEST_DBS = frozenset({"0", "1", "14"})


# ------------------------------------------------------------ Redis 配置自检 ----


def _url_parts(url: str) -> tuple[str, int, str, str]:
    parts = urlsplit(url)
    db = (parts.path or "").lstrip("/").split("/")[0]
    password = parts.password or ""
    return (parts.hostname or "127.0.0.1", parts.port or 6379, db, password)


def _redis_config_problem() -> str:
    """配置问题说明（空串 = 配置合法）。"""
    if not TEST_REDIS_URL:
        return (
            "未配置独占测试 Redis：请设置 KB_BREAKER_TEST_REDIS_URL"
            "（或 KB_TEST_REDIS_URL）；测试绝不写死主机端口、也绝不碰 DB0"
        )
    host, port, db, _password = _url_parts(TEST_REDIS_URL)
    if not db.isdigit():
        return f"测试 Redis URL 必须显式带 DB 序号：{TEST_REDIS_URL}"
    if db in FORBIDDEN_TEST_DBS:
        return (
            f"测试 Redis 指向 DB{db}（业务/主库，禁止用于测试）：{TEST_REDIS_URL}"
            f" —— 必须用独占 DB（例如 DB15）"
        )
    return ""


def _redis_reachable() -> bool:
    if _redis_config_problem():
        return False
    host, port, _db, _password = _url_parts(TEST_REDIS_URL)
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except OSError:
        return False


_REDIS_PROBLEM = _redis_config_problem() or (
    "" if _redis_reachable() else f"独占测试 Redis 不可达（{TEST_REDIS_URL}）"
)

needs_redis = pytest.mark.skipif(
    bool(_REDIS_PROBLEM),
    reason=_REDIS_PROBLEM
    or "需要独占测试 Redis（KB_BREAKER_TEST_REDIS_URL 指向专用 DB）",
)


def test_test_redis_url_must_be_exclusive_when_configured():
    """配了测试 Redis 就必须是**独占 DB**：指向 DB0/DB1/DB14 直接失败（不是 skip）。"""
    if not TEST_REDIS_URL:
        pytest.skip("未配置测试 Redis（跨进程用例会 skip；此用例只在配置后校验）")
    problem = _redis_config_problem()
    assert not problem, problem


# ------------------------------------------------------------------ 夹具 ----


@pytest.fixture
def redis_store():
    """真 Redis 上的 store（测试专用前缀；结束按前缀清理自己创建的键）。"""
    if _REDIS_PROBLEM:
        pytest.skip(_REDIS_PROBLEM)
    import redis

    client = redis.Redis.from_url(TEST_REDIS_URL, socket_timeout=3)
    store = BreakerStore(
        url=TEST_REDIS_URL,
        prefix=PREFIX,
        enabled=True,
        client=client,
        probe_ttl_seconds=30.0,
        # 故意**不**固定 key_ttl：让 TTL 走自动口径（>=600 且 >=冷却×4），
        # 这样"短冷却不得缩短索引 TTL"这类负例才有意义。
        deny_cache_seconds=0.0,
    )
    try:
        yield store
    finally:
        _cleanup_prefix(store)
        try:
            client.close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响用例结论
            pass


@pytest.fixture
def clean_breakers():
    """保存/恢复进程内注册表（避免用例互相污染）。"""
    saved = dict(BREAKERS)
    BREAKERS.clear()
    try:
        yield BREAKERS
    finally:
        BREAKERS.clear()
        BREAKERS.update(saved)


def _cleanup_prefix(store: BreakerStore) -> None:
    client = getattr(store, "_client", None)
    if client is None:
        return
    try:
        keys = list(client.scan_iter(match=f"{store.prefix}*", count=200))
        if keys:
            client.delete(*keys)
    except Exception as exc:  # noqa: BLE001 - 清理失败必须可见
        raise AssertionError(f"测试键清理失败（前缀 {store.prefix}）：{type(exc).__name__}: {exc}")


def _new_redis_client():
    import redis

    return redis.Redis.from_url(TEST_REDIS_URL, socket_timeout=3)


def _shared_store(**kwargs: Any) -> BreakerStore:
    """再建一个**独立**的 store（各自的连接），证明状态在 Redis 而不在 Python 对象里。"""
    return BreakerStore(
        url=TEST_REDIS_URL,
        prefix=PREFIX,
        enabled=True,
        client=_new_redis_client(),
        deny_cache_seconds=0.0,
        **kwargs,
    )


# --------------------------------------------------- stub store（无需 Redis） ----


class _StubStore:
    """"进程外共享存储"的替身：内存字典 + 锁 + **带租约的探测令牌**。

    两个独立的 :class:`CircuitBreaker` 共用一个 stub 实例，就等价于"web 与 worker
    共用一份 Redis 状态"；这也是 MockTransport 造故障时的正确做法（**不碰真 Redis**）。

    局限（如实说明）：租约用 ``time.monotonic()``，只在本进程内有意义；真实跨进程
    的时间基准是 Redis 服务器时间（见 ``test_cross_process_state_and_reset``）。
    """

    enabled = True
    deny_cache_seconds = 0.0
    is_stub = True
    backend_name = "stub"

    def __init__(self, *, probe_ttl_seconds: float = 30.0) -> None:
        self.probe_ttl_seconds = probe_ttl_seconds
        self._lock = threading.Lock()
        self._states: dict[str, dict[str, Any]] = {}
        self.calls = {"read": 0, "guard": 0, "failure": 0, "success": 0, "reset": 0}

    def _view(
        self, rec: dict[str, Any], threshold: int, cooldown: float, now: float
    ) -> tuple[str, float, bool]:
        failures = int(rec["failures"])
        opened = float(rec["opened_at"])
        thr = int(rec.get("threshold") or threshold)
        cd = float(rec.get("cooldown") or cooldown)
        if failures < thr:
            return "closed", 0.0, False
        elapsed = now - opened
        if opened > 0 and elapsed < cd:
            return "open", max(0.0, cd - elapsed), False
        owner = str(rec.get("probe_owner") or "")
        probing = bool(owner) and (now - float(rec.get("probe_at") or 0.0)) < self.probe_ttl_seconds
        return "half_open", 0.0, probing

    def try_record_failure(
        self,
        service: str,
        *,
        failure_threshold: int = 3,
        cooldown_seconds: float = 60.0,
        reason: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            self.calls["failure"] += 1
            rec = self._states.setdefault(
                service,
                {
                    "failures": 0,
                    "opened_at": 0.0,
                    "last_reason": "",
                    "probe_owner": "",
                    "probe_at": 0.0,
                    "threshold": failure_threshold,
                    "cooldown": cooldown_seconds,
                },
            )
            rec["failures"] += 1
            rec["threshold"] = failure_threshold
            rec["cooldown"] = cooldown_seconds
            rec["last_reason"] = reason[:300]
            if rec["failures"] >= failure_threshold:
                rec["opened_at"] = time.monotonic()
                rec["probe_owner"] = ""
                rec["probe_at"] = 0.0
            return {
                "ok": True,
                "error": "",
                "consecutive_failures": rec["failures"],
                "opened": rec["failures"] >= failure_threshold,
            }

    def try_record_success(self, service: str) -> dict[str, Any]:
        with self._lock:
            self.calls["success"] += 1
            self._states.pop(service, None)
            return {"ok": True, "error": ""}

    def try_reset(self, service: str) -> dict[str, Any]:
        with self._lock:
            self.calls["reset"] += 1
            existed = self._states.pop(service, None) is not None
            return {"ok": True, "error": "", "existed": existed}

    def try_read(
        self,
        service: str,
        *,
        failure_threshold: int = 3,
        cooldown_seconds: float = 60.0,
    ) -> dict[str, Any]:
        with self._lock:
            self.calls["read"] += 1
            rec = self._states.get(service)
            if rec is None:
                return {"ok": True, "record": None, "error": ""}
            state, retry, probing = self._view(
                rec, failure_threshold, cooldown_seconds, time.monotonic()
            )
            return {
                "ok": True,
                "error": "",
                "record": {
                    "service": service,
                    "state": state,
                    "consecutive_failures": int(rec["failures"]),
                    "failure_threshold": int(rec.get("threshold") or failure_threshold),
                    "cooldown_seconds": float(rec.get("cooldown") or cooldown_seconds),
                    "retry_after_seconds": round(retry, 1),
                    "last_reason": str(rec.get("last_reason") or ""),
                    "probe_in_flight": probing,
                    "source": "stub",
                },
            }

    def try_guard(
        self,
        service: str,
        *,
        failure_threshold: int = 3,
        cooldown_seconds: float = 60.0,
    ) -> dict[str, Any]:
        with self._lock:
            self.calls["guard"] += 1
            rec = self._states.get(service)
            if rec is None:
                return {
                    "ok": True,
                    "error": "",
                    "allowed": True,
                    "state": "closed",
                    "consecutive_failures": 0,
                    "retry_after_seconds": 0.0,
                    "probe_granted": False,
                    "last_reason": "",
                }
            now = time.monotonic()
            state, retry, probing = self._view(rec, failure_threshold, cooldown_seconds, now)
            base = {
                "ok": True,
                "error": "",
                "consecutive_failures": int(rec["failures"]),
                "retry_after_seconds": round(retry, 1),
                "probe_granted": False,
                "last_reason": str(rec.get("last_reason") or ""),
            }
            if state == "open":
                return {**base, "allowed": False, "state": "open"}
            if state == "half_open":
                if probing:
                    return {**base, "allowed": False, "state": "half_open"}
                rec["probe_owner"] = f"stub-{uuid.uuid4().hex}"
                rec["probe_at"] = now
                return {**base, "allowed": True, "state": "half_open", "probe_granted": True}
            return {**base, "allowed": True, "state": "closed"}


# ------------------------------------------- 单元级：状态真的跨实例共享（stub） ----


def test_two_breakers_share_state_through_stub_store():
    """两个**独立** CircuitBreaker 共用一个 stub store ⇒ 一个打开，另一个立刻看得见。

    这就是"web 打开熔断 → worker 也快速失败"的最小证据，且不需要外部 Redis。
    """
    store = _StubStore()
    web = CircuitBreaker("svc-shared", failure_threshold=1, cooldown_seconds=60, store=store)
    worker = CircuitBreaker("svc-shared", failure_threshold=1, cooldown_seconds=60, store=store)

    worker.record_failure("worker 侧外呼失败")  # 状态写在"共享存储"里
    snap = web.snapshot()
    assert snap["state"] == "open", snap
    assert snap["scope"] == "global" and snap["backend"] == "stub", snap
    assert snap["consecutive_failures"] == 1
    with pytest.raises(ServiceUnavailable):
        web.guard()  # 另一个实例（进程）必须被挡住
    assert store.calls["failure"] == 1 and store.calls["guard"] >= 1

    # 反向：worker 恢复 ⇒ web 也恢复（不是各清各的）
    web.record_success()
    assert worker.snapshot()["state"] == "closed"
    worker.guard()  # 不再抛

    # 反向：web 复位 ⇒ worker 也复位
    worker.record_failure("又失败")
    assert web.snapshot()["state"] == "open"
    worker.reset()
    assert web.snapshot()["state"] == "closed"


def test_half_open_single_probe_and_lease_recovery_stub():
    """半开**只放一个**探测；持有者崩溃（不写回结果）后租约到期，别人能接管。"""
    store = _StubStore(probe_ttl_seconds=0.15)
    first = CircuitBreaker("svc-probe", failure_threshold=1, cooldown_seconds=0, store=store)
    second = CircuitBreaker("svc-probe", failure_threshold=1, cooldown_seconds=0, store=store)

    first.record_failure("先失败一次")  # cooldown=0 ⇒ 立刻半开
    assert first.snapshot()["state"] == "half_open"

    first.guard()  # 第一个拿到探测权
    assert first.snapshot()["probe_in_flight"] is True
    with pytest.raises(ServiceUnavailable) as excinfo:
        second.guard()  # 第二个必须被挡（探测在飞）
    assert "半开" in excinfo.value.reason

    time.sleep(0.25)  # 模拟探测者崩溃：租约到期
    second.guard()  # 探测权必须能被人接管，不能永久卡死
    assert store.calls["guard"] >= 3


def test_breaker_for_store_false_is_process_only(clean_breakers):
    """单测注入假故障时的正确姿势：显式进程内后端，**不写**共享 Redis。"""
    breaker = breaker_for("svc-offline-test", failure_threshold=1, store=False)
    assert breaker.store in (None, False)
    breaker.record_failure("MockTransport 造的假故障")
    snap = breaker.snapshot()
    assert snap["state"] == "open"
    assert snap["scope"] == "process"
    assert snap["redis_available"] is False
    assert snap["shared"] is False


# ------------------------------------------------ 单元级（真 Redis）：全局口径 ----


@needs_redis
def test_reset_all_with_real_redis_is_global(redis_store):
    """真 Redis 上：``reset_all()`` 必须返回 ``scope=global`` 且复查无残留。"""
    breaker = CircuitBreaker("svc-global", failure_threshold=1, cooldown_seconds=120, store=redis_store)
    breaker.record_failure("模拟两个进程都会看到的失败")
    shared = redis_store.global_snapshot()
    assert [item["service"] for item in shared] == ["svc-global"], shared
    assert shared[0]["state"] == "open"

    result = redis_store.reset_all()
    assert result["scope"] == "global", result
    assert result["backend"] == "redis"
    assert result["redis_available"] is True
    assert result["redis_error"] == ""
    assert result["global_reset"] is True, result
    assert result["remaining"] == 0
    # 证据字段：必须基于**真实扫描**，不能只看索引
    assert result["scanned"] == 1, result  # 1 个状态键
    assert result["scanned_keys_total"] == 2, result  # + 索引键
    assert result["deleted"] == 2, result
    assert result["orphans_found"] == 0, result  # 索引里有登记 → 不是孤儿
    assert result["index_present"] is True
    assert [item["service"] for item in result["reset"]] == ["svc-global"], result["reset"]
    assert all(item["reset"] for item in result["reset"])
    assert redis_store.global_snapshot() == []
    # 复位后本进程的断路器也恢复（共享状态已清）
    breaker.guard()


@needs_redis
def test_short_cooldown_does_not_shorten_index_ttl(redis_store):
    """负例①：短冷却服务的写入**不得**缩短索引 TTL（独立审查实测到的缺陷）。

    旧实现里 ``EXPIRE(index, ttl)`` 会被后来的短冷却写入砍短（实测 index 8000→600）：
    索引先过期、长冷却的熔断状态还在 → 只按索引删就会漏删。现在 TTL **只延长**。
    """
    client = redis_store._client
    assert (
        redis_store.try_record_failure(
            "svc-long-ttl", failure_threshold=99, cooldown_seconds=2000.0, reason="长冷却"
        )["ok"]
        is True
    )
    long_ttl = int(client.ttl(redis_store._key("svc-long-ttl")))
    index_ttl_after_long = int(client.ttl(redis_store._index_key()))
    assert long_ttl > 600, long_ttl
    assert index_ttl_after_long >= long_ttl, (index_ttl_after_long, long_ttl)

    redis_store.try_record_failure(
        "svc-short-ttl", failure_threshold=99, cooldown_seconds=60.0, reason="短冷却"
    )
    short_ttl = int(client.ttl(redis_store._key("svc-short-ttl")))
    index_ttl_after_short = int(client.ttl(redis_store._index_key()))
    assert short_ttl < long_ttl, (short_ttl, long_ttl)
    assert index_ttl_after_short >= long_ttl, (
        f"索引 TTL 被短冷却写入缩短了：index={index_ttl_after_short} long_key={long_ttl}"
    )


@needs_redis
def test_reset_all_finds_orphans_when_index_is_lost(redis_store):
    """负例②：索引键丢失/过期后，``reset_all`` 仍必须清掉残余状态键（孤儿发现）。"""
    client = redis_store._client
    for name in ("svc-orphan-a", "svc-orphan-b"):
        redis_store.try_record_failure(name, failure_threshold=1, cooldown_seconds=600.0, reason="打开")
    assert int(client.exists(redis_store._index_key())) == 1

    client.delete(redis_store._index_key())  # 模拟索引过期/被手工删掉
    assert int(client.exists(redis_store._index_key())) == 0

    # 就算索引没了，全局快照也要能发现真实存在的状态键
    assert sorted(item["service"] for item in redis_store.global_snapshot()) == [
        "svc-orphan-a",
        "svc-orphan-b",
    ]

    result = redis_store.reset_all()
    assert result["scope"] == "global", result
    assert result["global_reset"] is True, result
    assert result["scanned"] == 2, result  # 真实扫描到的状态键
    assert result["scanned_keys_total"] == 2, result  # 索引已被删，只剩两个状态键
    assert result["deleted"] == 2, result
    assert result["orphans_found"] == 2, result  # 索引里没有 → 两个都是孤儿
    assert result["orphan_services"] == ["svc-orphan-a", "svc-orphan-b"], result
    assert result["index_present"] is False
    assert result["remaining"] == 0
    assert redis_store.global_snapshot() == []
    assert list(client.scan_iter(match=f"{redis_store.prefix}*")) == []


@needs_redis
def test_second_independent_breaker_sees_shared_state_real_redis(redis_store):
    """两个独立 store/breaker（各自的连接）经**真 Redis**共享同一份状态。"""
    other = _shared_store()
    try:
        web = CircuitBreaker("svc-real", failure_threshold=2, cooldown_seconds=120, store=redis_store)
        worker = CircuitBreaker("svc-real", failure_threshold=2, cooldown_seconds=120, store=other)

        worker.record_failure("第一次")
        assert web.snapshot()["state"] == "closed"  # 阈值 2，还差一次
        worker.record_failure("第二次")
        assert web.snapshot()["state"] == "open"
        assert web.snapshot()["consecutive_failures"] == 2
        with pytest.raises(ServiceUnavailable):
            web.guard()

        web.record_success()  # 一个进程恢复 ⇒ 另一个进程也恢复
        assert worker.snapshot()["state"] == "closed"
    finally:
        _cleanup_prefix(other)


@needs_redis
def test_half_open_probe_lease_expires_real_redis():
    """真 Redis：探测权带**租约**，持有者崩溃后自动过期，别人能接管（不永久卡死）。"""
    store = _shared_store(probe_ttl_seconds=1.0, key_ttl_seconds=120.0)
    second = _shared_store(probe_ttl_seconds=1.0, key_ttl_seconds=120.0)
    try:
        holder = CircuitBreaker("svc-lease", failure_threshold=1, cooldown_seconds=0, store=store)
        other = CircuitBreaker("svc-lease", failure_threshold=1, cooldown_seconds=0, store=second)
        holder.record_failure("半开前的失败")
        assert holder.snapshot()["state"] == "half_open"
        holder.guard()  # 拿到探测权（不写回结果 = 模拟崩溃）
        with pytest.raises(ServiceUnavailable) as excinfo:
            other.guard()
        assert "半开" in excinfo.value.reason

        time.sleep(1.2)  # 租约到期
        other.guard()  # 必须能接管
    finally:
        _cleanup_prefix(store)
        _cleanup_prefix(second)


# ------------------------------------------------------ 真实跨进程（subprocess） ----

_CHILD_SNIPPET = """
import json
import os
import sys

from apps.knowledge.breaker_store import BreakerStore
from apps.knowledge.resilience import CircuitBreaker, ServiceUnavailable

mode, url, prefix = sys.argv[1], sys.argv[2], sys.argv[3]
store = BreakerStore(
    url=url, prefix=prefix, enabled=True, probe_ttl_seconds=30.0,
    key_ttl_seconds=120.0, deny_cache_seconds=0.0,
)
breaker = CircuitBreaker(
    "svc-crossproc", failure_threshold=1, cooldown_seconds=300, store=store
)
out = {"mode": mode, "pid": os.getpid()}
if mode == "open":
    breaker.record_failure("子进程 A 注入的失败")
    snap = breaker.snapshot()
    out["state"] = snap["state"]
    out["scope"] = snap["scope"]
    out["failures"] = snap["consecutive_failures"]
    out["shared_items"] = len(store.global_snapshot())
elif mode == "probe":
    try:
        breaker.guard()
        out["blocked"] = False
    except ServiceUnavailable as exc:
        out["blocked"] = True
        out["reason"] = exc.reason
    snap = breaker.snapshot()
    out["state"] = snap["state"]
    out["scope"] = snap["scope"]
elif mode == "reset":
    result = store.reset_all()
    out["scope"] = result["scope"]
    out["global_reset"] = result["global_reset"]
    out["remaining"] = result["remaining"]
    out["scanned"] = result["scanned"]
    out["deleted"] = result["deleted"]
    out["orphans_found"] = result["orphans_found"]
    out["reset_services"] = [item["service"] for item in result["reset"]]
else:
    raise SystemExit("unknown mode: " + mode)
print("CHILD_RESULT " + json.dumps(out, ensure_ascii=False))
"""


def _run_child(mode: str) -> dict[str, Any]:
    """在**另一个操作系统进程**里操作共享熔断状态（不依赖 Django 启动）。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("DJANGO_SETTINGS_MODULE", None)
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD_SNIPPET, mode, TEST_REDIS_URL, PREFIX],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=90,
        env=env,
    )
    assert proc.returncode == 0, f"子进程失败（mode={mode}）：{proc.stderr[-800:]}"
    for line in proc.stdout.splitlines():
        if line.startswith("CHILD_RESULT "):
            return json.loads(line[len("CHILD_RESULT ") :])
    raise AssertionError(f"子进程没有输出结果（mode={mode}）：{proc.stdout[-500:]}")


@needs_redis
def test_cross_process_state_and_reset(redis_store):
    """**核心真实证据**：两个独立 Python 进程经共享存储互相看到熔断状态与重置。

    进程 A 打开熔断 → 进程 B 被挡住（而不是各自为政）→ 进程 C 全局重置
    → 进程 B 恢复。测试结束按前缀清理（fixture 的 finally 兜底）。
    """
    try:
        opened = _run_child("open")
        assert opened["state"] == "open", opened
        assert opened["scope"] == "global", opened
        assert opened["failures"] == 1, opened
        assert opened["shared_items"] == 1, opened

        blocked = _run_child("probe")
        assert blocked["blocked"] is True, blocked
        assert blocked["state"] == "open", blocked
        assert "熔断" in blocked["reason"], blocked

        # 父进程也看得见（同一个服务名）
        assert redis_store.global_snapshot()[0]["state"] == "open"

        reset = _run_child("reset")
        assert reset["scope"] == "global", reset
        assert reset["global_reset"] is True, reset
        assert reset["remaining"] == 0, reset
        assert reset["reset_services"] == ["svc-crossproc"], reset
        assert reset["scanned"] >= 1, reset
        assert reset["deleted"] >= 1, reset

        recovered = _run_child("probe")
        assert recovered["blocked"] is False, recovered
        assert recovered["state"] == "closed", recovered
    finally:
        _cleanup_prefix(redis_store)


# ------------------------------------------------- Redis 故障：必须如实、不许伪报 ----


def test_reset_all_with_redis_down_never_claims_global():
    """Redis 不可用：``reset_all()`` 如实返回 ``redis_available=false`` + 原因 + **不**声称全局成功。

    这一条**无条件可跑**（不依赖任何外部服务）：用的是必然连不上的地址。
    """
    store = BreakerStore(
        url="redis://127.0.0.1:1/15",  # 必然连不上（端口 1），且是**非 0** 的库
        prefix=f"{PREFIX}down",
        enabled=True,
        socket_timeout=0.3,
        backoff_seconds=0.0,
    )
    result = store.reset_all()
    assert result["scope"] == "process", result
    assert result["backend"] == "process"
    assert result["redis_available"] is False
    assert result["global_reset"] is False, "Redis 不可用时**绝不能**声称全局重置成功"
    assert result["redis_error"], "必须给出可读原因"
    assert "未做任何全局重置" in result["note"], result["note"]
    # 没扫描就没扫描：证据字段必须是"没有证据"，不能填 0 假装查过
    assert result["scanned"] is None, result
    assert result["deleted"] == 0, result
    assert result["orphans_found"] is None, result
    assert store.global_snapshot() == []
    snap = store.snapshot()
    assert snap["scope"] == "process" and snap["redis_available"] is False
    assert snap["redis_error"]


@pytest.mark.parametrize(
    ("failure", "deleted"),
    [("before_capture", 0), ("scan", 1), ("delete", 1), ("final_scan", 2), (None, 2)],
)
def test_reset_all_requires_completed_verification(monkeypatch, failure, deleted):
    store = BreakerStore(
        url="redis://127.0.0.1:1/15",
        prefix=f"{PREFIX}verify",
        enabled=True,
    )

    class Client:
        scan_count = 0
        delete_count = 0

        def scan_iter(self, *, match, count):
            self.scan_count += 1
            if (failure == "scan" and self.scan_count == 2) or (
                failure == "final_scan" and self.scan_count == 4
            ):
                raise ConnectionError("复查连接中断")
            if self.scan_count <= 2:
                yield store._key("verify")

        def smembers(self, key):
            return {"verify"}

        def exists(self, key):
            return True

        def eval(self, script, numkeys, *keys):
            self.delete_count += 1
            if failure == "delete" and self.delete_count == 2:
                raise ConnectionError("补删连接中断")
            return 1

    monkeypatch.setattr(store, "_client", Client())
    if failure == "before_capture":
        connectivity = store.connectivity

        def disconnect_after_check():
            result = connectivity()
            store._note_error(ConnectionError("连接检查后失效"))
            return result

        monkeypatch.setattr(store, "connectivity", disconnect_after_check)

    result = store.reset_all()
    assert result["deleted"] == deleted
    assert result["global_reset"] is (failure is None)
    assert result["redis_available"] is (failure is None)
    assert result["remaining"] == (0 if failure is None else None)
    if failure is not None:
        assert result["scope"] == "process"
        assert "ConnectionError" in result["redis_error"]


def test_redis_down_degrades_to_process_without_breaking_calls():
    """Redis 不可用时熔断逻辑仍按**进程内**工作（不抛 Redis 异常打断检索），口径如实。"""
    store = BreakerStore(
        url="redis://127.0.0.1:1/15",
        prefix=f"{PREFIX}down2",
        enabled=True,
        socket_timeout=0.3,
        backoff_seconds=0.0,
    )
    breaker = CircuitBreaker("svc-down", failure_threshold=1, cooldown_seconds=60, store=store)
    breaker.guard()  # 降级路径：不因为 Redis 挂了而失败
    breaker.record_failure("模型服务失败（Redis 也不可用）")
    snap = breaker.snapshot()
    assert snap["state"] == "open"  # 本地仍然打开（保护调用方）
    assert snap["scope"] == "process"
    assert snap["redis_available"] is False
    assert snap["redis_error"]
    with pytest.raises(ServiceUnavailable):
        breaker.guard()


@pytest.mark.parametrize(
    "url",
    [
        "redis://127.0.0.1:1/0",  # 明明指向 DB0
        "redis://127.0.0.1:1",  # 没写 DB ⇒ redis-py 默认 DB0
        "redis://127.0.0.1:1/notadb",  # DB 序号解析不出来
    ],
)
def test_store_refuses_db0_and_unknown_db(url):
    """**绝不在 DB0 上工作**：指向 DB0 或无法确认 DB 时必须拒绝，且连客户端都不建。"""
    store = BreakerStore(url=url, prefix=PREFIX, enabled=True, socket_timeout=0.3)
    assert store.refusal_reason, "必须给出拒绝原因"
    assert store.connectivity()[0] is False
    assert store._client is None, "拒绝工作时不允许建立连接"
    result = store.reset_all()
    assert result["scope"] == "process"
    assert result["redis_available"] is False
    assert result["global_reset"] is False
    assert result["scanned"] is None, "拒绝工作时不许假装扫描过"
    assert "DB0" in result["redis_error"] or "无法确定" in result["redis_error"], result


def test_reset_all_breakers_honest_when_shared_store_disabled(monkeypatch, clean_breakers):
    """显式"进程内后端"（``KB_BREAKER_STORE=process``）时：reset 只清本进程且**如实**说明。"""
    from apps.knowledge import breaker_store as store_module
    from apps.knowledge import resilience

    monkeypatch.setenv("KB_BREAKER_STORE", "process")
    store_module.reset_breaker_store()
    try:
        breaker = resilience.breaker_for("svc-process-only", failure_threshold=1)
        assert breaker.store in (None, False), "进程内后端不应挂共享存储"
        breaker.record_failure("本进程内失败")

        result = resilience.reset_all_breakers()
        assert result["scope"] == "process", result
        assert result["redis_available"] is False
        assert result["global_reset"] is False
        assert result["redis_error"], "必须说明为什么不是全局"
        assert "未完成全局重置" in result["scope_note"], result["scope_note"]
        assert [item["service"] for item in result["reset"]] == ["svc-process-only"]
        assert breaker.snapshot()["state"] == "closed"  # 本进程确实被复位了
    finally:
        store_module.reset_breaker_store()


def test_reset_all_breakers_merges_shared_and_process(redis_store, clean_breakers, monkeypatch):
    """共享可用时 ``reset_all_breakers()`` 同时清共享与进程内，并逐服务给结果。"""
    from apps.knowledge import breaker_store as store_module
    from apps.knowledge import resilience

    # 让模块级入口用**测试前缀**的 store（生产用的是 kb:breaker: 前缀）
    monkeypatch.setattr(store_module, "_STORE", redis_store)

    shared_breaker = resilience.breaker_for(
        "svc-merged", failure_threshold=1, cooldown_seconds=120
    )
    assert shared_breaker.store is redis_store, "breaker_for 必须挂上共享存储"
    shared_breaker.record_failure("共享侧失败")

    local_only = CircuitBreaker("svc-merged-local", failure_threshold=1, store=False)
    local_only.record_failure("仅进程内失败")
    clean_breakers["svc-merged-local"] = local_only

    result = resilience.reset_all_breakers()
    assert result["scope"] == "global", result
    assert result["redis_available"] is True
    assert result["global_reset"] is True, result
    assert result["reset_count"] == 2, result
    assert set(result["services"]) == {"svc-merged", "svc-merged-local"}
    assert all(item["reset"] for item in result["reset"])
    sources = {item["service"]: item["source"] for item in result["reset"]}
    assert sources["svc-merged"] == "redis", sources
    assert sources["svc-merged-local"] == "process", sources
    assert "全局重置完成" in result["scope_note"], result["scope_note"]
    assert shared_breaker.snapshot()["state"] == "closed"
    assert local_only.snapshot()["state"] == "closed"


# ------------------------------------------------------------ 指标口径自洽 ----


@pytest.mark.django_db
def test_metrics_breaker_block_is_self_consistent():
    """指标里的熔断口径必须自洽：可用 ⇒ global/redis；不可用 ⇒ process + 原因。"""
    from apps.knowledge.metrics import collect

    block = collect()["breakers"]
    assert block["scope"] in ("global", "process"), block
    if block["redis_available"]:
        assert block["scope"] == "global", block
        assert block["backend"] == "redis", block
        assert block["redis_error"] == "", block
        assert isinstance(block["shared"], list)
    else:
        assert block["scope"] == "process", block
        assert block["backend"] == "process", block
        assert block["redis_error"], "不可用时必须给出原因（不许含糊）"
        assert block["shared"] == []
    assert isinstance(block["items"], list)
    assert "note" in block and block["note"]
    # 每一项熔断状态自己也要口径自洽（不能一项 global、一项 process 混着说）
    for item in block["items"]:
        if block["redis_available"]:
            assert item["scope"] == "global" and item["redis_available"] is True
        else:
            assert item["scope"] == "process" and item["redis_available"] is False
