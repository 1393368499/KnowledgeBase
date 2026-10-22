"""跨进程共享的熔断状态（Redis 原子状态）。

为什么需要它（用户 2026-10-16 第 7 项）
--------------------------------------
``apps.knowledge.resilience.CircuitBreaker`` 原先的状态是**进程内**变量：web 与
worker 是两个进程，各自计数。于是"重置熔断器"只清得掉当前进程的那一份——
界面/接口说"已重置"，另一个进程其实还开着，这就是**伪报成功**。本模块把状态放进
Redis，用 Lua 脚本保证原子读写，让 web 与 worker 看到**同一份**熔断状态：

* 连续失败数、打开时刻（冷却截止）、上次原因：所有进程共用一份；
* **半开只放一个探测**：探测权在 Redis 里用**带租约的令牌**登记，多个进程/线程
  同时进入半开时只有一个能拿到；持有者崩溃（没写回结果）后租约到期，探测权会被
  别人拿走，**不会永久卡死**（消费者审查意见 4）；
* 时间基准用 **Redis 服务器时间**（Lua 里的 ``TIME``）：不同进程的
  ``time.monotonic()`` 原点不同、不能跨进程比较；服务器时间是唯一共同基准。
  （本模块也用服务器时间做快照判定，读路径与写入路径同一把尺子。）

安全边界（硬约束，必须如实）
----------------------------
* **绝不在 DB0 上工作**：DB0 是共享生产业务队列（有别人的业务消息）。URL 指向
  DB0、没写 DB 序号（redis-py 对 ``redis://host:6379`` 默认就是 0）、或 DB 序号
  解析不出来时，本模块**拒绝工作**（连客户端都不建），并把原因写进
  ``redis_error``——宁可退回进程内，也不冒险碰业务库；
* **Redis 不可用不抛异常打断检索**：所有读写原语都返回 ``ok=False``，由
  :class:`~apps.knowledge.resilience.CircuitBreaker` 退回进程内行为；同时
  ``scope`` 如实降为 ``process``、``redis_available=False``。**绝不**在共享存储
  不可用时说"全局重置完成"（``global_reset=False`` + 可读原因）。
* 键前缀默认 ``kb:breaker:``，与业务队列的键完全不重叠；每个键都带 TTL，
  长时间没人用的状态会自动消失（不会在 Redis 里永久堆积）。

为什么按前缀建一个"服务索引"集合 —— 以及为什么**不能只信索引**
--------------------------------------------------------------
``reset_all()`` 要清空的是"本前缀下的全部熔断状态"。用 ``KEYS`` 扫整个库在 DB14
这种和 Celery 共用的库上会**阻塞 Redis**（O(全库)），所以每次写入都把服务名
``SADD`` 进一个索引集合（便于诊断"哪些服务在共享状态里"）。

但**索引绝不能是唯一依据**（独立审查实测到的缺陷）：索引键的 TTL 会被后来的写入
刷新，早期实现里"短冷却服务"的写入把索引 TTL 从 8000 砍到 600，于是索引先过期、
长冷却的熔断状态却还在 —— 只按 ``SMEMBERS(index)`` 删就会**漏删**，还把"全局重置"
报成成功。现在：

* 索引与状态键的 TTL 一律**只延长、绝不缩短**（Lua 里先读 ``TTL`` 再比较），
  所以索引不会比它记录的状态键先过期；
* ``global_snapshot()`` 与 ``reset_all()`` 都**按前缀 SCAN 真实发现键**
  （``kb:breaker:*``，并区分"状态键"与"索引键" ``__services__``），
  索引只作为"孤儿"判定的对照；
* ``reset_all()`` 的结果里如实给出 ``scanned``（真实扫描到多少键）、
  ``deleted``（删掉多少）、``orphans_found``/``orphan_services``（索引外的残留），
  并且**复查**前缀下是否还有键——只有复查为 0（且扫描未被截断）才敢说
  ``global_reset=True``。

配置（环境变量优先，其次 Django settings，默认值为生产口径）
------------------------------------------------------------
====================== ========================== ============================================
变量                   默认值                      含义
====================== ========================== ============================================
KB_BREAKER_STORE       ``redis``                  ``redis`` = 用共享存储；``process`` = 只用进程内
KB_BREAKER_REDIS_URL   见 :func:`_default_url`    共享状态用的 Redis 地址
KB_BREAKER_PREFIX      ``kb:breaker``             键前缀（尾部冒号会被规整）
KB_BREAKER_PROBE_TTL_SECONDS 300                 半开探测权租约（秒），崩溃后自动过期
KB_BREAKER_KEY_TTL_SECONDS   0（自动）            状态键 TTL；0 表示自动 ``>=600`` 且 ``>=冷却×4``
KB_BREAKER_DENY_CACHE_SECONDS 0.5                进程内"拒绝"缓存上限（只缓存拒绝，不缓存放行）
KB_BREAKER_REDIS_TIMEOUT_SECONDS 1.5             socket 超时
KB_BREAKER_REDIS_BACKOFF_SECONDS 5.0             Redis 出错后的退避窗口
====================== ========================== ============================================
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)

#: 默认键前缀：``kb:breaker:<service>``（与业务队列的键不重叠）。
DEFAULT_PREFIX = "kb:breaker"
#: 索引集合的后缀：``kb:breaker:__services__``。
_INDEX_SUFFIX = "__services__"
#: **禁止工作的库**：DB0 是共享生产业务队列（有别人的业务消息，只读也不能碰）。
FORBIDDEN_DB_INDEXES: frozenset[str] = frozenset({"0"})
#: 项目里被当作"业务库"的库（见 scripts/loadtest_runner.py）：仅用于提示，不拒绝。
BUSINESS_DB_HINT: frozenset[str] = frozenset({"0", "1"})

DEFAULT_SOCKET_TIMEOUT_SECONDS = 1.5
DEFAULT_BACKOFF_SECONDS = 5.0
DEFAULT_PROBE_TTL_SECONDS = 300.0
DEFAULT_DENY_CACHE_SECONDS = 0.5
#: 键 TTL 下限：熔断状态是**易失**的，不需要永久保存；但必须显著长于冷却时间，
#: 否则"冷却还没结束、键先过期"会让刚打开的熔断器被误当成闭合。
MIN_KEY_TTL_SECONDS = 600.0


# --------------------------------------------------------------- 工具函数 ----


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return "" if value is None else str(value)


def sanitize_url(url: str) -> str:
    """去掉 URL 里的口令（日志/快照里绝不回显凭据）。"""
    return re.sub(r"://[^/@\s]*@", "://***@", url or "")


def url_db_index(url: str) -> str | None:
    """解析 URL 里的 Redis DB 序号。

    返回：
      * 数字字符串（``"14"``）—— 解析成功；
      * ``"0"`` —— URL 没写 DB（redis-py 的默认库就是 0）→ 调用方必须**拒绝**；
      * ``None`` —— 解析不出来（非数字、unix socket 等）→ 调用方必须**拒绝**
        （不能确定是不是 DB0 时，绝不冒险）。
    """
    raw = (url or "").strip()
    if not raw:
        return None
    body = raw.split("://", 1)[-1]
    if "/" not in body:
        # redis://host:6379 → redis-py 默认 db=0
        return "0"
    path = body.split("/", 1)[1].split("?", 1)[0].strip()
    if not path:
        return "0"
    # 形如 /14/ 或 /14?x=y 都按 14 处理；别的字符一律"无法解析"
    head = path.split("/", 1)[0]
    return head if head.isdigit() else None


def _config(name: str, default: Any = None) -> Any:
    """读配置：环境变量优先，其次 Django settings（未配置 Django 时也能用）。"""
    raw = os.environ.get(name)
    if raw is not None and str(raw) != "":
        return raw
    try:
        from django.conf import settings

        value = getattr(settings, name, None)
        if value is not None and str(value) != "":
            return value
    except Exception:  # noqa: BLE001 - 没有 Django（子进程/脚本）也必须能跑
        pass
    return default


def _config_float(name: str, default: float) -> float:
    return _to_float(_config(name, default), default)


def _config_int(name: str, default: int) -> int:
    return _to_int(_config(name, default), default)


def _default_url() -> str:
    """共享状态默认用"项目里给限流用的那套 Redis 配置"。

    顺序与 :func:`apps.knowledge.ratelimit._default_url` 一致（先专用变量，
    再 Celery 专用库），这样 web/worker 不改配置就能共用同一份状态。
    """
    for name in ("KB_BREAKER_REDIS_URL", "KB_MODEL_LIMIT_REDIS_URL"):
        value = _config(name, "")
        if value:
            return str(value)
    for name in ("CELERY_BROKER_URL", "KB_REDIS_URL"):
        value = _config(name, "")
        if value:
            return str(value)
    return ""


def _new_probe_token() -> str:
    """每次探测**唯一**的令牌：带上进程/线程，便于排查"谁拿着探测权"。"""
    return f"{os.getpid()}:{threading.get_ident()}:{uuid.uuid4().hex}"


# ------------------------------------------------------------------ Lua ----
# 所有脚本都在 Redis 侧取 ``TIME``（跨进程唯一共同时间基准），并且**只**用本模块
# 前缀下的键。返回值统一是扁平数组，Python 侧按位置解析。

_FAIL_LUA = """
local key = KEYS[1]
local index = KEYS[2]
local threshold = tonumber(ARGV[1])
local cooldown = tonumber(ARGV[2])
local reason = ARGV[3]
local ttl = tonumber(ARGV[4])
local service = ARGV[5]
-- **只延长、绝不缩短** TTL：短冷却服务的写入不得把索引（或别的服务）的 TTL 砍短，
-- 否则"索引先过期、熔断状态还在"→ reset_all 会漏删（独立审查实测到 index 8000→600）。
local function expire_keep(k, seconds)
  local current = redis.call('TTL', k)
  if current < 0 or seconds > current then
    redis.call('EXPIRE', k, seconds)
  end
end
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local failures = redis.call('HINCRBY', key, 'failures', 1)
redis.call('HSET', key, 'threshold', threshold, 'cooldown', cooldown,
           'last_reason', reason, 'updated_at', now)
if failures >= threshold then
  redis.call('HSET', key, 'opened_at', now)
  redis.call('HDEL', key, 'probe_owner', 'probe_at')
end
redis.call('SADD', index, service)
expire_keep(key, ttl)
expire_keep(index, ttl)
return {failures, now}
"""

_SUCCESS_LUA = """
redis.call('DEL', KEYS[1])
redis.call('SREM', KEYS[2], ARGV[1])
return 1
"""

_RESET_LUA = """
local existed = redis.call('EXISTS', KEYS[1])
redis.call('DEL', KEYS[1])
redis.call('SREM', KEYS[2], ARGV[1])
return existed
"""

_GUARD_LUA = """
local key = KEYS[1]
local threshold = tonumber(ARGV[1])
local cooldown = tonumber(ARGV[2])
local probe_ttl = tonumber(ARGV[3])
local token = ARGV[4]
local ttl = tonumber(ARGV[5])
local function expire_keep(k, seconds)
  local current = redis.call('TTL', k)
  if current < 0 or seconds > current then
    redis.call('EXPIRE', k, seconds)
  end
end
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local data = redis.call('HGETALL', key)
if #data == 0 then
  return {1, 0, 0, 0, 0, ''}
end
local h = {}
for i = 1, #data, 2 do h[data[i]] = data[i + 1] end
local failures = tonumber(h['failures'] or '0')
local stored_threshold = tonumber(h['threshold'] or threshold)
local stored_cooldown = tonumber(h['cooldown'] or cooldown)
local opened_at = tonumber(h['opened_at'] or '0')
local last_reason = h['last_reason'] or ''
if failures < stored_threshold then
  return {1, 0, failures, 0, opened_at, last_reason}
end
local elapsed = now - opened_at
if opened_at > 0 and elapsed < stored_cooldown then
  return {0, 1, failures, stored_cooldown - elapsed, opened_at, last_reason}
end
-- 半开：探测权带租约，过期后别人可以拿走（持有者崩溃不会永久卡死）
local owner = h['probe_owner'] or ''
local probe_at = tonumber(h['probe_at'] or '0')
if owner ~= '' and (now - probe_at) < probe_ttl then
  return {0, 2, failures, 0, opened_at, last_reason}
end
redis.call('HSET', key, 'probe_owner', token, 'probe_at', now)
expire_keep(key, ttl)
return {1, 3, failures, 0, opened_at, last_reason}
"""

_READ_LUA = """
local key = KEYS[1]
local threshold = tonumber(ARGV[1])
local cooldown = tonumber(ARGV[2])
local probe_ttl = tonumber(ARGV[3])
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local data = redis.call('HGETALL', key)
if #data == 0 then
  return {0, 'closed', 0, threshold, cooldown, 0, 0, 0, '', 0}
end
local h = {}
for i = 1, #data, 2 do h[data[i]] = data[i + 1] end
local failures = tonumber(h['failures'] or '0')
local stored_threshold = tonumber(h['threshold'] or threshold)
local stored_cooldown = tonumber(h['cooldown'] or cooldown)
local opened_at = tonumber(h['opened_at'] or '0')
local last_reason = h['last_reason'] or ''
local updated_at = tonumber(h['updated_at'] or '0')
local state = 'closed'
local retry = 0
local probing = 0
if failures >= stored_threshold then
  local elapsed = now - opened_at
  if opened_at > 0 and elapsed < stored_cooldown then
    state = 'open'
    retry = stored_cooldown - elapsed
  else
    state = 'half_open'
    local owner = h['probe_owner'] or ''
    local probe_at = tonumber(h['probe_at'] or '0')
    if owner ~= '' and (now - probe_at) < probe_ttl then
      probing = 1
    end
  end
end
return {1, state, failures, stored_threshold, stored_cooldown, retry, opened_at,
        probing, last_reason, updated_at}
"""

#: 原子批量删除（键由调用方**按前缀真实扫描**得到，不依赖索引集合）。
_DELETE_KEYS_LUA = """
local deleted = 0
for i = 1, #ARGV do
  deleted = deleted + redis.call('DEL', ARGV[i])
end
return deleted
"""


class BreakerStore:
    """Redis 上的**共享熔断状态**（原子读写 + 半开探测租约）。

    设计要点：
      * ``enabled=False``（``KB_BREAKER_STORE=process``）时不做任何 Redis 操作，
        所有原语返回 ``ok=False`` 并在快照里如实说明"共享存储未启用"；
      * URL 指向 DB0 / DB 序号无法确认 → **拒绝工作**（连客户端都不建）；
      * 任何 Redis 异常都被吞掉并记录（``redis_error``），绝不冒到检索路径上；
      * 出错后进入 ``backoff_seconds`` 退避窗口，避免每个请求都白等一次连接超时。
    """

    #: 快照里对外声明的后端名（真实实现是 Redis；替身/测试可覆盖）。
    backend_name = "redis"

    def __init__(
        self,
        *,
        url: str | None = None,
        prefix: str | None = None,
        enabled: bool | None = None,
        client: Any | None = None,
        key_ttl_seconds: float | None = None,
        probe_ttl_seconds: float | None = None,
        deny_cache_seconds: float | None = None,
        socket_timeout: float | None = None,
        backoff_seconds: float | None = None,
    ) -> None:
        raw_prefix = (
            prefix
            if prefix is not None
            else str(_config("KB_BREAKER_PREFIX", DEFAULT_PREFIX) or DEFAULT_PREFIX)
        )
        # 前缀尾部冒号会被规整掉：传 "kb:test:breaker:x:" 与 "kb:test:breaker:x" 等价
        self.prefix = raw_prefix.rstrip(":") or DEFAULT_PREFIX
        self.url = sanitize_url(str(url if url is not None else _default_url() or ""))
        self._raw_url = str(url if url is not None else _default_url() or "")
        self.enabled = bool(
            enabled
            if enabled is not None
            else str(_config("KB_BREAKER_STORE", "redis") or "redis").strip().lower()
            not in ("process", "off", "none", "local", "0", "false", "no")
        )
        self.probe_ttl_seconds = max(
            1.0,
            float(
                probe_ttl_seconds
                if probe_ttl_seconds is not None
                else _config_float("KB_BREAKER_PROBE_TTL_SECONDS", DEFAULT_PROBE_TTL_SECONDS)
            ),
        )
        self.deny_cache_seconds = max(
            0.0,
            float(
                deny_cache_seconds
                if deny_cache_seconds is not None
                else _config_float("KB_BREAKER_DENY_CACHE_SECONDS", DEFAULT_DENY_CACHE_SECONDS)
            ),
        )
        self.socket_timeout = max(
            0.05,
            float(
                socket_timeout
                if socket_timeout is not None
                else _config_float(
                    "KB_BREAKER_REDIS_TIMEOUT_SECONDS", DEFAULT_SOCKET_TIMEOUT_SECONDS
                )
            ),
        )
        self.backoff_seconds = max(
            0.0,
            float(
                backoff_seconds
                if backoff_seconds is not None
                else _config_float("KB_BREAKER_REDIS_BACKOFF_SECONDS", DEFAULT_BACKOFF_SECONDS)
            ),
        )
        self._key_ttl_seconds = (
            float(key_ttl_seconds)
            if key_ttl_seconds is not None
            else float(_config_float("KB_BREAKER_KEY_TTL_SECONDS", 0.0))
        )
        #: 前缀扫描（发现状态键/孤儿键）的上限：超过就如实报"结果被截断"。
        self.max_scan_keys = max(
            100, int(_config_int("KB_BREAKER_SCAN_LIMIT", 5000))
        )
        self._explicit_client = client
        self._client: Any | None = client
        self._client_error = ""
        self._unavailable_until = 0.0
        self._lock = threading.Lock()
        self._db_index = url_db_index(self._raw_url) if self._raw_url else None
        self._db_verified = bool(self._raw_url) or client is None
        self._refusal = self._refusal_reason()
        self._disabled_reason = (
            "KB_BREAKER_STORE=process：共享存储未启用（显式使用进程内后端）"
        )

    # ------------------------------------------------------------ 边界自检 ----
    def _refusal_reason(self) -> str:
        """返回"拒绝工作"的原因；空串表示可以工作。"""
        if not self._raw_url:
            if self._explicit_client is not None:
                # 显式注入客户端的调用方（测试/嵌入式）自证库序号，这里不强判
                return ""
            return (
                "未配置共享熔断 Redis 地址（KB_BREAKER_REDIS_URL / "
                "KB_MODEL_LIMIT_REDIS_URL / CELERY_BROKER_URL 均为空）→ 拒绝工作："
                "无法确认不是 DB0"
            )
        if self._db_index is None:
            return (
                f"无法确定 Redis DB 序号（url={self.url}）→ 拒绝工作："
                "不能冒险落在 DB0（共享生产业务队列）"
            )
        if self._db_index in FORBIDDEN_DB_INDEXES:
            return (
                f"配置指向 DB{self._db_index}（共享生产业务库）→ 拒绝在 DB0 上工作"
            )
        return ""

    @property
    def db_index(self) -> str:
        return self._db_index or ""

    @property
    def refusal_reason(self) -> str:
        return self._refusal

    # -------------------------------------------------------------- 客户端 ----
    def _redis(self) -> Any | None:
        """拿 Redis 客户端；不可用/被拒绝时返回 ``None``（调用方如实降级）。"""
        if not self.enabled:
            return None
        if self._refusal:
            return None
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is not None:
                return self._client
            if time.monotonic() < self._unavailable_until:
                return None
            try:
                import redis

                client = redis.Redis.from_url(
                    self._raw_url,
                    socket_timeout=self.socket_timeout,
                    socket_connect_timeout=self.socket_timeout,
                )
                client.ping()
            except Exception as exc:  # noqa: BLE001 - 连不上就降级，不让检索失败
                self._note_error(exc)
                return None
            self._client = client
            self._client_error = ""
            return client

    def _note_error(self, exc: BaseException) -> None:
        self._client_error = f"{type(exc).__name__}: {exc}"
        self._unavailable_until = time.monotonic() + self.backoff_seconds
        # 自动建的客户端在出错后丢弃：下次退避结束时重新连接 + PING，
        # 否则一个坏掉的连接会被当成"可用"（假绿灯）。
        if self._explicit_client is None:
            self._client = None
        logger.warning("共享熔断状态不可用（本次退回进程内）：%s", self._client_error)

    def connectivity(self) -> tuple[bool, str]:
        """``(是否可用, 不可用原因)``。**只读**，不会抛异常。"""
        if not self.enabled:
            return False, self._disabled_reason
        if self._refusal:
            return False, self._refusal
        if self._redis() is None:
            return False, (self._client_error or "共享熔断 Redis 不可用")
        return True, ""

    def _eval(self, script: str, keys: list[str], args: list[Any]) -> Any | None:
        client = self._redis()
        if client is None:
            return None
        try:
            return client.eval(script, len(keys), *keys, *args)
        except Exception as exc:  # noqa: BLE001 - Redis 抖动不允许打断业务
            self._note_error(exc)
            return None

    # -------------------------------------------------------------- 键工具 ----
    def _key(self, service: str) -> str:
        return f"{self.prefix}:{service}"

    def _index_key(self) -> str:
        return f"{self.prefix}:{_INDEX_SUFFIX}"

    def _service_from_key(self, key: str) -> str:
        """把 ``<prefix>:<service>`` 还原成服务名；索引键/异前缀键返回空串。

        必须**区分状态键与索引键**：索引（``__services__``）不是熔断状态，
        不能被当成某个服务，也不能漏掉它（重置时它也要删）。
        """
        head = f"{self.prefix}:"
        if not key.startswith(head):
            return ""
        name = key[len(head) :]
        if not name or name == _INDEX_SUFFIX:
            return ""
        return name

    def _key_ttl(self, cooldown_seconds: float) -> int:
        if self._key_ttl_seconds > 0:
            return max(60, int(self._key_ttl_seconds))
        return int(
            max(
                MIN_KEY_TTL_SECONDS,
                float(cooldown_seconds) * 4.0,
                self.probe_ttl_seconds * 2.0,
            )
        )

    # ---------------------------------------------------- 写入原语（原子）----
    def try_record_failure(
        self,
        service: str,
        *,
        failure_threshold: int = 3,
        cooldown_seconds: float = 60.0,
        reason: str = "",
    ) -> dict[str, Any]:
        """记一次失败（``HINCRBY`` 原子自增；达阈值则写打开时刻并清探测权）。"""
        threshold = max(1, int(failure_threshold))
        cooldown = max(0.0, float(cooldown_seconds))
        raw = self._eval(
            _FAIL_LUA,
            [self._key(service), self._index_key()],
            [
                threshold,
                repr(cooldown),
                (reason or "")[:300],
                self._key_ttl(cooldown),
                service,
            ],
        )
        if raw is None:
            return {"ok": False, "error": self._error_text(), "consecutive_failures": None}
        failures = _to_int(raw[0])
        return {
            "ok": True,
            "error": "",
            "consecutive_failures": failures,
            "opened": failures >= threshold,
            "now": _to_float(raw[1]),
        }

    def try_record_success(self, service: str) -> dict[str, Any]:
        """记一次成功：**全局**清空该服务的熔断状态（所有进程一起恢复）。"""
        raw = self._eval(_SUCCESS_LUA, [self._key(service), self._index_key()], [service])
        if raw is None:
            return {"ok": False, "error": self._error_text()}
        return {"ok": True, "error": ""}

    def try_reset(self, service: str) -> dict[str, Any]:
        raw = self._eval(_RESET_LUA, [self._key(service), self._index_key()], [service])
        if raw is None:
            return {"ok": False, "error": self._error_text(), "existed": None}
        return {"ok": True, "error": "", "existed": _to_int(raw) == 1}

    # ---------------------------------------------------- 读取原语（原子）----
    def try_read(
        self,
        service: str,
        *,
        failure_threshold: int = 3,
        cooldown_seconds: float = 60.0,
    ) -> dict[str, Any]:
        """读共享状态：``{"ok": bool, "record": dict|None, "error": str}``。

        ``record is None`` 表示"共享存储里没有该服务的状态"（等价于闭合），
        与 ``ok=False``（读不到，调用方应退回进程内）严格区分。
        """
        raw = self._eval(
            _READ_LUA,
            [self._key(service)],
            [
                max(1, int(failure_threshold)),
                repr(max(0.0, float(cooldown_seconds))),
                repr(self.probe_ttl_seconds),
            ],
        )
        if raw is None:
            return {"ok": False, "record": None, "error": self._error_text()}
        exists = _to_int(raw[0]) == 1
        if not exists:
            return {"ok": True, "record": None, "error": ""}
        return {"ok": True, "error": "", "record": self._record_from_read(service, raw)}

    def _record_from_read(self, service: str, raw: list[Any]) -> dict[str, Any]:
        state = _to_text(raw[1]) or "closed"
        return {
            "service": service,
            "state": state,
            "consecutive_failures": _to_int(raw[2]),
            "failure_threshold": _to_int(raw[3], 3),
            "cooldown_seconds": _to_float(raw[4], 60.0),
            "retry_after_seconds": round(max(0.0, _to_float(raw[5])), 1),
            "opened_at": _to_float(raw[6]),
            "probe_in_flight": _to_int(raw[7]) == 1,
            "last_reason": _to_text(raw[8]),
            "updated_at": _to_float(raw[9]),
            "source": "redis",
            "scope": "global",
            "backend": "redis",
            "redis_available": True,
            "redis_error": "",
        }

    def try_guard(
        self,
        service: str,
        *,
        failure_threshold: int = 3,
        cooldown_seconds: float = 60.0,
    ) -> dict[str, Any]:
        """调用前检查（**原子**）：冷却中拒绝、半开时只放一个探测。

        ``allowed=False`` 时的 ``state`` 为 ``open``（冷却中）或 ``half_open``
        （已有探测在飞）；``probe_granted=True`` 表示本次拿到了探测权。
        """
        threshold = max(1, int(failure_threshold))
        cooldown = max(0.0, float(cooldown_seconds))
        token = _new_probe_token()
        raw = self._eval(
            _GUARD_LUA,
            [self._key(service)],
            [
                threshold,
                repr(cooldown),
                repr(self.probe_ttl_seconds),
                token,
                self._key_ttl(cooldown),
            ],
        )
        if raw is None:
            return {
                "ok": False,
                "error": self._error_text(),
                "allowed": None,
                "state": "",
                "retry_after_seconds": 0.0,
                "probe_granted": False,
                "consecutive_failures": None,
                "last_reason": "",
            }
        code = _to_int(raw[1])
        state = {0: "closed", 1: "open", 2: "half_open", 3: "half_open"}.get(code, "closed")
        return {
            "ok": True,
            "error": "",
            "allowed": _to_int(raw[0]) == 1,
            "state_code": code,
            "state": state,
            "consecutive_failures": _to_int(raw[2]),
            "retry_after_seconds": round(max(0.0, _to_float(raw[3])), 1),
            "probe_granted": code == 3,
            "probe_token": token if code == 3 else "",
            "last_reason": _to_text(raw[5]),
        }

    def _error_text(self) -> str:
        return self._client_error or self._refusal or self._disabled_reason or "共享熔断 Redis 不可用"

    # -------------------------------------------------------------- 快照 ----
    def global_snapshot(self) -> list[dict[str, Any]]:
        """**跨进程**全部熔断状态（Redis 不可用时返回空列表，由 scope 说明）。

        **按前缀真实扫描发现**，不只看索引集合：索引键可能丢失/过期（或被人手工删掉），
        而长冷却的熔断状态仍然有效 —— 只信索引就会漏报、也会漏删。
        """
        available, _ = self.connectivity()
        if not available:
            return []
        client = self._client
        if client is None:
            return []
        try:
            keys, _truncated = self._scan_keys()
        except Exception as exc:  # noqa: BLE001
            self._note_error(exc)
            return []
        services = sorted({name for name in (self._service_from_key(k) for k in keys) if name})
        items: list[dict[str, Any]] = []
        for service in services:
            result = self.try_read(service)
            if not result["ok"]:
                return []
            if result["record"] is None:
                # 竞态：键刚过期/被删 → 顺手剪掉索引里的残留，别让它越积越多
                try:
                    client.srem(self._index_key(), service)
                except Exception:  # noqa: BLE001 - 剪枝失败不影响读数
                    pass
                continue
            items.append(result["record"])
        return items

    def describe(self) -> dict[str, Any]:
        """只读的自述（供指标使用；不含凭据）。"""
        available, error = self.connectivity()
        return {
            "scope": "global" if available else "process",
            "backend": "redis" if available else "process",
            "enabled": self.enabled,
            "redis_available": available,
            "redis_error": error,
            "prefix": self.prefix,
            "redis_url": self.url,
            "redis_db": self.db_index,
            "db_verified": self._db_verified,
            "probe_lease_seconds": self.probe_ttl_seconds,
            "note": (
                "熔断状态放在 Redis，web/worker 共用同一份；半开探测权带租约"
                if available
                else f"共享存储不可用（{error}）→ 熔断状态退回**进程内**口径，重置也只能作用于本进程"
            ),
        }

    def snapshot(self) -> dict[str, Any]:
        """指标用快照：自述 + 全局状态列表。"""
        info = self.describe()
        items: list[dict[str, Any]] = []
        if info["redis_available"]:
            before = self._client_error
            items = self.global_snapshot()
            if self._client_error and self._client_error != before:
                # 读的过程中 Redis 挂了：如实降级为进程口径，不掩盖
                info = {
                    **info,
                    "scope": "process",
                    "backend": "process",
                    "redis_available": False,
                    "redis_error": self._client_error,
                    "note": (
                        f"读取共享状态时 Redis 报错（{self._client_error}）→ "
                        "如实降级为进程内口径"
                    ),
                }
                items = []
        info["items"] = items
        info["count"] = len(items)
        return info

    # -------------------------------------------------------------- 重置 ----
    def reset_all(self) -> dict[str, Any]:
        """**全局**重置本前缀下的所有熔断状态。

        返回结构（字段名是对外契约，planner 与界面都读它）：
          ``scope``（``global``/``process``）、``backend``、``enabled``、
          ``redis_available``（bool）、``redis_error``（str）、
          ``global_reset``（bool，**只有**共享状态真的被清空且复查无残留才为 True）、
          ``reset``（逐服务结果 list[dict]，含 ``orphan`` 标记）、``reset_count``、
          ``services``（服务名列表）、
          ``scanned``（按前缀真实扫描到的**状态键**数；不可用时为 None）、
          ``scanned_keys_total``（连同索引键在内的全部键数）、
          ``deleted``（实际删除的键数，含索引键）、
          ``orphans_found``/``orphan_services``（索引里没有、但真实存在的状态键）、
          ``index_present``（索引键当时是否存在）、``truncated``（扫描是否被上限截断）、
          ``remaining``（复查后仍在的键数）、
          ``note``（人话说明，Redis 不可用时明确写"未做全局重置"）。
        """
        available, error = self.connectivity()
        result: dict[str, Any] = {
            "scope": "global" if available else "process",
            "backend": "redis" if available else "process",
            "enabled": self.enabled,
            "redis_available": available,
            "redis_error": error,
            "global_reset": False,
            "reset": [],
            "reset_count": 0,
            "services": [],
            # 证据字段（独立审查要求）：**真实扫描**到多少键、删掉多少、发现多少孤儿
            "scanned": None,
            "scanned_keys_total": None,
            "deleted": 0,
            "orphans_found": None,
            "orphan_services": [],
            "index_present": None,
            "remaining": None,
            "truncated": False,
            "prefix": self.prefix,
            "redis_db": self.db_index,
            "at": time.time(),
        }
        if not available:
            result["note"] = (
                f"共享存储不可用（{error}）→ **未做任何全局重置**；"
                "只能重置各自进程内的状态，不能在 Redis 不可用时声称全局成功"
            )
            return result

        client = self._client
        index_key = self._index_key()
        # 1) **按前缀真实扫描**发现键（不只看索引：索引可能丢失/过期，
        #    而长冷却的熔断状态仍然有效；只信索引会漏删）。
        try:
            scanned_keys, truncated = self._scan_keys()
            indexed = {_to_text(name) for name in (client.smembers(index_key) or [])}
            index_present = bool(client.exists(index_key))
        except Exception as exc:  # noqa: BLE001 - 发现阶段失败 → 如实报告
            self._note_error(exc)
            result.update(
                {
                    "scope": "process",
                    "backend": "process",
                    "redis_available": False,
                    "redis_error": self._client_error,
                    "note": f"扫描共享状态失败（{self._client_error}）→ **未完成全局重置**",
                }
            )
            return result

        state_keys = [key for key in scanned_keys if self._service_from_key(key)]
        names = sorted({self._service_from_key(key) for key in state_keys})
        orphan_names = sorted(name for name in names if name not in indexed)
        result.update(
            {
                "scanned": len(state_keys),
                "scanned_keys_total": len(scanned_keys),
                "orphans_found": len(orphan_names),
                "orphan_services": orphan_names,
                "index_present": index_present,
                "truncated": truncated,
            }
        )

        # 2) 原子批量删除**真实发现的键**（含索引键本身）
        to_delete = list(scanned_keys)
        if index_key not in to_delete:
            to_delete.append(index_key)
        deleted = self._eval(_DELETE_KEYS_LUA, [], to_delete)
        if deleted is None:
            result.update(
                {
                    "scope": "process",
                    "backend": "process",
                    "redis_available": False,
                    "redis_error": self._client_error,
                    "note": (
                        f"批量删除脚本执行失败（{self._client_error}）→ **未完成全局重置**，"
                        "不能在失败时说全局成功"
                    ),
                }
            )
            return result

        # 3) 复查（最多两轮，捕捉并发新建的键）：还有残留就再删一轮，并如实记账
        for _round in range(2):
            remaining_keys, more_truncated = self._scan_keys()
            truncated = truncated or more_truncated
            if not remaining_keys:
                break
            extra = self._eval(_DELETE_KEYS_LUA, [], remaining_keys)
            if extra is None:
                break
            deleted += extra
            for key in remaining_keys:
                name = self._service_from_key(key)
                if name and name not in names:
                    names.append(name)
            names = sorted(names)
        remaining = self._count_prefix()

        reset_entries = [
            {
                "service": name,
                "reset": True,
                "source": "redis",
                "local": False,
                "orphan": name in orphan_names,
                "error": "",
            }
            for name in names
        ]
        result.update(
            {
                "reset": reset_entries,
                "reset_count": len(reset_entries),
                "services": names,
                "deleted": int(deleted),
                "remaining": remaining,
                "truncated": truncated,
                "global_reset": remaining == 0 and not truncated,
            }
        )
        if result["global_reset"]:
            result["note"] = (
                f"全局重置完成：按前缀 {self.prefix} **真实扫描**到 {result['scanned']} 个键，"
                f"删除 {result['deleted']} 个，清理了 {len(names)} 个服务的熔断状态"
                f"（其中索引外的孤儿 {len(orphan_names)} 个），复查无残留"
            )
        else:
            result["note"] = (
                f"共享 Redis 已执行重置，但复查仍有 {remaining} 个键未删除"
                f"{'（扫描达到上限，结果可能被截断）' if truncated else ''}"
                " → **不能**声称全局重置成功"
            )
        return result

    def _scan_keys(self) -> tuple[list[str], bool]:
        """按前缀扫描发现键（SCAN 增量扫描，不阻塞 Redis；有上限并如实报告截断）。"""
        client = self._client
        if client is None:
            return [], False
        keys: list[str] = []
        truncated = False
        for key in client.scan_iter(match=f"{self.prefix}:*", count=200):
            if len(keys) >= self.max_scan_keys:
                truncated = True
                break
            keys.append(_to_text(key))
        return keys, truncated

    def _count_prefix(self) -> int:
        keys, _truncated = self._scan_keys()
        return len(keys)


# ------------------------------------------------------- 进程内单例/便捷入口 ----


_STORE: BreakerStore | None = None
_STORE_LOCK = threading.Lock()


def build_breaker_store() -> BreakerStore:
    """按当前配置构造一个 store（每次读配置，便于测试与运行时改配置）。"""
    return BreakerStore()


def breaker_store() -> BreakerStore:
    """进程内单例（**配置**决定它是否真的用 Redis）。"""
    global _STORE
    if _STORE is None:
        with _STORE_LOCK:
            if _STORE is None:
                _STORE = build_breaker_store()
    return _STORE


def reset_breaker_store() -> None:
    """测试钩子：丢弃单例，下次按当前配置重建。"""
    global _STORE
    with _STORE_LOCK:
        _STORE = None


def global_snapshot() -> list[dict[str, Any]]:
    """跨进程全部熔断状态（Redis 不可用时返回空列表）。"""
    return breaker_store().global_snapshot()


def reset_all() -> dict[str, Any]:
    """全局重置共享熔断状态（返回结构见 :meth:`BreakerStore.reset_all`）。"""
    return breaker_store().reset_all()


def breaker_store_snapshot() -> dict[str, Any]:
    """指标用：共享熔断存储的真实口径 + 全局状态列表。"""
    return breaker_store().snapshot()
