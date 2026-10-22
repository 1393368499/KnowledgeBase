"""审计事件：只追加的哈希链。

每条事件的 ``entry_hash`` 把前一条的哈希串进来，形成可校验的链。
任何就地修改都会让 :func:`apps.audit.services.verify_chain` 失败——
这是"不可篡改底层"在审计侧的证据，而不是承诺。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC as _UTC

from django.conf import settings
from django.db import models
from django.utils import timezone

from governance.registry import ImmutableManager

GENESIS_HASH = "0" * 64


class AuditEvent(models.Model):
    class Severity(models.TextChoices):
        INFO = "info", "信息"
        WARNING = "warning", "警告"
        CRITICAL = "critical", "严重"

    created_at = models.DateTimeField(default=timezone.now, editable=False, db_index=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="audit_events",
        null=True,
        blank=True,
    )
    actor_label = models.CharField(max_length=150, blank=True)
    action = models.CharField(max_length=80, db_index=True)
    severity = models.CharField(max_length=16, choices=Severity.choices, default=Severity.INFO)
    resource_kind = models.CharField(max_length=60, blank=True)
    resource_key = models.CharField(max_length=200, blank=True)
    conversation_key = models.CharField(max_length=64, blank=True)
    summary = models.CharField(max_length=500, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    source = models.CharField(max_length=32, default="api")
    request_id = models.CharField(max_length=64, blank=True, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    prev_hash = models.CharField(max_length=64, default=GENESIS_HASH)
    entry_hash = models.CharField(max_length=64, default="", db_index=True)

    objects = ImmutableManager()

    class Meta:
        ordering = ("-id",)
        indexes = [
            models.Index(fields=["action", "created_at"]),
            models.Index(fields=["resource_kind", "resource_key"]),
        ]

    def __str__(self) -> str:
        return f"{self.created_at:%Y-%m-%d %H:%M:%S} {self.action} {self.resource_key}"

    # ------------------------------------------------------------- 哈希链 ----
    def canonical_payload(self) -> str:
        """稳定序列化：键排序、紧凑分隔符、时间转 UTC，保证跨进程跨数据库可重算。"""
        created = ""
        if self.created_at is not None:
            value = self.created_at
            if timezone.is_aware(value):
                value = value.astimezone(_UTC)
            created = value.isoformat()
        return json.dumps(
            {
                "action": self.action,
                "actor_id": self.actor_id,
                "actor_label": self.actor_label,
                "conversation_key": self.conversation_key,
                "payload": self.payload,
                "resource_key": self.resource_key,
                "resource_kind": self.resource_kind,
                "severity": self.severity,
                "source": self.source,
                "summary": self.summary,
                "created_at": created,
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def compute_entry_hash(self) -> str:
        material = f"{self.prev_hash}|{self.canonical_payload()}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def save(self, *args, **kwargs):
        # 不许复用主键：AuditEvent(pk=<已存在>).save() 会走 UPDATE，
        # 而 _state.adding 在这种构造下是真的，不能当"不存在"的证据。
        if self.pk is not None and AuditEvent.objects.filter(pk=self.pk).exists():
            raise RuntimeError("审计事件不可修改（append-only）：主键已存在，拒绝改写")
        if not self.entry_hash:
            # created_at 参与哈希，必须在算哈希之前定下来（不依赖 auto_now_add 的时序）
            if self.created_at is None:
                self.created_at = timezone.now()
            self.entry_hash = self.compute_entry_hash()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuntimeError("审计事件不可删除（append-only）")


class AuditChainHead(models.Model):
    """审计链的**固定链头**。

    为什么要单独一张表：``select_for_update`` 锁不住"还不存在的行"。
    如果链尾为空（空表、或并发下的第一条），两个写入方都读到"没有上一条"，
    各自拿 GENESIS_HASH 写一条，链就分叉了。这里用一张只有一行的表当锁对象：
    ``get_or_create`` + ``select_for_update`` 保证所有追加方排队。
    """

    SINGLETON_ID = 1

    id = models.PositiveSmallIntegerField(primary_key=True, default=SINGLETON_ID)
    last_event = models.ForeignKey(
        AuditEvent, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "kb_audit_chain_head"
        verbose_name = "审计链头"
        verbose_name_plural = "审计链头"

    def __str__(self) -> str:  # pragma: no cover
        return f"chain-head#{self.pk} last={self.last_event_id}"
