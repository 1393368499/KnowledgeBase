"""跨进程模型外呼闸门的回归（用户 m11581 第 (3) 项里「跨进程限流」这一未完成项）。

为什么这些断言长这样
--------------------
闸门的价值全在**两个失效模式**上，所以用例直接盯它们，而不是只测"能拿到额度"：

1. **额度真的跨进程共享** —— 父进程占满额度后，**另一个操作系统进程**必须拿不到；
   这是进程内熔断器做不到的事（``test_cross_process_visibility``）。
2. **持有者崩溃不会永久占死额度** —— 排序集合的过期清理必须真的自愈
   （``test_expired_holder_self_heals``）；而"正在飞的持有者"不能被误清
   （靠 TTL > 最坏耗时这个不变量，见 ``test_slot_ttl_exceeds_worst_case_call``）。

需要真 Redis 的用例标了 ``needs_redis``：本机 Redis 在 56379（Docker 发布端口），
不可达就 skip —— 不假装测过。用**测试专用 DB15** 与独立前缀，绝不碰业务库 DB0
与主队列键（``test_limiter_never_touches_business_keys``）。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import textwrap
import time
import uuid

import pytest
from django.test import override_settings

from apps.knowledge import ratelimit
from apps.knowledge.ratelimit import SlotLimiter
from apps.knowledge.resilience import ServiceUnavailable

#: 默认用**测试专用 broker DB**（15，与 tests/test_real_worker.py 同一个隔离库）。
TEST_REDIS_URL = os.environ.get("KB_LIMIT_TEST_REDIS_URL", "redis://127.0.0.1:56379/15")
PREFIX = f"kb:test:model:slots:{uuid.uuid4().hex[:8]}"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _redis_reachable(url: str = TEST_REDIS_URL) -> bool:
    host_port = url.split("//", 1)[-1].split("/", 1)[0]
    host, _, port = host_port.partition(":")
    try:
        with socket.create_connection((host or "127.0.0.1", int(port or "6379")), timeout=1.5):
            return True
    except OSError:
        return False


needs_redis = pytest.mark.skipif(
    not _redis_reachable(), reason=f"需要 Redis（{TEST_REDIS_URL}）"
)


@pytest.fixture
def limiter():
    """一个限额 2、TTL 2s、等待 0.2s 的闸门（用完清掉自己那批键）。"""
    import redis

    client = redis.Redis.from_url(TEST_REDIS_URL, socket_timeout=3)
    lim = SlotLimiter(
        url=TEST_REDIS_URL,
        prefix=PREFIX,
        limit=2,
        ttl_seconds=2,
        wait_seconds=0.2,
        poll_seconds=0.02,
        client=client,
    )
    try:
        yield lim
    finally:
        for key in client.keys(f"{PREFIX}:*"):
            client.delete(key)


def _zcard(prefix: str, service: str) -> int:
    import redis

    client = redis.Redis.from_url(TEST_REDIS_URL, socket_timeout=3)
    return int(client.zcard(f"{prefix}:{service}") or 0)


# ---------------------------------------------------------------- 基本语义 ----


@needs_redis
def test_limit_is_enforced_and_release_restores(limiter):
    first = limiter.try_acquire("embedding")
    second = limiter.try_acquire("embedding")
    assert first and second, "前两次必须拿到"
    assert first != second, "每次占用的令牌必须唯一（同令牌会覆盖自己，闸门就没了）"
    assert limiter.try_acquire("embedding") is None, "第 3 个必须被挡在闸门外"
    assert limiter.in_flight("embedding") == 2

    limiter.release("embedding", first)
    assert limiter.in_flight("embedding") == 1
    third = limiter.try_acquire("embedding")
    assert third is not None

    limiter.release("embedding", second)
    limiter.release("embedding", third)
    assert limiter.in_flight("embedding") == 0


@needs_redis
def test_services_have_independent_quotas(limiter):
    first = limiter.try_acquire("embedding")
    second = limiter.try_acquire("embedding")
    assert first and second
    assert limiter.try_acquire("embedding") is None
    # 另一个服务不该被 embedding 占满而受牵连
    rerank = limiter.try_acquire("rerank")
    assert rerank is not None
    limiter.release("rerank", rerank)
    limiter.release("embedding", first)
    limiter.release("embedding", second)


@needs_redis
def test_release_on_empty_quota_does_not_go_negative(limiter):
    limiter.release("ocr", "从未占用过的令牌")  # 从没拿过就释放
    assert _zcard(PREFIX, "ocr") == 0
    token = limiter.try_acquire("ocr")
    assert token is not None
    limiter.release("ocr", token)


@needs_redis
def test_slot_times_out_with_honest_service_unavailable(limiter):
    held = limiter.try_acquire("llm")
    held2 = limiter.try_acquire("llm")  # 额度已满
    assert held and held2
    started = time.monotonic()
    with pytest.raises(ServiceUnavailable) as excinfo:
        with limiter.slot("llm"):
            pytest.fail("额度已满时不该进入上下文")
    waited = time.monotonic() - started
    assert waited >= 0.2, "必须真的等到 deadline，而不是立刻失败或立刻放行"
    assert "跨进程并发闸门已满" in str(excinfo.value)
    assert excinfo.value.service == "llm"
    assert excinfo.value.reason, "必须给出可读原因（界面据此如实显示）"
    limiter.release("llm", held)


@needs_redis
def test_slot_releases_even_when_body_raises(limiter):
    with pytest.raises(RuntimeError):
        with limiter.slot("embedding"):
            raise RuntimeError("外呼本身失败")
    assert limiter.in_flight("embedding") == 0, "失败路径也必须释放额度"


# ------------------------------------------------------------ 失效模式自愈 ----


@needs_redis
def test_expired_holder_self_heals():
    """持有者被 SIGKILL（永远不 release）→ 过期后额度必须自动回来，而不是永久占死。"""
    import redis

    client = redis.Redis.from_url(TEST_REDIS_URL, socket_timeout=3)
    crashed = SlotLimiter(
        url=TEST_REDIS_URL,
        prefix=PREFIX,
        limit=1,
        ttl_seconds=1,
        wait_seconds=0.0,
        client=client,
    )
    survivor = SlotLimiter(
        url=TEST_REDIS_URL,
        prefix=PREFIX,
        limit=1,
        ttl_seconds=1,
        wait_seconds=0.0,
        client=client,
    )
    try:
        assert crashed.try_acquire("ocr") is not None
        assert survivor.try_acquire("ocr") is None, "TTL 内不能自愈（否则会超发）"
        time.sleep(1.2)
        token = survivor.try_acquire("ocr")
        assert token is not None, "过期持有者必须被清理掉"
        survivor.release("ocr", token)
    finally:
        for key in client.keys(f"{PREFIX}:*"):
            client.delete(key)


def test_slot_ttl_exceeds_worst_case_call():
    """不变量：额度 TTL 必须大于单次外呼最坏耗时，否则在飞的持有者会被误判过期。"""
    from django.conf import settings

    assert settings.KB_MODEL_SLOT_TTL_SECONDS > settings.KB_LLM_DOC_TIMEOUT_SECONDS
    assert settings.KB_MODEL_SLOT_TTL_SECONDS > settings.KB_OCR_TIMEOUT_SECONDS


# ------------------------------------------------------- 跨进程（核心证据） ----


@needs_redis
def test_cross_process_visibility():
    """**核心断言**：额度是跨进程共享的 —— 另一个进程必须看得见父进程占用的额度。

    用真正的子进程（``sys.executable -c``），不是线程、不是 mock：进程内熔断器/计数器
    在这里必然失败，而 Redis 排序集合必须让它成立。
    """
    parent = SlotLimiter(
        url=TEST_REDIS_URL, prefix=PREFIX, limit=1, ttl_seconds=30, wait_seconds=0.0
    )
    held = parent.try_acquire("embedding")
    assert held is not None
    try:
        assert _child_acquire() is False, "子进程必须看得见父进程占用的额度"
    finally:
        parent.release("embedding", held)
    assert _child_acquire() is True, "父进程释放后子进程必须能拿到"


def _child_acquire() -> bool:
    """在**另一个操作系统进程**里尝试拿一次额度（不依赖 Django 启动）。"""
    snippet = textwrap.dedent(
        """
        from apps.knowledge.ratelimit import SlotLimiter
        lim = SlotLimiter(url={url!r}, prefix={prefix!r}, limit=1,
                          ttl_seconds=30, wait_seconds=0.0)
        print("CHILD_ACQUIRED", int(lim.try_acquire("embedding") is not None))
        """
    ).format(url=TEST_REDIS_URL, prefix=PREFIX)
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("DJANGO_SETTINGS_MODULE", None)
    proc = subprocess.run(
        [sys.executable, "-c", snippet],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert proc.returncode == 0, f"子进程失败：{proc.stderr[-500:]}"
    assert "CHILD_ACQUIRED" in proc.stdout, proc.stdout
    return proc.stdout.strip().endswith("1")


# ---------------------------------------------------------------- 降级口径 ----


def test_redis_down_fails_open_and_is_counted():
    """Redis 不可用 → 放行（fail-open）**且**降级次数可查，不静默。"""
    before = ratelimit.fallback_counts()["total"]
    dead = SlotLimiter(
        url="redis://127.0.0.1:1/0", prefix=PREFIX, limit=1, wait_seconds=0.05
    )
    assert dead.try_acquire("embedding") is not None, "Redis 挂了不能把外呼一起拖死"
    with dead.slot("embedding"):  # 放行，不抛
        pass
    after = ratelimit.fallback_counts()["total"]
    assert after >= before + 2, "每次降级都要被记下来"
    snapshot = dead.snapshot()
    assert snapshot["available"] is False
    assert snapshot["unavailable_reason"], "要给出为什么降级（可读原因）"
    assert snapshot["scope"] == "redis"


def test_disabled_limiter_never_touches_redis():
    class Boom:
        def __getattr__(self, name):  # pragma: no cover - 只应在被调用时炸
            raise AssertionError(f"闸门关闭时不该碰 Redis（.{name}）")

    off = SlotLimiter(enabled=False, client=Boom(), limit=1)
    assert off.try_acquire("embedding") is not None
    with off.slot("embedding"):
        pass
    snapshot = off.snapshot()
    assert snapshot["enabled"] is False
    assert snapshot["in_flight"]["embedding"] is None, "没测到就报 null，不填 0"


def test_limiter_never_touches_business_keys():
    """键前缀必须与业务队列/业务库彻底分开（DB0 的 celery 列表绝不能被碰）。"""
    from django.conf import settings

    prefix = settings.KB_MODEL_LIMIT_PREFIX
    assert prefix and prefix.startswith("kb:")
    assert "celery" not in prefix and "unacked" not in prefix
    assert ratelimit.DEFAULT_PREFIX == "kb:model:slots"


def test_test_settings_disable_the_shared_slot_limiter():
    """**测试配置默认关闭闸门**：单测绝不读写业务 Redis 的槽位键。

    用户 2026-10-17 指出的隔离缺口：熔断器早就被改成进程内后端（DB14 曾被假故障
    污染），但闸门没有 —— ``build_limiter()`` 会从 ``CELERY_BROKER_URL`` 推出
    **业务 Redis** 地址。于是一条只用了 ``MockTransport`` 的用例，只要路径经过
    ``slots(...)``，就会往共享键空间里写槽位键（闸门是在外呼**之前**取额度的，
    "没真外呼"并不能幸免）。

    这条用例同时守住两件事：默认必须是关的；关的时候连客户端都不许构造。
    """
    from django.conf import settings

    assert settings.KB_MODEL_LIMIT_ENABLED is False, (
        "测试配置必须默认关闭模型并发闸门（需要真限流的用例自己显式启用）"
    )
    assert (settings.KB_MODEL_LIMIT_REDIS_URL or "") == "", (
        "测试配置不得给闸门留下可回落到业务 Redis 的地址"
    )

    class Boom:
        def __getattr__(self, name):  # pragma: no cover - 只应在被调用时炸
            raise AssertionError(f"闸门关闭时不该碰 Redis（.{name}）")

    ratelimit.reset_limiter()
    try:
        lim = ratelimit.build_limiter()
        assert lim.enabled is False
        # 塞一个"一碰就炸"的客户端：关闭状态下任何方法都不该触到它
        lim._client = Boom()
        assert lim.try_acquire("embedding"), "关闭时必须 fail-open 放行"
        with lim.slot("embedding"):
            pass
        assert lim.in_flight("embedding") is None
        snapshot = lim.snapshot()
        assert snapshot["enabled"] is False
        assert snapshot["available"] is False
        assert "KB_MODEL_LIMIT_ENABLED=0" in snapshot["unavailable_reason"]
    finally:
        ratelimit.reset_limiter()


# ------------------------------------------------------------ 挂点（接线） ----


def test_post_json_passes_through_the_gate(monkeypatch):
    """所有外呼都走 call_with_resilience，所以闸门必须在这里生效（不是各客户端各写一套）。"""
    from apps.knowledge import resilience

    seen: list[str] = []

    class Recording:
        def __enter__(self):
            return None

        def __exit__(self, *exc):
            return False

    def fake_slots(service, **_kw):
        seen.append(service)
        return Recording()

    monkeypatch.setattr(ratelimit, "slots", fake_slots)
    http = _FakeHttp()
    with override_settings(KB_MODEL_CALLS_DISABLED=False):
        resilience.post_json(
            http=http, url="http://example.invalid/v1", payload={}, service="embedding"
        )
    assert seen == ["embedding"]
    assert http.calls == 1


def test_full_gate_blocks_post_json_without_calling_http(monkeypatch):
    """闸门等不到额度 → 抛"服务不可用"，**且不发出请求**（不能把压力漏到模型服务）。"""
    from apps.knowledge import resilience

    def full_gate(service, **_kw):
        raise ServiceUnavailable(service, "跨进程并发闸门已满（同时最多 1 个外呼）")

    monkeypatch.setattr(ratelimit, "slots", full_gate)
    http = _FakeHttp()
    with override_settings(KB_MODEL_CALLS_DISABLED=False):
        with pytest.raises(ServiceUnavailable) as excinfo:
            resilience.post_json(
                http=http, url="http://example.invalid/v1", payload={}, service="llm"
            )
    assert excinfo.value.service == "llm"
    assert http.calls == 0, "闸门满了就绝不发请求"


class _FakeHttp:
    def __init__(self) -> None:
        self.calls = 0

    def post(self, url, json=None, headers=None):  # noqa: A002 - 对齐 httpx 签名
        self.calls += 1
        return _FakeResponse()


class _FakeResponse:
    status_code = 200
    text = "{}"

    def json(self):
        return {"ok": True}


# ---------------------------------------------------------------- 指标口径 ----


@pytest.mark.django_db
def test_metrics_report_cross_process_scope():
    from apps.knowledge.metrics import collect

    payload = collect()["model_concurrency"]
    assert payload["scope"] == "redis", "跨进程闸门与进程内熔断器必须分开标口径"
    assert payload["limit_per_service"] >= 1
    assert "embedding" in payload["in_flight"]
    assert "fallback" in payload
    assert set(payload["in_flight"]) == set(ratelimit.KNOWN_SERVICES)
    # 熔断器的口径必须**与它实际用的后端自洽**：离线回归用进程后端（scope=process、
    # redis_available=false）；只有在显式用共享后端时才允许 scope=global。两条一起断言，
    # 防止"说是全局其实只清了本进程"这种伪报（独立审查第 7 项）。
    breakers = collect()["breakers"]
    assert breakers["scope"] in {"global", "process"}, breakers
    if breakers["scope"] == "global":
        assert breakers["backend"] == "redis" and breakers["redis_available"] is True, breakers
    else:
        assert breakers["redis_available"] is False, breakers
