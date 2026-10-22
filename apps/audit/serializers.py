"""审计事件只读 API。"""

from __future__ import annotations

from rest_framework import serializers


class AuditEventSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)
    action = serializers.CharField(read_only=True)
    actor_id = serializers.IntegerField(read_only=True, allow_null=True)
    actor_label = serializers.CharField(read_only=True)
    resource_kind = serializers.CharField(read_only=True)
    resource_key = serializers.CharField(read_only=True)
    conversation_key = serializers.CharField(read_only=True)
    payload = serializers.JSONField(read_only=True)
    prev_hash = serializers.CharField(read_only=True)
    entry_hash = serializers.CharField(read_only=True)
    trace_id = serializers.UUIDField(read_only=True)
