"""跨进程模型外呼闸门：**Redis 排序集合信号量**。

为什么需要它（这是文档里如实登记的未完成项之一）
------------------------------------------------
``resilience.CircuitBreaker`` 是**进程内**状态：多进程下每个 web/worker 进程
各自计数，"同时打给 AS711 的请求数"也是每个进程各算一份。而 AS711 上四个模型
服务跑在同一台机器上，upload（worker）与检索/对话（web）同时开跑时并发会叠加，
进程内限流挡不住。所以闸门必须放在**所有进程都能看见的共享存储**里。

为什么用排序集合，而不是简单的 ``INCR/DECR`` 计数器
----------------------------------------------------
朴素计数器有两个都很难接受的失效模式：

* 持有者被 SIGKILL → ``DECR`` 永远不会执行 → **额度被永久占死**；
* 给键加整体 TTL 来兜底 → 负载持续时过期会把**正在飞**的持有者也一起清零，
  造成超发（比 leak 更糟：超发会打挂本来要保护的服务）。

排序集合（``ZADD member=持有者, score=获得时刻``）可以同时避开两者：

* ``acquire`` 时先 ``ZREMRANGEBYSCORE`` 清掉**超过 TTL 的单个持有者**（自愈），
  再在 ``ZCARD < limit`` 时 ``ZADD`` 自己 —— 原子 Lua 脚本，无竞态；
* ``release`` 就是 ``ZREM member``；崩溃的持有者只会漏掉自己的那一个，
  且会被后来的 acquire 顺手清掉，**不会**在负载持续时永久占额度。

口径与降级（必须能查证，不是"大概没事"）
----------------------------------------
* Redis 不可用 → **fail-open 放行**，并把降级次数如实计入 ``fallback_counts()``
  （指标里报 ``available=False``）。理由：闸门是保护模型服务的，不能因为 Redis
  抖动让检索/入库整体停摆；但"降级了"这件事必须能被看到。
* 拿不到额度 → 轮询等到 deadline，超时抛 :class:`ServiceUnavailable`，
  **不放行也不假装成功**（调用方与界面会如实显示"模型服务不可用"）。
* TTL 必须大于单次外呼的最坏耗时（默认 300s > ``KB_LLM_DOC_TIMEOUT_SECONDS``=180s），
  否则正在飞的持有者会被误判为过期。
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

#: 键前缀：``kb:model:slots:<service>``。**不碰业务队列的键**（DB0 的 celery 列表）。
DEFAULT_PREFIX = "kb:model:slots"
DEFAULT_LIMIT = 4
DEFAULT_TTL_SECONDS = 300
DEFAULT_WAIT_SECONDS = 10.0
DEFAULT_POLL_SECONDS = 0.05

#: Redis 出错后，这段窗口内不再尝试连（避免每个外呼都白等一次连接超时）。
_UNAVAILABLE_BACKOFF_SECONDS = 5.0


def _new_token() -> str:
    """**每次取额度**都要一个新令牌。

    踩过的坑（就是这里）：第一版把令牌做成**模块级常量**（每进程一个），于是同进程
    第二次 ``ZADD`` 用的是同一个 member，直接把第一条覆盖掉 —— ``ZCARD`` 永远是 1，
    闸门形同不存在，而且不会报错（``ZADD`` 覆盖同 member 是合法操作）。
    令牌必须唯一到"这一次占用"，release 才能精确移除自己那一个。
    """
    return f"{os.getpid()}:{threading.get_ident()}:{uuid.uuid4().hex}"

_ACQUIRE_LUA = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
local member = ARGV[3]
local now = tonumber(ARGV[4])
-- 自愈：清掉超过 TTL 的单个持有者（崩溃/被 SIGKILL 的进程不会自己 release）
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - ttl)
if redis.call('ZCARD', key) < limit then
  redis.call('ZADD', key, now, member)
  redis.call('PEXPIRE', key, math.floor(ttl * 1000))
  return 1
end
return 0
"""

_RELEASE_LUA = """
local key = KEYS[1]
local member = ARGV[1]
return redis.call('ZREM', key, member)
"""


class _FallbackCounter:
    """fail-open 降级次数（进程内计数 + 最后一次原因，供指标如实展示）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}
        self._last_reason = ""

    def record(self, service: str, reason: str) -> None:
        with self._lock:
            self._counts[service] = self._counts.get(service, 0) + 1
            self._last_reason = reason[:300]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "total": sum(self._counts.values()),
                "by_service": dict(self._counts),
                "last_reason": self._last_reason,
            }


_FALLBACKS = _FallbackCounter()


def fallback_counts() -> dict[str, Any]:
    """Redis 不可用而放行的次数（指标用；全 0 表示闸门一直真实生效）。"""
    return _FALLBACKS.snapshot()


class SlotLimiter:
    """按服务限制**同时在飞**的外呼数，额度由 Redis 跨进程共享。"""

    def __init__(
        self,
        *,
        url: str = "",
        prefix: str = DEFAULT_PREFIX,
        limit: int = DEFAULT_LIMIT,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        wait_seconds: float = DEFAULT_WAIT_SECONDS,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        enabled: bool = True,
        client: Any | None = None,
        service_limits: dict[str, int] | None = None,
        service_wait_seconds: dict[str, float] | None = None,
    ) -> None:
        self.url = url
        self.prefix = prefix
        self.limit = max(1, int(limit))
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self.wait_seconds = max(0.0, float(wait_seconds))
        self.poll_seconds = max(0.01, float(poll_seconds))
        self.enabled = bool(enabled)
        self.service_limits = {key: max(1, int(value)) for key, value in (service_limits or {}).items()}
        self.service_wait_seconds = {key: max(0.0, float(value)) for key, value in (service_wait_seconds or {}).items()}
        self._client = client
        self._client_error: str = ""
        self._client_unavailable_until = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 客户端 ----
    def _redis(self) -> Any | None:
        """拿 Redis 客户端；连不上返回 None（调用方 fail-open）。"""
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is not None:
                return self._client
            if time.monotonic() < self._client_unavailable_until:
                return None
            try:
                import redis

                url = self.url or _default_url()
                client = redis.Redis.from_url(
                    url, socket_timeout=1.5, socket_connect_timeout=1.5
                )
                client.ping()
            except Exception as exc:  # noqa: BLE001 - 连不上就降级，不让外呼失败
                self._client_error = f"{type(exc).__name__}: {exc}"
                self._client_unavailable_until = (
                    time.monotonic() + _UNAVAILABLE_BACKOFF_SECONDS
                )
                logger.warning("模型并发闸门不可用（本次放行）：%s", self._client_error)
                return None
            self._client = client
            self._client_error = ""
            return client

    def _key(self, service: str) -> str:
        return f"{self.prefix}:{service}"

    def limit_for(self, service: str) -> int:
        return self.service_limits.get(service.split(":", 1)[0], self.limit)

    def _connectivity(self) -> tuple[bool, str]:
        if not self.enabled:
            return False, "闸门已关闭（KB_MODEL_LIMIT_ENABLED=0）"
        if self._redis() is not None:
            return True, ""
        return False, self._client_error

    # ---------------------------------------------------------- 取/放额度 ----
    def try_acquire(self, service: str) -> str | None:
        """尝试立刻拿一个额度。

        返回值是**本次占用的令牌**（``release`` 要用它），失败/拿不到时返回 ``None``。
        为什么返回值不是布尔：令牌必须每次唯一（否则同进程的第二次占用会覆盖第一次，
        见 :func:`_new_token` 里的记录），而调用方要拿着同一个令牌才能精确释放自己那一个。

        三种结果的区分（都有断言覆盖）：
        * 返回令牌 —— 真拿到了额度（**必须** release）；
        * 返回令牌 —— 闸门已关闭 / Redis 不可用而**降级放行**（release 是空操作，
          降级次数计入 :func:`fallback_counts`）；
        * 返回 ``None`` —— 额度确实已满（Redis 是好的，没有降级）。
        """
        if not self.enabled:
            return _new_token()
        client = self._redis()
        if client is None:
            _FALLBACKS.record(service, self._client_error or "Redis 不可用")
            return _new_token()
        token = _new_token()
        try:
            granted = client.eval(
                _ACQUIRE_LUA,
                1,
                self._key(service),
                self.limit_for(service),
                self.ttl_seconds,
                token,
                time.time(),
            )
        except Exception as exc:  # noqa: BLE001 - Redis 抖动 → 降级放行
            self._client_error = f"{type(exc).__name__}: {exc}"
            _FALLBACKS.record(service, self._client_error)
            logger.warning("模型并发闸门取额度失败（本次放行）：%s", self._client_error)
            return _new_token()
        if granted:
            return token
        return None

    def release(self, service: str, token: str) -> None:
        """释放**自己那一个**额度；持有时崩溃的额度靠 acquire 的自愈清理。"""
        if not self.enabled or not token:
            return
        client = self._client
        if client is None:
            return
        try:
            client.eval(_RELEASE_LUA, 1, self._key(service), token)
        except Exception as exc:  # noqa: BLE001 - 释放失败只影响容量，不影响本次结果
            logger.warning("模型并发闸门释放失败：%s: %s", type(exc).__name__, exc)

    @contextmanager
    def slot(self, service: str, *, wait_seconds: float | None = None) -> Iterator[None]:
        """占用一个额度直到退出上下文；等不到就抛"服务不可用"。"""
        # 延迟导入：resilience 会在调用路径上导入本模块，模块级互相导入会成环。
        from .resilience import ServiceUnavailable

        if not self.enabled:
            yield
            return
        deadline = time.monotonic() + (
            self.service_wait_seconds.get(service.split(":", 1)[0], self.wait_seconds)
            if wait_seconds is None else max(0.0, float(wait_seconds))
        )
        token: str | None = None
        while True:
            token = self.try_acquire(service)
            if token is not None:
                break
            if time.monotonic() >= deadline:
                raise ServiceUnavailable(
                    service,
                    (
                        f"跨进程并发闸门已满（同时最多 {self.limit_for(service)} 个外呼），"
                        f"等待 {self.wait_seconds:g}s 仍未获得额度"
                    ),
                    retry_after_seconds=0.0,
                )
            time.sleep(self.poll_seconds)
        try:
            yield
        finally:
            self.release(service, token)

    # -------------------------------------------------------------- 快照 ----
    def in_flight(self, service: str) -> int | None:
        """当前在飞数（Redis 读不到时 None —— 指标如实显示"未知"，不填 0）。"""
        if not self.enabled:
            return None
        client = self._client
        if client is None:
            return None
        try:
            return int(client.zcard(self._key(service)) or 0)
        except Exception:  # noqa: BLE001
            return None

    def snapshot(self, services: list[str] | None = None) -> dict[str, Any]:
        available, reason = self._connectivity()
        services = services or list(KNOWN_SERVICES)
        return {
            "scope": "redis",
            "enabled": self.enabled,
            "available": available,
            "unavailable_reason": reason,
            "limit_per_service": self.limit,
            "service_limits": self.service_limits,
            "service_wait_seconds": self.service_wait_seconds,
            "slot_ttl_seconds": self.ttl_seconds,
            "wait_seconds": self.wait_seconds,
            "prefix": self.prefix,
            "in_flight": {name: self.in_flight(name) for name in services},
            "fallback": fallback_counts(),
            "note": (
                "额度由 Redis 跨进程共享（web 与 worker 共用同一份）；"
                "Redis 不可用时放行并把次数计入 fallback"
            ),
        }


#: 指标里逐个报在飞数的服务名（与 AS711 上的四个服务一致）。
KNOWN_SERVICES: tuple[str, ...] = ("embedding", "rerank", "ocr", "llm")


def _default_url() -> str:
    from django.conf import settings

    return str(
        getattr(settings, "KB_MODEL_LIMIT_REDIS_URL", "")
        or getattr(settings, "CELERY_BROKER_URL", "")
        or ""
    )


def build_limiter() -> SlotLimiter:
    """按 settings 构造（每次读配置，便于测试与运行时改配置）。"""
    from django.conf import settings

    return SlotLimiter(
        url=_default_url(),
        prefix=str(getattr(settings, "KB_MODEL_LIMIT_PREFIX", DEFAULT_PREFIX)),
        limit=int(getattr(settings, "KB_MODEL_MAX_CONCURRENCY", DEFAULT_LIMIT)),
        ttl_seconds=float(
            getattr(settings, "KB_MODEL_SLOT_TTL_SECONDS", DEFAULT_TTL_SECONDS)
        ),
        wait_seconds=float(
            getattr(settings, "KB_MODEL_SLOT_WAIT_SECONDS", DEFAULT_WAIT_SECONDS)
        ),
        enabled=bool(getattr(settings, "KB_MODEL_LIMIT_ENABLED", True)),
        service_limits={"ocr": int(getattr(settings, "KB_OCR_MAX_CONCURRENCY", 2))},
        service_wait_seconds={"ocr": float(getattr(settings, "KB_OCR_SLOT_WAIT_SECONDS", 300))},
    )


_LIMITER: SlotLimiter | None = None


def limiter() -> SlotLimiter:
    """进程内单例（额度本身在 Redis，跨进程共享；这里是避免每次重建客户端）。"""
    global _LIMITER
    if _LIMITER is None:
        _LIMITER = build_limiter()
    return _LIMITER


def reset_limiter() -> None:
    """测试用：丢弃单例（下次按当前 settings 重建）。"""
    global _LIMITER
    _LIMITER = None


@contextmanager
def slots(service: str, *, wait_seconds: float | None = None) -> Iterator[None]:
    """:meth:`SlotLimiter.slot` 的模块级入口（所有外呼的统一挂点）。"""
    with limiter().slot(service, wait_seconds=wait_seconds):
        yield


def limiter_snapshot() -> dict[str, Any]:
    """给指标用的只读快照（不含密钥，可安全对外）。"""
    return limiter().snapshot()
