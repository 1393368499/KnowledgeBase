"""受保护资源登记表 + ORM 层不可篡改守卫。

设计目标（需求 7）："AI 权限最大，但不能改已入库信息"。这里实现的是
**不依赖 HTTP 层** 的那一道：只要走 Django ORM，无论调用方是谁都改不动。

覆盖的绕过路径（每条都有测试）：

============================  ==========================================
绕过手段                      拦截点
============================  ==========================================
``obj.save()``（已存在）      :func:`guard_instance_save`
``Model(pk=..., **fields).save()``（``_state.adding`` 为真但库里已有该 pk）
                              :func:`guard_instance_save` 回查数据库
``QuerySet.update()``         :class:`ImmutableQuerySet`
``QuerySet.delete()``         :class:`ImmutableQuerySet`
``QuerySet.bulk_update()``    :class:`ImmutableQuerySet`
``QuerySet.bulk_create()``    :meth:`ImmutableQuerySet.bulk_create`
``bulk_create(update_conflicts=True)``
                              同上（``conflict_fields`` 即主键，等于 UPSERT 改写）
``update_or_create()``        ``save(update_fields=...)`` → 实例守卫
``get_or_create()``（已存在） 只读路径，不改写
``KnowledgeLibrary.delete()`` → CASCADE / PROTECT
                              受保护表自身的 ``delete()`` 先被拦截
``cursor.execute("UPDATE …")``
                              P1 的数据库触发器（见 ``docs/`` 与 migration 0002/0003）
============================  ==========================================

关于 ``_state.adding``：**它不是"不存在证据"**。``Model(pk=5)` 构造出来的实例
``_state.adding`` 为真，但 Django 会按已存在处理并发出 UPDATE。所以这里以
"数据库里是否已有该主键"为准。
"""

from __future__ import annotations

import contextvars
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Iterable

from django.db import models

from .immutable import (
    PARTIAL_DELETE_ACTIONS,
    ImmutableRecordError,
    GuardedOperationError,
)

# 当前线程/协程是否运行在受控特权通道里（受控删除专用）。
_PRIVILEGED: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "immutable_privileged", default=None
)


def _privileged_reason() -> str | None:
    context = _PRIVILEGED.get()
    return None if context is None else context.get("reason", "")


def current_privilege() -> dict[str, str] | None:
    """给需要写审计事件的调用方读取（例如受控删除服务）。"""
    context = _PRIVILEGED.get()
    return dict(context) if context else None


@contextmanager
def privileged_channel(*, action: str, reason: str, actor_id: int | None = None) -> Iterator[dict[str, str]]:
    """受控特权通道：唯一允许删除受保护记录的入口。

    只应该被"三次确认 + 一次性凭证"的服务层打开，且每次打开都必须留审计事件
    （由调用方负责，见 ``apps/knowledge/deletion.py``）。这里额外要求传入
    ``action`` 必须来自白名单，避免被当成通用后门。
    """
    if action not in PARTIAL_DELETE_ACTIONS:
        raise GuardedOperationError(f"未知的受控操作：{action}")
    if not reason or len(reason.strip()) < 4:
        raise GuardedOperationError("受控通道必须给出可读的操作理由")
    token = uuid.uuid4().hex
    payload = {
        "action": action,
        "reason": reason.strip(),
        "actor_id": "" if actor_id is None else str(actor_id),
        "token": token,
    }
    reset = _PRIVILEGED.set(payload)
    try:
        yield payload
    finally:
        _PRIVILEGED.reset(reset)


# ---------------------------------------------------------------- 模型登记 ----
# 语义：
#   "append_only" —— 只允许新建，已有行不允许 update/delete（审计链、入库资料）
#   "append_only_children" —— 派生数据，同样只允许新建
APPEND_ONLY_MODELS: frozenset[str] = frozenset(
    {
        "knowledge.IngestedFile",
        "knowledge.DocumentChunk",
        "audit.AuditEvent",
    }
)

# 这些字段允许在 append-only 模型上通过受控领域方法变更（仍然每次写审计事件）。
# 该表是"受控通道能写什么"的**唯一真源**：模型方法、数据库触发器、测试都读它。
CONTROLLED_MUTABLE_FIELDS: dict[str, frozenset[str]] = {
    "knowledge.IngestedFile": frozenset(
        {
            "parse_status",
            "parse_error",
            "parse_attempts",
            "chunk_count",
            "embedded_chunk_count",
            "vector_collection",
            "vector_collection_name",
            "vector_point_id",
            "vector_generation",
            "vector_status",
            "parsed_at",
            # 原始字节的落盘位置与字节数：入库时（含 P1 老记录补落盘）通过受控
            # 通道写入，仍受触发器保护——这俩字段不是"内容"，不构成篡改。
            "raw_path",
            "raw_size_bytes",
            # 处理租约三件套：它们的 UPDATE 必须放行（触发器按"受控字段之外全锁"
            # 生成），所以必须在受控表里；但租约不是内容字段，不影响不可篡改性。
            "parse_lease_token",
            "parse_lease_started_at",
            "parse_lease_expires_at",
            # 资料性质（2026-10-16 用户要求）：它是**元数据**，不是内容 —— 原件字节
            # 与派生分段一个都不动。放开的理由与代价都写在这里：
            #   * 写入路径唯一：planner 的 `classify_file` 受控操作（库管理员 +
            #     一次确认 + 审计事件 knowledge.file_classified）；
            #   * 数据库侧同步放开：migration 0018 用当前受控表重刷触发器，
            #     否则会出现"ORM 放行、触发器仍拒绝"的漂移；
            #   * 内容字段（original_name / content_sha256 / directory_path …）
            #     照旧锁死，不可篡改的核心保证没有被削弱。
            "classification",
            # 性质的两个伴随元数据（2026-10-16 自动识别）：`classification_source`
            # （""/auto_rule/auto_model/manual）与 `classification_note`（依据或失败
            # 原因）。它们必须与 `classification` 在**同一次受控写入**里落库，
            # 否则"值来自人工、来源却是规则"这类错账无法避免。同属元数据，
            # 原件字节、分段与向量一个都不动。
            "classification_source",
            "classification_note",
        }
    ),
}

# 数据库触发器要挡住的"核心"列：即使走原始 SQL 也不允许改。
# 与 CONTROLLED_MUTABLE_FIELDS 的补集关系：除受控字段外全部锁定。
TRIGGER_PROTECTED_TABLES: frozenset[str] = frozenset(
    {"kb_ingested_file", "kb_document_chunk", "kb_audit_event"}
)

# ------------------------------------------------------------ HTTP 层登记 ----
# key 是 URL 前缀（relative to 站点根），value 是人类可读的资源名。
PROTECTED_API_PREFIXES: dict[str, str] = {
    "/api/v1/knowledge/files": "已入库资料",
    "/api/v1/audit/events": "审计事件",
}
# 精确匹配的路径（例如未来新增的受保护单对象接口）
PROTECTED_API_EXACT: dict[str, str] = {}

PROTECTED_HTTP_METHODS: frozenset[str] = frozenset({"PUT", "PATCH", "DELETE"})


def protected_resource_for(path: str) -> str | None:
    """返回该路径受保护时的资源名，否则 ``None``。"""
    normalized = path if path.endswith("/") else f"{path}/"
    for prefix, label in PROTECTED_API_PREFIXES.items():
        prefix_norm = prefix if prefix.endswith("/") else f"{prefix}/"
        if normalized == prefix_norm or normalized.startswith(prefix_norm):
            return label
    for exact, label in PROTECTED_API_EXACT.items():
        if path.rstrip("/") == exact.rstrip("/"):
            return label
    return None


def is_protected_api_path(path: str) -> bool:
    return protected_resource_for(path) is not None


# --------------------------------------------------------------- ORM 守卫 ----
def allowed_fields_for(model_label: str) -> frozenset[str]:
    """受控通道在该模型上允许写的字段。"""
    return CONTROLLED_MUTABLE_FIELDS.get(model_label, frozenset())


def is_protected_model(model_label: str) -> bool:
    return model_label in APPEND_ONLY_MODELS


def _row_exists(instance: models.Model) -> bool:
    """该主键在数据库里是否已经有行（不看 ``_state.adding``）。"""
    pk = instance.pk
    if pk is None:
        return False
    model = type(instance)
    return model._meta.base_manager.filter(pk=pk).exists()


def guard_instance_save(
    instance: models.Model,
    *,
    allow_existing_fields: Iterable[str] = (),
    _controlled_write: bool = False,
) -> None:
    """已存在的受保护实例不允许通过 ``save()`` 落库。

    ``_controlled_write=True`` 只由 :func:`controlled_save` 内部的领域方法使用；
    普通调用方即使传了也会被 :func:`controlled_save` 的字段白名单兜住。
    """
    model_label = instance._meta.label
    if not is_protected_model(model_label):
        return
    if not _row_exists(instance):
        return  # 真正的新增放行
    if _controlled_write or set(allow_existing_fields):
        return
    raise ImmutableRecordError(
        model_label,
        "修改（save）",
        "已入库记录只能追加新版本，不能原地改写；状态流转请使用领域方法",
    )


def guard_instance_delete(instance: models.Model) -> None:
    """已存在的受保护实例不允许删除。

    唯一例外是 :func:`privileged_channel`（走完三次确认的受控删除通道）。
    """
    model_label = instance._meta.label
    if not is_protected_model(model_label):
        return
    if _privileged_reason() is not None:
        return
    raise ImmutableRecordError(
        model_label,
        "删除（delete）",
        "受保护记录不可直接删除；如需删除必须走三次确认 + 一次性凭证的受控通道",
    )


def controlled_save(
    instance: models.Model,
    *,
    fields: Iterable[str],
) -> None:
    """受控通道专用的写入口：只允许改登记表里声明的字段。

    这是"允许解析状态更新须限字段"的落地点：调用方给出想改的字段，
    这里逐项比对登记表，越界即拒绝；再走 ``_base_manager`` 的 UPDATE，
    绕开 append-only 的实例守卫。
    """
    model_label = instance._meta.label
    allowed = allowed_fields_for(model_label)
    wanted = set(fields)
    illegal = wanted - allowed
    if illegal:
        raise ImmutableRecordError(
            model_label,
            "受控更新",
            f"不允许通过受控通道写 {sorted(illegal)}；允许：{sorted(allowed)}",
        )
    if not _row_exists(instance):
        raise ImmutableRecordError(
            model_label, "受控更新", "受控通道只能更新已存在的行，不能新建"
        )
    updates = {name: getattr(instance, name) for name in wanted}
    type(instance)._meta.base_manager.filter(pk=instance.pk).update(**updates)


class ImmutableQuerySet(models.QuerySet):
    """彻底封死批量改删，避免绕过实例级守卫。"""

    def update(self, **kwargs):  # type: ignore[override]
        raise ImmutableRecordError(
            self.model._meta.label, "批量修改（queryset.update）", "受保护表的批量更新已禁用"
        )

    def delete(self):  # type: ignore[override]
        raise ImmutableRecordError(
            self.model._meta.label, "批量删除（queryset.delete）", "受保护表的批量删除已禁用"
        )

    def bulk_update(self, objs: Iterable[models.Model], fields, batch_size=None):  # type: ignore[override]
        raise ImmutableRecordError(
            self.model._meta.label, "批量修改（bulk_update）", "受保护表的批量更新已禁用"
        )

    def bulk_create(self, objs, *args, **kwargs):  # type: ignore[override]
        """只允许纯新增。

        ``update_conflicts=True`` 走的就是 UPSERT，会把已有行原地改写，
        等价于 ``UPDATE``，必须拒绝；``conflict_fields`` 显式给了也要拒绝。
        """
        if kwargs.get("update_conflicts") or kwargs.get("conflict_fields"):
            raise ImmutableRecordError(
                self.model._meta.label,
                "批量写入（bulk_create update_conflicts）",
                "UPSERT 会原地改写已有行，受保护表禁止",
            )
        rows = list(objs)
        existing = [row for row in rows if _row_exists(row)]
        if existing:
            raise ImmutableRecordError(
                self.model._meta.label,
                "批量写入（bulk_create）",
                f"{len(existing)} 行的主键在库中已存在，bulk_create 可能改写它们",
            )
        return super().bulk_create(rows, *args, **kwargs)

    def update_or_create(self, *args, **kwargs):  # type: ignore[override]
        """``update_or_create`` 在命中已有行时会走 ``save(update_fields=…)``。

        对受保护模型来说，"更新"分支本身就是越界，这里直接拒掉，
        避免依赖 Django 内部实现细节。
        """
        raise ImmutableRecordError(
            self.model._meta.label,
            "更新或创建（update_or_create）",
            "受保护表不允许更新分支，只能追加",
        )


class ImmutableManager(models.Manager.from_queryset(ImmutableQuerySet)):  # type: ignore[misc]
    """受保护模型的默认管理器。"""
