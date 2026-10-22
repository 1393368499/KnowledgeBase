"""审计事件后台：只读（Django admin 的删除/修改动作已被模型层拦死）。"""

from __future__ import annotations

from django.contrib import admin

from .models import AuditEvent


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("id", "created_at", "action", "severity", "actor_label", "resource_kind", "resource_key")
    list_filter = ("action", "severity", "resource_kind")
    search_fields = ("action", "resource_key", "actor_label", "conversation_key", "summary")
    ordering = ("-created_at",)

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False
