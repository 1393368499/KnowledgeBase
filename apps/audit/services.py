"""审计服务：追加事件、校验哈希链。

并发要点（评审意见）：不能靠 ``select_for_update`` 去锁"链尾那一行"——
链尾不存在时（空表、或并发下的第一条）根本锁不住，两个写入方会各自读到
"没有上一条"，都拿 ``GENESIS_HASH`` 写一条，链就分叉了。
所以这里用一张只有一行的 ``kb_audit_chain_head`` 当**固定锁对象**：
先锁住它，再读链尾、写事件、更新链头，全部在一个事务里。
"""

from __future__ import annotations

from typing import Any

from django.db import transaction

from .models import GENESIS_HASH, AuditChainHead, AuditEvent


def append_event(
    *,
    action: str,
    actor_id: int | None = None,
    actor_label: str = "",
    resource_kind: str = "",
    resource_key: str = "",
    conversation_key: str = "",
    payload: dict[str, Any] | None = None,
    severity: str = "info",
    source: str = "api",
    request_id: str = "",
) -> AuditEvent:
    """追加一条不可修改的审计事件，并把它接到哈希链尾部。"""
    db = AuditEvent.objects.db
    with transaction.atomic(using=db):
        head = _lock_chain_head(db)
        event = AuditEvent(
            action=action,
            actor_id=actor_id,
            actor_label=actor_label or "",
            resource_kind=resource_kind or "",
            resource_key=resource_key or "",
            conversation_key=conversation_key or "",
            payload=payload or {},
            severity=severity,
            source=source,
            request_id=request_id,
            prev_hash=last_hash(using=db),
        )
        event.save(using=db)
        if head is not None:
            AuditChainHead.objects.using(db).filter(pk=head.pk).update(last_event=event)
    return event


def _lock_chain_head(db: str) -> AuditChainHead | None:
    """锁住固定链头行；第一次追加时先把链头行建出来再锁。"""
    queryset = AuditChainHead.objects.using(db).select_for_update()
    head = queryset.filter(pk=AuditChainHead.SINGLETON_ID).first()
    if head is not None:
        return head
    # 并发下两个写入方都可能走到这里：get_or_create 由主键唯一约束兜底，
    # 抢输的一方拿到已有行，再走一次 select_for_update 拿锁。
    AuditChainHead.objects.using(db).get_or_create(pk=AuditChainHead.SINGLETON_ID)
    return (
        AuditChainHead.objects.using(db)
        .select_for_update()
        .filter(pk=AuditChainHead.SINGLETON_ID)
        .first()
    )


def last_hash(*, using: str | None = None) -> str:
    """当前链尾哈希。链头表存在时以它为准，否则回退按 id 排序取最后一条。"""
    db = using or AuditEvent.objects.db
    head = AuditChainHead.objects.using(db).filter(pk=AuditChainHead.SINGLETON_ID).first()
    if head is not None and head.last_event_id is not None:
        entry = (
            AuditEvent.objects.using(db)
            .filter(pk=head.last_event_id)
            .only("entry_hash")
            .first()
        )
        if entry is not None:
            return entry.entry_hash
    tail = AuditEvent.objects.using(db).order_by("-id").only("entry_hash").first()
    return tail.entry_hash if tail is not None else GENESIS_HASH


def verify_chain(*, limit: int | None = None) -> dict[str, Any]:
    """重算哈希链，返回校验报告。

    **流式遍历**：审计表只增不减，`list(queryset)` 会把整条链一次性读进内存
    （百万级事件就是几百 MB），一旦爆掉就等于"校验功能不可用"。这里按
    ``iterator(chunk_size=…)`` 走游标；``limit`` 在 SQL 层截断，不再全量取回再切片。
    """
    queryset = AuditEvent.objects.order_by("id")
    if limit is not None:
        queryset = queryset[:limit]
    checked = 0
    prev = GENESIS_HASH
    broken_at = None
    first_reason = ""
    mismatches = []
    mismatch_count = hash_mismatch_count = link_mismatch_count = 0
    for event in queryset.iterator(chunk_size=500):
        checked += 1
        reasons = []
        if event.prev_hash != prev:
            reasons.append("prev_hash 与前一条的 entry_hash 不一致")
            link_mismatch_count += 1
        recomputed = event.compute_entry_hash()
        if recomputed != event.entry_hash:
            reasons.append("entry_hash 与重算结果不一致")
            hash_mismatch_count += 1
        if reasons:
            mismatch_count += 1
            if broken_at is None:
                broken_at, first_reason = event.pk, reasons[0]
            if len(mismatches) < 50:
                mismatches.append({"id": event.pk, "reasons": reasons})
        prev = event.entry_hash
    return {
        "ok": mismatch_count == 0, "checked": checked, "broken_at": broken_at,
        "reason": first_reason, "mismatch_count": mismatch_count,
        "hash_mismatch_count": hash_mismatch_count, "link_mismatch_count": link_mismatch_count,
        "mismatches": mismatches, "details_truncated": mismatch_count > len(mismatches),
        "scope": "prefix" if limit is not None else "full",
    }


def emit(**kwargs: Any) -> AuditEvent:
    """``append_event`` 的别名，供治理层等内部调用点使用（语义更短）。"""
    return append_event(**kwargs)
