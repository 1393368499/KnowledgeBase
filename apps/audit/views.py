"""审计事件读接口：只读，改删由治理层拦截。

**权限口径（Codex 独立审查发现的高危越权）**：这两个端点原先只要求"已登录"，
而查询是 ``AuditEvent.objects.all()`` —— 任何登录用户都能读全库审计事件
（含 payload / actor / resource_key）并触发整条哈希链重算；而 Agent 控制台的
审计卡却按 ``status===403`` 假设"只有系统管理员能看"，那段 403 分支其实是死代码。
现在两个端点都要求**系统管理员**（``system:read`` 口径，与 planner 的
``list_audit`` 操作一致），前端的分支才与后端真实行为对得上。
"""

from __future__ import annotations

from rest_framework import generics, permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import AuditEvent
from .serializers import AuditEventSerializer
from .services import verify_chain


class IsSystemAdminForAudit(permissions.BasePermission):
    """审计读取必须系统管理员（fail-closed）。"""

    message = "只有系统管理员可以查看审计事件"

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False
        from apps.knowledge.permissions import is_system_admin

        return bool(is_system_admin(user))


class AuditEventListView(generics.ListAPIView):
    serializer_class = AuditEventSerializer
    permission_classes = [IsSystemAdminForAudit]

    def get_queryset(self):
        queryset = AuditEvent.objects.all()
        action = self.request.query_params.get("action")
        resource_key = self.request.query_params.get("resource_key")
        conversation_key = self.request.query_params.get("conversation_key")
        if action:
            queryset = queryset.filter(action=action)
        if resource_key:
            queryset = queryset.filter(resource_key=resource_key)
        if conversation_key:
            queryset = queryset.filter(conversation_key=conversation_key)
        return queryset


class AuditChainVerifyView(APIView):
    """重算哈希链，用于证明审计记录没被改动过。"""

    permission_classes = [IsSystemAdminForAudit]

    def get(self, request):
        # limit 必须**校验**成有界正整数：旧实现直接 int(limit)，输入 abc 或 -1 会
        # 变成 500（Codex 审查点名）。这里给明确的 400，不把参数错误说成服务故障。
        raw = request.query_params.get("limit")
        limit = None
        if raw not in (None, ""):
            try:
                limit = int(str(raw).strip())
            except (TypeError, ValueError):
                return Response({"detail": "limit 必须是正整数", "code": "invalid_limit"}, status=400)
            if limit <= 0 or limit > 100_000:
                return Response(
                    {"detail": "limit 必须在 1…100000 之间", "code": "invalid_limit"},
                    status=400,
                )
        result = verify_chain(limit=limit)
        return Response(result)
