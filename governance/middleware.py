"""HTTP 层不可篡改守卫。

即使某个视图将来"忘记"了权限声明，只要路径命中治理登记表，
``PUT`` / ``PATCH`` / ``DELETE`` 也会在进入视图之前被拒绝，并留下一审计事件。
"""

from __future__ import annotations

import json
import logging

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
