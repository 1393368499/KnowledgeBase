"""外部模型服务的韧性层：超时、重试上限、熔断、明确不可用状态。

背景（用户 2026-10-13 指示）
----------------------------
AS711 断开时**开发不能停**。所以外呼必须满足四条：

1. **超时**：每个服务有独立超时，不允许无限等；
2. **重试上限**：只对"可重试"的失败（连接错误、超时、5xx、429）重试，
   且次数写死上限；维度不匹配、400/422 这类"重试也不会变好"的错误立刻放弃；
3. **熔断**：连续失败到阈值就 **打开断路器**，后续调用直接快速失败，
   不再把请求打到挂掉的服务上（否则每次上传都要等满超时）；
4. **明确不可用**：断路器打开时抛 :class:`ServiceUnavailable`，
   带上"什么时候恢复"和"上次失败原因"，调用方能在界面上如实显示，
   而不是假装成功、也不能把失败当"没有结果"。

为什么不用第三方库（如 tenacity / pybreaker）
--------------------------------------------
逻辑只有几十行，而"什么时候算失败、熔断后什么时候半开"是我们自己的业务口径，
写在这里比调库参数更好审查。依赖越少，干净环境重装越稳。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


class ServiceUnavailable(RuntimeError):
    """服务当前不可用（熔断打开，或判定为不可重试的失败）。

    这个异常是"如实告知"的载体：界面与 API 都据它显示"模型服务不可用"，
    而不是显示"检索到 0 条"——后者会让人误以为库里没有东西。
    """

    def __init__(
        self,
        service: str,
        reason: str,
        *,
        retry_after_seconds: float | None = None,
        attempts: int = 0,
    ) -> None:
        super().__init__(f"{service} 不可用：{reason}")
        self.service = service
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds
        self.attempts = attempts

    def as_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "reason": self.reason,
            "retry_after_seconds": self.retry_after_seconds,
            "attempts": self.attempts,
        }


#: 判定为"可以重试"的 HTTP 状态码（服务端临时问题）
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


@dataclass
class CircuitBreaker:
    """一个服务的连续失败计数 + 熔断状态（共享存储可用时以**共享状态**为准）。

    状态机：``closed`` --失败达阈值--> ``open`` --冷却结束--> ``half_open``
    --试一次成功--> ``closed``；半开时再失败 --> 立刻回 ``open``。

    **跨进程一致性（用户 2026-10-16 第 7 项）**
    -------------------------------------------
    web 与 worker 是两个进程，进程内计数只能保护自己：某进程"重置熔断器"后，
    另一个进程其实还开着，界面若说"已重置"就是**伪报成功**。所以当 :attr:`store`
    挂了共享存储（:mod:`apps.knowledge.breaker_store`，Redis + Lua 原子脚本）时：

    * :meth:`guard` / :meth:`record_failure` / :meth:`record_success` / :meth:`reset`
      都读写**共享**状态，多个进程看到同一份；半开时"只放一个探测"由 Redis 里
      **带租约的探测令牌**保证（跨进程也不超发，持有者崩溃后租约到期自动让位）；
    * 进程内仍保留一份**降级镜像**：Redis 不可用时立刻退回原有进程内行为，
      不抛异常打断检索；但 :meth:`snapshot` 会如实标注 ``scope=process``
      + ``redis_available=false`` + 原因，绝不假装全局。

    ``store=None``（直接构造，例如单测/离线）就是**纯进程内**行为——与旧版一致。
    生产代码只有 :func:`breaker_for` 一个构造入口，它按 ``KB_BREAKER_STORE``
    （默认 ``redis``）决定是否挂共享存储。
    """

    name: str
    failure_threshold: int = 3
    cooldown_seconds: float = 60.0
    #: 共享存储（:class:`apps.knowledge.breaker_store.BreakerStore`）；None/False = 纯进程内。
    store: Any | None = None
    _failures: int = 0
    _opened_at: float = 0.0
    _half_open: bool = False
    _probing: bool = False
    _last_reason: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    #: 进程内"拒绝"短缓存：只缓存**拒绝**（Redis 侧已判定冷却中），
    #: 避免熔断期间每个请求都打一次 Redis；绝不用它缓存"放行"。
    _shared_deny_until: float = 0.0

    # ------------------------------------------------------- 共享存储桥接 ----
    def _active_store(self) -> Any | None:
        store = self.store
        if store is None or store is False:
            return None
        if not getattr(store, "enabled", True):
            return None
        return store

    def _shared_record(self) -> tuple[bool, dict[str, Any] | None, str]:
        """``(共享存储是否可用, 该服务的共享记录或 None, 错误原因)``。

        任何异常都在这里被吞掉并当作"共享不可用"——**绝不**让 Redis 抖动打断检索。
        """
        store = self._active_store()
        if store is None:
            return False, None, ""
        try:
            result = store.try_read(
                self.name,
                failure_threshold=self.failure_threshold,
                cooldown_seconds=self.cooldown_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - 共享存储异常不允许冒到业务路径
            logger.warning("读取共享熔断状态失败（退回进程内）：%s: %s", type(exc).__name__, exc)
            return False, None, f"{type(exc).__name__}: {exc}"
        if not result.get("ok"):
            return False, None, str(result.get("error") or "共享存储不可用")
        return True, result.get("record"), ""

    # ------------------------------------------------------------ 查询 ----
    @property
    def state(self) -> str:
        ok, record, _error = self._shared_record()
        if ok:
            # 共享存储可用 → 全局口径（无记录 = 全局闭合）
            return str((record or {}).get("state") or "closed")
        with self._lock:
            return self._state_locked()

    def _state_locked(self) -> str:
        if self._failures < self.failure_threshold:
            return "closed"
        if (time.monotonic() - self._opened_at) >= self.cooldown_seconds:
            return "half_open"
        return "open"

    def _local_snapshot_locked(self) -> dict[str, Any]:
        state = self._state_locked()
        remaining = 0.0
        if state == "open":
            remaining = max(
                0.0, self.cooldown_seconds - (time.monotonic() - self._opened_at)
            )
        return {
            "service": self.name,
            "state": state,
            "consecutive_failures": self._failures,
            "failure_threshold": self.failure_threshold,
            "cooldown_seconds": self.cooldown_seconds,
            "retry_after_seconds": round(remaining, 1),
            "last_reason": self._last_reason,
        }

    def snapshot(self) -> dict[str, Any]:
        """快照。**口径如实**：共享可用时 ``scope=global``，否则 ``scope=process``。"""
        with self._lock:
            local = self._local_snapshot_locked()
        store = self._active_store()
        if store is None:
            local.update(
                {
                    "scope": "process",
                    "backend": "process",
                    "redis_available": False,
                    "redis_error": "",
                    "shared": False,
                }
            )
            return local
        ok, record, error = self._shared_record()
        if not ok:
            local.update(
                {
                    "scope": "process",
                    "backend": "process",
                    "redis_available": False,
                    "redis_error": error,
                    "shared": False,
                    "note": "共享熔断状态读不到 → 本行是**进程内**口径，不代表其它 web/worker 进程",
                }
            )
            return local
        shared = record or {}
        backend = str(getattr(store, "backend_name", "redis") or "redis")
        return {
            "service": self.name,
            "state": str(shared.get("state") or "closed"),
            "consecutive_failures": int(shared.get("consecutive_failures") or 0),
            "failure_threshold": int(
                shared.get("failure_threshold") or self.failure_threshold
            ),
            "cooldown_seconds": float(shared.get("cooldown_seconds") or self.cooldown_seconds),
            "retry_after_seconds": float(shared.get("retry_after_seconds") or 0.0),
            "last_reason": str(shared.get("last_reason") or ""),
            "probe_in_flight": bool(shared.get("probe_in_flight", False)),
            "opened_at": float(shared.get("opened_at") or 0.0),
            "local_consecutive_failures": int(local["consecutive_failures"]),
            "scope": "global",
            "backend": backend,
            "redis_available": True,
            "redis_error": "",
            "shared": True,
        }

    def allow_request(self) -> bool:
        """**只读**地问一句"现在还能不能发请求"，不抛异常、不改状态。

        用途：检索这类**有降级路径**的场景可以据此提前跳过外呼
        （服务已经确认挂掉时立刻走关键字），避免每个请求都先付一次超时的代价。
        注意它不代替 :meth:`guard`：真正要发请求前仍必须 guard（半开门闩在那里）。

        共享存储可用时读的是**全局**状态（其它进程打开的熔断在这里同样可见），
        并且**不**声明探测权（只读，不改变半开门闩）。
        """
        ok, record, _error = self._shared_record()
        if ok:
            if not record:
                return True
            state = str(record.get("state") or "closed")
            if state == "open":
                return False
            if state == "half_open" and record.get("probe_in_flight"):
                return False
            return True
        with self._lock:
            state = self._state_locked()
            if state == "open":
                return False
            if state == "half_open" and self._probing:
                return False
            return True

    def guard(self) -> None:
        """调用前检查：熔断打开时立刻抛；半开时**只放一个探测请求**。

        "半开只放一个"很重要（审查意见）：如果不加门闩，冷却刚结束的那一瞬间，
        所有并发请求会一起打到刚刚恢复（可能还很脆弱）的服务上，等于用一次
        "恢复"把它再打挂一次。

        共享存储在时门闩在 **Redis**（Lua 原子授予带租约的探测权），所以
        web 与 worker 之间也不会超发；退避/不可用时退回进程内布尔门闩 + 锁。
        """
        store = self._active_store()
        if store is not None:
            now = time.monotonic()
            if now < self._shared_deny_until:
                raise ServiceUnavailable(
                    self.name,
                    "共享熔断状态：冷却中（本进程短期拒绝缓存，不重复打 Redis）",
                    retry_after_seconds=round(max(0.0, self._shared_deny_until - now), 1),
                )
            try:
                decision = store.try_guard(
                    self.name,
                    failure_threshold=self.failure_threshold,
                    cooldown_seconds=self.cooldown_seconds,
                )
            except Exception as exc:  # noqa: BLE001 - 共享异常退回进程内
                logger.warning(
                    "共享熔断检查失败（退回进程内）：%s: %s", type(exc).__name__, exc
                )
                decision = {"ok": False}
            if decision.get("ok"):
                if decision.get("allowed"):
                    return
                retry_after = max(0.0, float(decision.get("retry_after_seconds") or 0.0))
                cache = min(retry_after, float(getattr(store, "deny_cache_seconds", 0.0) or 0.0))
                self._shared_deny_until = now + cache
                if decision.get("state") == "half_open":
                    raise ServiceUnavailable(
                        self.name,
                        "熔断半开中：已有一个探测请求在飞（共享门闩，跨 web/worker），"
                        "其余请求快速失败等待结果",
                        retry_after_seconds=retry_after,
                    )
                raise ServiceUnavailable(
                    self.name,
                    (
                        f"连续 {decision.get('consecutive_failures')} 次失败已熔断"
                        f"（共享状态，跨进程一致），冷却中；上次原因：{decision.get('last_reason') or ''}"
                    ),
                    retry_after_seconds=retry_after,
                )
            # 共享存储不可用 → 落到下面的进程内快路径（不打断业务）
        with self._lock:
            state = self._state_locked()
            if state == "open":
                remaining = max(
                    0.0, self.cooldown_seconds - (time.monotonic() - self._opened_at)
                )
                raise ServiceUnavailable(
                    self.name,
                    f"连续 {self._failures} 次失败已熔断，冷却中；上次原因：{self._last_reason}",
                    retry_after_seconds=round(remaining, 1),
                )
            if state == "half_open":
                if self._probing:
                    raise ServiceUnavailable(
                        self.name,
                        "熔断半开中：已有一个探测请求在飞，其余请求快速失败等待结果",
                        retry_after_seconds=0.0,
                    )
                self._probing = True

    def record_success(self) -> None:
        """成功：**全局**清空该服务的熔断状态（web/worker 一起恢复）。"""
        with self._lock:
            if self._failures:
                logger.info("服务 %s 恢复：清空 %d 次连续失败", self.name, self._failures)
            self._failures = 0
            self._opened_at = 0.0
            self._half_open = False
            self._probing = False
            self._last_reason = ""
            self._shared_deny_until = 0.0
        store = self._active_store()
        if store is not None:
            try:
                store.try_record_success(self.name)
            except Exception as exc:  # noqa: BLE001 - 共享写失败只影响跨进程可见性
                logger.warning(
                    "写共享熔断状态失败（成功未同步，仅本进程恢复）：%s: %s",
                    type(exc).__name__,
                    exc,
                )

    def record_failure(self, reason: str) -> None:
        with self._lock:
            self._probing = False  # 探测失败：门闩释放，下次半开再放一个
            self._failures += 1
            self._last_reason = reason[:300]
            if self._failures >= self.failure_threshold:
                self._opened_at = time.monotonic()
                logger.warning(
                    "服务 %s 熔断打开（连续 %d 次失败）：%s",
                    self.name,
                    self._failures,
                    self._last_reason,
                )
        store = self._active_store()
        if store is not None:
            try:
                result = store.try_record_failure(
                    self.name,
                    failure_threshold=self.failure_threshold,
                    cooldown_seconds=self.cooldown_seconds,
                    reason=reason[:300],
                )
                if result.get("ok"):
                    # 刚打开 → 顺手填上进程内拒绝缓存（只缓存拒绝）
                    if result.get("opened") and self.cooldown_seconds > 0:
                        cache = min(
                            self.cooldown_seconds,
                            float(getattr(store, "deny_cache_seconds", 0.0) or 0.0),
                        )
                        self._shared_deny_until = time.monotonic() + max(0.0, cache)
            except Exception as exc:  # noqa: BLE001 - 共享写失败只影响跨进程可见性
                logger.warning(
                    "写共享熔断状态失败（本次失败未同步，仅本进程计数）：%s: %s",
                    type(exc).__name__,
                    exc,
                )

    def reset(self) -> None:
        """把断路器复位：**共享可用时连共享状态一起清掉**（跨进程生效）。

        （用户 2026-10-16 第 7 项：不能只清当前进程。若 Redis 不可用，
        这里只清得掉本进程，调用方必须用 :func:`reset_all_breakers` 的返回结构
        如实告知"没做成全局重置"。）
        """
        with self._lock:
            self._failures = 0
            self._opened_at = 0.0
            self._half_open = False
            self._probing = False
            self._last_reason = ""
            self._shared_deny_until = 0.0
        store = self._active_store()
        if store is not None:
            try:
                store.try_reset(self.name)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "清除共享熔断状态失败（仅本进程已复位）：%s: %s", type(exc).__name__, exc
                )


#: 每个服务一个断路器（**进程内注册表**：这里是本进程的视图/降级快照）。
#: 跨进程一致由 :mod:`apps.knowledge.breaker_store`（Redis）保证，见 ``breaker_for``。
BREAKERS: dict[str, CircuitBreaker] = {}


def shared_breaker_store() -> Any | None:
    """按 ``KB_BREAKER_STORE``（默认 ``redis``）拿共享存储；关闭/不可用时返回 None。

    **注意**：``KB_BREAKER_STORE=process`` 是显式的"进程内后端"（离线回归/单测用），
    此时所有熔断状态与重置都只作用于本进程，快照里如实标注 ``scope=process``。
    """
    from .breaker_store import breaker_store

    store = breaker_store()
    return store if store.enabled else None


def breaker_for(
    name: str,
    *,
    failure_threshold: int = 3,
    cooldown_seconds: float = 60.0,
    store: Any | None = None,
) -> CircuitBreaker:
    """取（或建）某服务的断路器。

    ``store`` 省略时按配置挂共享存储（``KB_BREAKER_STORE``，默认 redis）；
    显式传 ``store=False`` 可强制进程内后端（单测注入 MockTransport 故障时必须这样，
    否则假的故障会被写进真实的共享熔断状态）。
    """
    active = None if store is False else (store if store is not None else shared_breaker_store())
    existing = BREAKERS.get(name)
    if existing is None:
        existing = CircuitBreaker(
            name=name,
            failure_threshold=failure_threshold,
            cooldown_seconds=cooldown_seconds,
            store=active,
        )
        BREAKERS[name] = existing
    elif existing.store in (None, False) and active is not None:
        # 注册表里已有实例但当时没挂共享存储（例如配置后打开）→ 补挂
        existing.store = active
    return existing


def breaker_snapshots() -> list[dict[str, Any]]:
    """本进程注册表里每个服务的快照（每项自带 ``scope``/``redis_available``）。"""
    return [breaker.snapshot() for breaker in BREAKERS.values()]


def breaker_state_snapshot() -> dict[str, Any]:
    """熔断状态的**完整如实口径**（指标/健康检查用）。

    字段：``scope``（``global``/``process``）、``backend``、``redis_available``、
    ``redis_error``、``store_enabled``、``prefix``、``redis_db``、
    ``items``（本进程注册表快照）、``shared``（**跨进程**共享状态列表）、``note``。
    """
    from .breaker_store import breaker_store_snapshot

    shared = breaker_store_snapshot()
    scope = str(shared.get("scope") or "process")
    if scope == "global":
        note = (
            "熔断状态在 Redis 共享（web/worker 共用一份），scope=global；"
            "shared 为跨进程全量状态，items 为本进程注册表视图"
        )
    else:
        note = (
            "共享熔断存储不可用 → scope=process：items 只代表**本进程**，"
            f"重置也只能作用于本进程。原因：{shared.get('redis_error') or '未启用'}"
        )
    return {
        "scope": scope,
        "backend": str(shared.get("backend") or "process"),
        "redis_available": bool(shared.get("redis_available")),
        "redis_error": str(shared.get("redis_error") or ""),
        "store_enabled": bool(shared.get("enabled")),
        "prefix": str(shared.get("prefix") or ""),
        "redis_db": str(shared.get("redis_db") or ""),
        "items": breaker_snapshots(),
        "shared": list(shared.get("items") or []),
        "note": note,
    }


def reset_all_breakers() -> dict[str, Any]:
    """**重置熔断器**的统一入口（跨 web/worker）。

    返回结构（planner/界面按它如实回答，**Redis 不可用时绝不声称全局成功**）：
      * ``scope``：``global``（共享状态真的被清空）或 ``process``（只清了本进程）；
      * ``redis_available`` (bool)、``redis_error`` (str)；
      * ``global_reset`` (bool)：**只有**共享 Redis 被清空且复查无残留才为 True；
      * ``reset``：逐服务结果 ``[{service, reset, source, local, error}, ...]``；
      * ``reset_count`` / ``local_reset_count`` / ``services`` / ``remaining``；
      * ``note`` / ``scope_note``：人话说明。
    """
    from .breaker_store import breaker_store

    store = breaker_store()
    local_names = sorted(BREAKERS.keys())
    # 先清共享（原子），再清本进程注册表（顺带把每一条的共享键再删一次，幂等）
    shared = store.reset_all()
    for breaker in list(BREAKERS.values()):
        breaker.reset()

    entries: dict[str, dict[str, Any]] = {}
    for item in shared.get("reset") or []:
        name = str(item.get("service") or "")
        if name:
            entries[name] = dict(item)
    for name in local_names:
        entry = entries.setdefault(
            name,
            {"service": name, "reset": True, "source": "process", "local": True, "error": ""},
        )
        entry["local"] = True
    for name, entry in entries.items():
        entry.setdefault("local", name in local_names)

    result = dict(shared)
    result["reset"] = [entries[name] for name in sorted(entries)]
    result["reset_count"] = len(entries)
    result["services"] = sorted(entries)
    result["local_reset_count"] = len(local_names)
    result["process_reset"] = local_names
    if result.get("redis_available") and result.get("global_reset"):
        result["scope_note"] = (
            "全局重置完成：共享 Redis 熔断状态已清空并复查无残留，"
            "web 与 worker 都受影响"
        )
    elif result.get("redis_available"):
        result["scope_note"] = (
            f"共享 Redis 已执行重置，但复查仍有 {result.get('remaining')} 个键"
            " → **不能**声称全局重置成功"
        )
    else:
        result["scope_note"] = (
            "**未完成全局重置**：共享存储不可用"
            f"（{result.get('redis_error') or '未知原因'}），只重置了本进程内的 "
            f"{len(local_names)} 个熔断器，其它 web/worker 进程的状态仍然有效"
        )
    return result


class RetryableHttpError(RuntimeError):
    """可重试的外呼失败（连接错误、超时）。"""


class RetryExhausted(ServiceUnavailable):
    """重试用尽仍未成功。"""


class ServiceHttpError(RuntimeError):
    """服务返回了非 200。``http_status`` 决定它是否可重试。"""

    def __init__(self, message: str, *, http_status: int) -> None:
        super().__init__(message)
        self.http_status = http_status


#: 这些状态码值得重试：超时/限流/服务端临时故障。
#: 400/401/403/404/422 属于"重试也不会变好"，立刻失败，不浪费次数。
RETRYABLE_STATUS: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})


def _is_retryable(exc: Exception) -> bool:
    """判断一个异常是否值得重试。**判定口径只有这一处。**

    顺序很重要：**先看 HTTP 状态码**，再看 ``retryable`` 标记。

    为什么状态码优先：异常类可以同时带 ``http_status`` 和 ``retryable``，
    如果先看标记，一个把 ``retryable`` 默认设成 False 的类就会让
    "503 应该重试"变成"不重试"——分类被标记悄悄覆盖，很难查。
    状态码是更具体的信息，它说了算。

    其他情形：
    - 明确标了 ``retryable=True`` 的连接/超时类异常 → 重试；
    - 其余一律不重试（结构异常、维度不符、解析失败，重试也不会变好）。
    """
    status = getattr(exc, "http_status", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    if status is not None:
        return int(status) in RETRYABLE_STATUS
    flag = getattr(exc, "retryable", None)
    if flag is not None:
        return bool(flag)
    return isinstance(exc, (RetryableHttpError, ConnectionError, TimeoutError, OSError))


def model_calls_disabled() -> bool:
    """**离线/无模型模式**是否打开（见 settings.KB_MODEL_CALLS_DISABLED）。

    打开时所有模型外呼立刻失败，不做任何网络尝试。用途：AS711 未就绪时
    让界面可用且**绝不外呼**（不会卡超时，也不会有真实请求）。
    """
    try:
        from django.conf import settings

        return bool(getattr(settings, "KB_MODEL_CALLS_DISABLED", False))
    except Exception:  # noqa: BLE001 - 配置不可读时按"正常"处理
        return False


def disabled_service(service: str) -> ServiceUnavailable:
    """离线模式下统一构造的"服务未启用"异常。"""
    return ServiceUnavailable(
        service,
        "服务未启用（离线模式 KB_MODEL_CALLS_DISABLED=1）：未发起任何外呼",
    )


def breaker_snapshot(name: str) -> dict[str, Any]:
    breaker = BREAKERS.get(name)
    return breaker.snapshot() if breaker is not None else {}


def post_json(
    *,
    http,
    url: str,
    payload: dict[str, Any],
    service: str,
    max_attempts: int = 3,
    breaker: CircuitBreaker | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    """POST 一个 JSON 接口并解析响应，**统一带熔断 + 重试上限 + 明确不可用**。

    为什么做成共享函数：embedding / rerank / ocr 三个客户端曾经各写一套外呼
    （rerank 和 ocr 干脆完全没接韧性层，服务掉线时就是一个裸 httpx 异常冒到接口）。
    统一入口之后，"超时多久、重试几次、什么时候熔断"只有一处口径。

    **离线模式的检查放在最前面**：连"构造客户端"这一步都不做。
    （踩过：客户端是调用方先造好再传进来的，所以只在"请求前"检查还不够——
    测试的离线守卫会在构造阶段就抛错，报出来的错和真实原因对不上。）
    """
    import httpx

    active_breaker = breaker or breaker_for(
        service,
        failure_threshold=3,
        cooldown_seconds=60.0,
    )

    def once() -> Any:
        if model_calls_disabled():
            raise disabled_service(service)
        try:
            response = http.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise RetryableHttpError(f"请求超时：{exc}") from exc
        except httpx.HTTPError as exc:
            raise RetryableHttpError(f"网络错误：{exc}") from exc
        if response.status_code != 200:
            raise ServiceHttpError(
                f"服务返回 {response.status_code}：{response.text[:300]}",
                http_status=response.status_code,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ValueError(f"响应不是合法 JSON：{response.text[:200]}") from exc

    return call_with_resilience(
        service=service,
        operation="post",
        func=once,
        breaker=active_breaker,
        max_attempts=max_attempts,
    )


def call_with_resilience(
    *,
    service: str,
    operation: str,
    func: Callable[[], Any],
    breaker: CircuitBreaker,
    max_attempts: int = 3,
    base_delay_seconds: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """按"熔断 → 调用 → 分类失败 → 退避重试"执行一次外呼。

    ``max_attempts`` 是**总尝试次数**（1 表示不重试）。失败分类：

    - :class:`ServiceUnavailable`（熔断中）直接往外抛，不再叠加重试；
    - 可重试（``retryable=True`` 标记的异常，或 HTTP 5xx/429）→ 退避后重试；
    - 不可重试（4xx、维度不匹配、解析失败）→ 立刻抛，不浪费重试次数。

    注意（曾被 ruff F811 抓到）：这里原本还有一个**只有 docstring、没有函数体**的
    同名定义，被这一个静默覆盖。运行时行为是对的（后者生效），但死代码会让
    "我改的那个函数到底生不生效"变得不确定——已删除。
    """
    attempts = max(1, int(max_attempts))
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        breaker.guard()  # 熔断中会在这里抛 ServiceUnavailable
        try:
            # **跨进程并发闸门挂在这里**：embedding/rerank/ocr/llm 四条外呼路径
            # 全部经由本函数，所以"同时打给 AS711 的请求数"只有这一处口径。
            # 额度是 Redis 里跨进程共享的（进程内熔断器做不到这件事）。
            # 闸门等不到额度会抛 ServiceUnavailable，被下面的分支原样抛出，
            # **不**计入熔断失败（那不是被保护的服务本身出问题）。
            from .ratelimit import slots

            with slots(service):
                result = func()
        except ServiceUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - 分类由 _is_retryable 决定
            retryable = _is_retryable(exc)
            breaker.record_failure(f"{operation} 第 {attempt} 次失败：{exc}")
            last_exc = exc
            # 重试用尽：把最终失败统一包装成"服务不可用"。
            # 为什么不在用尽时抛原始异常：调用方（上传/检索接口）需要能**统一**回答
            # "模型服务不可用"，而不是去认每一种底层异常类型；原始异常仍在 __cause__ 里。
            if attempt >= attempts:
                raise RetryExhausted(
                    service,
                    f"{operation} 尝试 {attempts} 次仍未成功：{exc}",
                    attempts=attempts,
                ) from exc
            if not retryable:
                raise
            delay = base_delay_seconds * (2 ** (attempt - 1))
            logger.warning(
                "%s.%s 第 %d/%d 次失败（可重试），%.1fs 后重试：%s",
                service,
                operation,
                attempt,
                attempts,
                delay,
                exc,
            )
            sleep(delay)
            continue
        else:
            breaker.record_success()
            return result
    raise RetryExhausted(
        service, f"{operation} 尝试 {attempts} 次仍未成功：{last_exc}", attempts=attempts
    )


def service_unavailable_snapshot() -> dict[str, Any]:
    """给健康检查/界面用：当前所有外呼服务的熔断状态（含**全局/进程**口径）。

    ``breakers`` 保持旧结构（本进程注册表列表，向后兼容）；
    ``breaker_state`` 是如实口径块（共享状态是否可用、跨进程状态列表）。
    """
    state = breaker_state_snapshot()
    return {
        "breakers": state["items"],
        "breaker_state": state,
        "scope": state["scope"],
        "redis_available": state["redis_available"],
    }
