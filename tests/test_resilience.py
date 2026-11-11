"""韧性层测试：超时边界、重试上限、失败分类、熔断状态机。

这些用例锁死用户 2026-10-13 的指示：
"模型接口保留超时、重试上限、熔断和明确不可用状态"。
全部离线可跑——不碰任何真实网络。
"""

from __future__ import annotations

import httpx
import pytest

from apps.knowledge.resilience import (
    RETRYABLE_STATUS,
    CircuitBreaker,
    RetryExhausted,
    ServiceUnavailable,
    call_with_resilience,
)

pytestmark = pytest.mark.django_db


class _Retryable(Exception):
    retryable = True


class _Fatal(Exception):
    retryable = False


class _HttpError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.http_status = status


# ------------------------------------------------------------- 熔断状态机 ----


def test_breaker_starts_closed():
    breaker = CircuitBreaker("svc", failure_threshold=3, cooldown_seconds=60)
    assert breaker.state == "closed"
    assert breaker.snapshot()["consecutive_failures"] == 0


def test_breaker_opens_exactly_at_threshold():
    breaker = CircuitBreaker("svc", failure_threshold=3, cooldown_seconds=60)
    breaker.record_failure("a")
    breaker.record_failure("b")
    assert breaker.state == "closed"  # 还差一次
    breaker.record_failure("c")
    assert breaker.state == "open"


def test_breaker_guard_raises_with_retry_after(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr("apps.knowledge.resilience.time.monotonic", lambda: clock["t"])
    breaker = CircuitBreaker("svc", failure_threshold=1, cooldown_seconds=30)
    breaker.record_failure("boom")
    with pytest.raises(ServiceUnavailable) as excinfo:
        breaker.guard()
    assert excinfo.value.retry_after_seconds == 30.0
    assert "boom" in excinfo.value.reason
    # 冷却过半：还是 open
    clock["t"] += 29
    assert breaker.state == "open"
    # 冷却结束：半开
    clock["t"] += 1
    assert breaker.state == "half_open"
    breaker.guard()  # 半开允许试探


def test_breaker_success_resets_everything():
    breaker = CircuitBreaker("svc", failure_threshold=2, cooldown_seconds=60)
    breaker.record_failure("a")
    breaker.record_failure("b")
    assert breaker.state == "open"
    breaker.record_success()
    assert breaker.state == "closed"
    snapshot = breaker.snapshot()
    assert snapshot["consecutive_failures"] == 0
    assert snapshot["last_reason"] == ""


def test_breaker_reset_is_explicit():
    breaker = CircuitBreaker("svc", failure_threshold=1, cooldown_seconds=60)
    breaker.record_failure("a")
    breaker.reset()
    assert breaker.state == "closed"


# --------------------------------------------------------- 重试与失败分类 ----


def test_retry_stops_at_max_attempts():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        raise _Retryable("网络抖动")

    breaker = CircuitBreaker("svc", failure_threshold=99)
    with pytest.raises(RetryExhausted) as excinfo:
        call_with_resilience(
            service="svc",
            operation="op",
            func=flaky,
            breaker=breaker,
            max_attempts=3,
            sleep=lambda _seconds: None,
        )
    assert calls["n"] == 3
    assert excinfo.value.attempts == 3
    assert isinstance(excinfo.value, ServiceUnavailable)


def test_success_after_retry_records_success():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _Retryable("还没好")
        return "ok"

    breaker = CircuitBreaker("svc", failure_threshold=5)
    result = call_with_resilience(
        service="svc",
        operation="op",
        func=flaky,
        breaker=breaker,
        max_attempts=3,
        sleep=lambda _seconds: None,
    )
    assert result == "ok"
    assert breaker.state == "closed"
    assert breaker.snapshot()["consecutive_failures"] == 0


def test_non_retryable_fails_immediately_without_wasting_attempts():
    calls = {"n": 0}

    def fatal():
        calls["n"] += 1
        raise _Fatal("参数错了，重试也没用")

    breaker = CircuitBreaker("svc", failure_threshold=5)
    with pytest.raises(_Fatal):
        call_with_resilience(
            service="svc",
            operation="op",
            func=fatal,
            breaker=breaker,
            max_attempts=5,
            sleep=lambda _seconds: None,
        )
    assert calls["n"] == 1  # 只打一次


@pytest.mark.parametrize(
    ("status", "should_retry"),
    [
        (500, True),
        (502, True),
        (503, True),
        (504, True),
        (429, True),
        (408, True),
        (400, False),
        (401, False),
        (403, False),
        (404, False),
        (422, False),
    ],
)
def test_http_status_classification(status, should_retry):
    calls = {"n": 0}

    def failing():
        calls["n"] += 1
        raise _HttpError(status)

    breaker = CircuitBreaker("svc", failure_threshold=99)
    with pytest.raises(Exception):  # noqa: B017 - 只关心重试次数
        call_with_resilience(
            service="svc",
            operation="op",
            func=failing,
            breaker=breaker,
            max_attempts=3,
            sleep=lambda _seconds: None,
        )
    assert calls["n"] == (3 if should_retry else 1)
    assert (status in RETRYABLE_STATUS) is should_retry


def test_breaker_short_circuits_before_any_call():
    calls = {"n": 0}

    def never_called():
        calls["n"] += 1
        return "ok"

    breaker = CircuitBreaker("svc", failure_threshold=1, cooldown_seconds=120)
    breaker.record_failure("已经挂了")
    with pytest.raises(ServiceUnavailable):
        call_with_resilience(
            service="svc",
            operation="op",
            func=never_called,
            breaker=breaker,
            max_attempts=3,
            sleep=lambda _seconds: None,
        )
    assert calls["n"] == 0


def test_backoff_is_exponential_and_bounded():
    """退避上限按指数增长，单次延迟落在 [0, 上限]（full jitter，规则 §4）。"""
    delays: list[float] = []
    ceilings: list[float] = []

    def flaky():
        raise _Retryable("x")

    def uniform(lo: float, hi: float) -> float:
        # 注入确定随机源：取上界，便于断言指数增长；不依赖真实随机、不 sleep。
        ceilings.append(hi)
        return hi

    breaker = CircuitBreaker("svc", failure_threshold=99)
    with pytest.raises(RetryExhausted):
        call_with_resilience(
            service="svc",
            operation="op",
            func=flaky,
            breaker=breaker,
            max_attempts=4,
            base_delay_seconds=0.5,
            sleep=delays.append,
            uniform=uniform,
        )
    assert ceilings == [0.5, 1.0, 2.0]
    assert delays == ceilings, "取上界采样时延迟应等于指数上限"
    assert all(0.0 <= delay <= ceiling for delay, ceiling in zip(delays, ceilings))


def test_backoff_uses_full_jitter_not_fixed_delays():
    """同一个失败序列的延迟应随随机源变化，而不是写死的 0.5/1.0/2.0。"""
    delays: list[float] = []

    def flaky():
        raise _Retryable("x")

    breaker = CircuitBreaker("svc", failure_threshold=99)
    with pytest.raises(RetryExhausted):
        call_with_resilience(
            service="svc",
            operation="op",
            func=flaky,
            breaker=breaker,
            max_attempts=4,
            base_delay_seconds=0.5,
            sleep=delays.append,
            uniform=lambda lo, hi: hi / 2,
        )
    assert delays == [0.25, 0.5, 1.0]


# ------------------------------------------------- 与嵌入客户端真实联通 ----


def test_embed_texts_honours_timeout_setting(profile1024):
    """超时是真实设置的（不是装饰）：httpx 超时对象上能读到我们给的值。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["timeout"] = request.extensions.get("timeout")
        return httpx.Response(
            200, json={"data": [{"embedding": [0.1] * 1024}]}, request=request
        )

    from apps.knowledge.embedding import embed_texts

    client = httpx.Client(transport=httpx.MockTransport(handler), timeout=7.5)
    vectors = embed_texts(["甲"], profile=profile1024, client=client, max_attempts=1)
    assert len(vectors[0]) == 1024
    assert captured["timeout"]["read"] == 7.5


@pytest.fixture
def profile1024():
    from apps.knowledge import embedding as emb

    return emb.get_profile("bge1024")
