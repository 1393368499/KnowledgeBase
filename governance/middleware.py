"""HTTP 层不可篡改守卫。

即使某个视图将来"忘记"了权限声明，只要路径命中治理登记表，
``PUT`` / ``PATCH`` / ``DELETE`` 也会在进入视图之前被拒绝，并留下一审计事件。
"""

from __future__ import annotations

import json
import logging
import time

from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse, JsonResponse

from .registry import PROTECTED_HTTP_METHODS, protected_resource_for

logger = logging.getLogger("governance")


class ImmutabilityGuardMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        label = protected_resource_for(request.path)
        if label is not None and request.method.upper() in PROTECTED_HTTP_METHODS:
            self._record(request, label)
            return JsonResponse(
                {
                    "detail": f"{label}不可修改或删除（后端强制约束）",
                    "code": "immutable_resource",
                    "resource": label,
                    "method": request.method.upper(),
                    "path": request.path,
                },
                status=403,
                json_dumps_params={"ensure_ascii": False},
            )
        return self.get_response(request)

    @staticmethod
    def _record(request: HttpRequest, label: str) -> None:
        """把拒绝动作写进审计链；审计自身故障不能影响拒绝语义。"""
        logger.warning(
            "immutable resource blocked: %s %s (%s)", request.method, request.path, label
        )
        try:
            from apps.audit.services import emit

            user = getattr(request, "user", None)
            emit(
                action="immutable.write_blocked",
                actor_id=getattr(user, "pk", None),
                actor_label=getattr(user, "get_username", lambda: "anonymous")(),
                resource_kind="http_path",
                resource_key=request.path,
                payload={
                    "method": request.method.upper(),
                    "resource": label,
                    "query": request.META.get("QUERY_STRING", ""),
                },
            )
        except Exception:  # noqa: BLE001 - 审计失败不得改变拒绝结果
            logger.exception("failed to append audit event for blocked request")
        _ = json  # 保持导入语义清晰：payload 已由 json 字段落库


# --------------------------------------------------------------------- 登录限速 ----
#: 需要限速的登录入口（浏览器登录页 + Django admin 登录页）。
LOGIN_THROTTLE_PATHS = frozenset({"/app/login/", "/admin/login/"})
#: 计数键前缀（放进项目 cache：生产是 Redis，跨进程共享；单机开发是进程内）。
LOGIN_THROTTLE_KEY = "kb:login-throttle:{ip}:{username}"


def _client_ip(request: HttpRequest) -> str:
    # 只信任代理注入的头当且仅当项目显式声明在代理之后；否则用直连地址。
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded and getattr(settings, "KB_TRUST_PROXY_HEADERS", False):
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "") or "unknown"


def _throttle_key(request: HttpRequest, username: str) -> str:
    return LOGIN_THROTTLE_KEY.format(ip=_client_ip(request), username=username.lower()[:150])


def _load_state(key: str) -> dict:
    state = cache.get(key)
    return state if isinstance(state, dict) else {"fails": 0, "lock_until": 0.0}


def _lock_remaining(key: str) -> int:
    state = _load_state(key)
    remaining = float(state.get("lock_until", 0.0)) - time.time()
    return max(0, int(remaining) + (1 if remaining > int(remaining) else 0))


def _record_failure(key: str, attempts: int, lockout: int) -> None:
    state = _load_state(key)
    state["fails"] = int(state.get("fails", 0)) + 1
    if state["fails"] >= attempts:
        # 达到阈值：锁定一段时间，并把计数清零，避免锁定期内继续累加。
        state["lock_until"] = time.time() + lockout
        state["fails"] = 0
    cache.set(key, state, timeout=lockout + 60)


class LoginThrottleMiddleware:
    """登录失败限速（``/app/login/`` 与 ``/admin/login/`` 共用口径）。

    F-10（2026-11-11 复审）：认证入口此前无失败锁定，可被在线爆破。
    计数键 = 客户端 IP + 用户名；达到 ``KB_LOGIN_MAX_ATTEMPTS`` 后锁定
    ``KB_LOGIN_LOCKOUT_SECONDS``，成功登录清零。计数走 Django cache：
    生产 ``USE_REDIS_CACHE=1`` 时跨进程共享，单机开发退化为进程内（够用）。
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.method.upper() != "POST" or request.path not in LOGIN_THROTTLE_PATHS:
            return self.get_response(request)
        username = (request.POST.get("username") or "").strip()
        if not username:
            return self.get_response(request)
        attempts = int(getattr(settings, "KB_LOGIN_MAX_ATTEMPTS", 5) or 5)
        lockout = int(getattr(settings, "KB_LOGIN_LOCKOUT_SECONDS", 900) or 900)
        key = _throttle_key(request, username)
        remaining = _lock_remaining(key)
        if remaining:
            logger.warning("login throttled: ip=%s user=%s", _client_ip(request), username)
            response = HttpResponse(
                f"登录尝试过于频繁，请在 {remaining} 秒后再试。",
                status=429,
                content_type="text/plain; charset=utf-8",
            )
            response["Retry-After"] = str(remaining)
            return response
        response = self.get_response(request)
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            cache.delete(key)
        elif response.status_code in (200, 401, 403):
            _record_failure(key, attempts, lockout)
        return response
