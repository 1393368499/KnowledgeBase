"""库级写入闸门：排他冻结、冲突拒绝与 fencing。

**本模块的纪律（增量审查后定稿，任何调用点都不得绕过）**

1. **锁在同一个事务里**：所有会写数据/指针/租约的入口，必须在**自己那个外层
   ``transaction.atomic()``** 里先调用 :func:`assert_writable_locked`（它对闸门行做
   ``select_for_update``）—— 短检查各自开事务、检查完即释放，等于退回 TOCTOU。
2. **冻结与租约检查同锁**：:func:`freeze` 在**持有闸门行锁的同一事务内**检查活跃租约，
   否则"先查租约、再冻结"之间仍可能有新租约插入。
3. **token 只旋转、永不清空（防 ABA）**：闸门初建即给非空 token；``freeze`` 每次旋转；
   ``unfreeze`` **只清冻结状态、并再旋转一个新 token** —— 这样**旧 freeze owner 的令牌**
   与**旧普通 writer 的令牌**都永久失效（"保留同一个 token"会让解除后旧 job 仍能通过
   ``assert_fence``）。比较 token 时按"**是否提供**"判断（``None`` = 未提供），
   **不能**用真值判断，否则空串旧 token 会跳过校验。
4. **fencing**：:func:`assert_fence` 用于"远端写入之后"复核（例如 Qdrant upsert 之后），
   令牌已被接管/旋转 → 抛错，绝不把结果当作有效写入。
5. **锁序固定**：``gate → library``（激活/切换按此顺序），避免与其它路径交叉死锁；
   **模型 IO（秒级~分钟级）绝不放进持有闸门行锁的事务里**。
"""

from __future__ import annotations

import uuid

from django.db import transaction
from django.utils import timezone

from .models import IngestedFile, LibraryWriteGate


class WriteFrozen(RuntimeError):
    """本库正处于排他冻结（重建/切换窗口），写入被显式拒绝。"""

    def __init__(self, message: str, *, reason: str = "", frozen_by: str = "") -> None:
        super().__init__(message)
        self.reason = reason
        self.frozen_by = frozen_by


def _require_atomic(where: str) -> None:
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError(
            f"{where} 必须在调用方的外层 transaction.atomic() 内调用："
            "否则闸门行锁会被立即释放，写路径重新暴露在 TOCTOU 之下"
        )


def gate_for(library_id: int) -> LibraryWriteGate:
    """取得（必要时创建）本库闸门行；**初建即给非空 token**（防 ABA）。"""
    gate, created = LibraryWriteGate.objects.get_or_create(
        library_id=library_id,
        defaults={"fence_token": uuid.uuid4().hex},
    )
    if created or not (gate.fence_token or ""):
        LibraryWriteGate.objects.filter(library_id=library_id).update(
            fence_token=gate.fence_token or uuid.uuid4().hex
        )
        gate.refresh_from_db()
    return gate


def lock_gate_for_update(library_id: int) -> LibraryWriteGate:
    """在**当前外层事务**内锁闸门行并返回它（锁序：gate → library）。"""
    _require_atomic("lock_gate_for_update()")
    gate_for(library_id)
    return LibraryWriteGate.objects.select_for_update().get(library_id=library_id)


def assert_writable_locked(
    library_id: int,
    *,
    fence_token: str | None = None,
    action: str = "写入",
) -> str:
    """**唯一正确的写闸门**：必须在调用方外层事务内；锁闸门行后校验。

    ``fence_token`` 语义：``None`` = 调用方未持有令牌（普通写路径）；
    传了字符串（含空串）就**必须**与当前令牌完全相等，否则拒绝（fencing）。
    """
    gate = lock_gate_for_update(library_id)
    if gate.frozen_at is not None:
        raise WriteFrozen(
            f"信息库 #{library_id} 正在被冻结（{action} 被拒绝）："
            f"{gate.freeze_reason or '未注明原因'}（by {gate.frozen_by or '未知'}）",
            reason=gate.freeze_reason,
            frozen_by=gate.frozen_by,
        )
    if fence_token is not None and fence_token != (gate.fence_token or ""):
        raise WriteFrozen(
            f"信息库 #{library_id} 的冻结令牌已变化（{action} 被拒绝）："
            "调用方持有的是失效许可（fencing）",
            reason="fence_token 不匹配",
            frozen_by=gate.frozen_by,
        )
    return gate.fence_token or ""


def assert_writable(library_id: int, *, fence_token: str | None = None, action: str = "写入") -> str:
    """便捷包装：**仅供"入口第一句"使用**。

    它自己开事务只是为了让"锁行 + 校验"成为原子；调用方随后的实际写入**必须**
    仍在**同一个外层** ``atomic()`` 里（否则锁在这里就释放了）。真正的写路径
    应当直接调用 :func:`assert_writable_locked`。
    """
    with transaction.atomic():
        return assert_writable_locked(library_id, fence_token=fence_token, action=action)


def current_token(library_id: int) -> str:
    """只读当前令牌（不加锁；用于把令牌交给后台 job 携带）。"""
    return (gate_for(library_id).fence_token or "")


def assert_fence(library_id: int, fence_token: str | None, *, action: str = "远端写入后复核") -> None:
    """**远端写入之后**复核令牌：不一致 = 本 job 已被接管，结果作废。

    加锁读取，保证读到的是提交后的最新令牌；不写任何数据。
    """
    with transaction.atomic():
        gate = lock_gate_for_update(library_id)
        if fence_token is not None and fence_token != (gate.fence_token or ""):
            raise WriteFrozen(
                f"信息库 #{library_id} 在 {action} 时令牌已变化：本 job 的写入不允许生效",
                reason="fence_token 不匹配",
                frozen_by=gate.frozen_by,
            )


def assert_freeze_owner(library_id: int, fence_token: str | None, *, action: str = "发布代次切换") -> None:
    """校验"我**仍然是当前冻结的所有者**"：闸门仍在冻结，且令牌与我持有的一致。

    为什么不能只用 :func:`assert_fence`：``assert_fence`` 只比令牌，**不看冻结状态**。
    被冻结者主动 ``unfreeze`` 之后（令牌已旋转）虽然也会不匹配，但"冻结被解除"和
    "令牌被换"是两种不同的运维事实，报出准确原因才能让人判断该重跑还是该排查。
    更关键的是：**激活/切换必须发生在本 job 自己的冻结窗口内**——冻结已被解除
    还去发布，等于在没有排他保护的情况下切换线上索引。
    """
    with transaction.atomic():
        gate = lock_gate_for_update(library_id)
        if gate.frozen_at is None:
            raise WriteFrozen(
                f"信息库 #{library_id} 的冻结已被解除，本 job 不再是所有者"
                f"（{action} 被拒绝）：拒绝在没有排他冻结的情况下切换线上索引",
                reason="冻结已被解除",
                frozen_by=gate.frozen_by,
            )
        if fence_token is None or fence_token != (gate.fence_token or ""):
            raise WriteFrozen(
                f"信息库 #{library_id} 的冻结令牌与本 job 持有的不一致"
                f"（{action} 被拒绝）：本 job 的重建已被接管或解除",
                reason="fence_token 不匹配",
                frozen_by=gate.frozen_by,
            )


def assert_no_active_leases_locked(library_id: int, *, now=None) -> None:
    """**必须在持有闸门行锁的事务内**调用（由 :func:`freeze` 调用）。

    活跃租约 = ``parse_lease_expires_at > now``：说明有 worker 正在解析/嵌入。
    重建**不得抢占**它，必须显式拒绝——否则两个写者会抢同一份 file 的状态。
    """
    _require_atomic("assert_no_active_leases_locked()")
    now = now or timezone.now()
    busy = list(
        IngestedFile._base_manager.filter(library_id=library_id)
        .filter(parse_lease_expires_at__gt=now)
        .values_list("pk", "original_name")[:20]
    )
    if busy:
        names = ", ".join(f"#{pk}({name})" for pk, name in busy[:5])
        raise WriteFrozen(
            f"信息库 #{library_id} 有 {len(busy)} 份资料正在被处理（活跃租约：{names}…）："
            "重建被拒绝，保留旧 job 的租约；请等其完成或显式取消后再重建",
            reason="存在活跃 file 租约",
        )


def freeze(library_id: int, *, reason: str, actor: str = "") -> str:
    """冻结本库并**旋转**令牌，返回新令牌。

    **租约检查与冻结节写入在同一个持锁事务内**（增量审查要求）：否则"查租约 →
    写冻结"之间还能插入新租约。
    """
    with transaction.atomic():
        gate = lock_gate_for_update(library_id)
        if gate.frozen_at is not None:
            raise WriteFrozen(
                f"信息库 #{library_id} 已被冻结：{gate.freeze_reason or '未注明原因'}"
                f"（by {gate.frozen_by or '未知'}）；拒绝重复冻结/并发重建",
                reason=gate.freeze_reason,
                frozen_by=gate.frozen_by,
            )
        assert_no_active_leases_locked(library_id)
        token = uuid.uuid4().hex
        LibraryWriteGate.objects.filter(library_id=library_id).update(
            frozen_at=timezone.now(),
            frozen_by=actor or "",
            freeze_reason=reason[:200],
            # **旋转、但永不清空**：旧 token 从此永久失效（防 ABA）
            fence_token=token,
        )
    return token


def unfreeze(library_id: int, *, fence_token: str | None = None) -> None:
    """解除冻结：只清冻结状态，并**再旋转一个新令牌**（绝不"保留最新令牌"）。

    为什么必须再旋转一次，而不是把冻结时的令牌留着：
    "冻结期间的令牌"同时也是**冻结之前那些普通 writer** 可能已经拿到手的令牌
    （:func:`gate_for` 初建即给非空令牌，写路径会把它当成许可带过模型 IO）。
    如果解除时保留同一个值，那么"冻结 → 解除 → collection 恰好没变"时，
    一个在冻结前就拿到该令牌的旧 job 会在解除后**复活**并通过 ``assert_fence``。
    每解除一次就换一次令牌，旧 owner 与旧 writer 的许可一并作废。

    ``fence_token`` 给定时按"是否提供"严格比较（``None`` 才是未提供）：
    提供但不匹配 → 拒绝（说明冻结已被新的重建接管），绝不覆盖别人的状态。
    """
    with transaction.atomic():
        gate = lock_gate_for_update(library_id)
        if gate.frozen_at is None:
            return
        if fence_token is not None and fence_token != (gate.fence_token or ""):
            raise WriteFrozen(
                f"信息库 #{library_id} 的令牌不匹配，拒绝解除"
                "（可能已被新的重建接管）",
                reason="fence_token 不匹配",
                frozen_by=gate.frozen_by,
            )
        LibraryWriteGate.objects.filter(library_id=library_id).update(
            frozen_at=None,
            frozen_by="",
            freeze_reason="",
            # **再旋转一次**：不仅本次 freeze owner 的令牌失效，解除之前所有普通 writer
            # 持过的旧令牌也一并失效（"永不清空"= 永远非空且持续变化）。
            # 否则旧重建 job 在冻结解除后醒来，仍能用它手里的 token 通过 assert_fence。
            fence_token=uuid.uuid4().hex,
        )


def gate_state(library_id: int) -> dict:
    """只读快照（界面/CLI 展示用，不加锁、不写入）。"""
    row = (
        LibraryWriteGate.objects.filter(library_id=library_id)
        .values("frozen_at", "frozen_by", "freeze_reason", "fence_token")
        .first()
    )
    if not row:
        return {
            "frozen": False,
            "frozen_at": None,
            "frozen_by": "",
            "reason": "",
            "fence_token": "",
        }
    return {
        "frozen": row["frozen_at"] is not None,
        "frozen_at": row["frozen_at"],
        "frozen_by": row["frozen_by"],
        "reason": row["freeze_reason"],
        "fence_token": row["fence_token"] or "",
    }
