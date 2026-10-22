"""运维命令回归：``kbvacuum``（陈旧向量登记）与 ``kbcleanup``（过期一次性凭证）。

这两个命令补的是**没有任何入口**的日常维护缺口（审计确认：清理陈旧登记只能
"整库重置成功"才顺带做，而过期凭证根本没人清）。用例只验证"它真的只做该做的事"：

* ``kbvacuum``：默认 dry-run 一个字不改；``--apply`` 只删**Qdrant 里确实不存在**的
  登记行，真实存在的 collection 的登记行必须保住；
* ``kbcleanup``：默认 dry-run；``--apply`` 只删**已过期且超过保留期**的凭证，
  审计事件与操作历史一律不动。
"""

from __future__ import annotations

import io

import pytest
from django.core.management import call_command

from apps.audit.models import AuditEvent
from apps.knowledge.models import VectorCollection

pytestmark = pytest.mark.django_db


class _KeepAlive:
    """代理真实 Qdrant 客户端，但 ``close()`` 是空实现。

    命令会在 ``finally`` 里关掉自己建的客户端；测试里客户端由夹具共享，
    被命令关掉会让后续断言拿到 "instance is closed"。生产不受影响
    （``build_client()`` 每次返回新实例）。
    """

    def __init__(self, client):
        self._client = client

    def __getattr__(self, name):
        return getattr(self._client, name)

    def close(self) -> None:  # noqa: D401 - 故意什么都不做
        pass


@pytest.fixture
def patched_client(qdrant_client, monkeypatch):
    from apps.knowledge import vectorindex

    monkeypatch.setattr(vectorindex, "build_client", lambda: _KeepAlive(qdrant_client))
    return qdrant_client


# --------------------------------------------------------------- kbvacuum ----


def test_kbvacuum_dry_run_reports_stale_without_deleting(db, patched_client, make_library):
    library = make_library(code="vacuum-lib", name="真空库")
    ghost = VectorCollection.objects.create(
        name="kb-vacuum-g9", dimension=1024, library=library
    )

    out = io.StringIO()
    call_command("kbvacuum", stdout=out)

    text = out.getvalue()
    assert "kb-vacuum-g9" in text, text
    assert "dry-run" in text
    assert VectorCollection.objects.filter(pk=ghost.pk).exists(), "dry-run 竟然删了行"


def test_kbvacuum_apply_only_removes_registrations_missing_in_qdrant(
    db, patched_client, make_library
):
    from apps.knowledge.vectorindex import ensure_collection

    library = make_library(code="vacuum-apply", name="真空清理库")
    ensure_collection(patched_client, "kb-real-g1", 1024)  # 真实存在的 collection
    real = VectorCollection.objects.create(
        name="kb-real-g1", dimension=1024, library=library, is_active=True
    )
    ghost = VectorCollection.objects.create(name="kb-ghost-g1", dimension=1024, library=library)

    out = io.StringIO()
    call_command("kbvacuum", "--apply", "--json", stdout=out)

    assert VectorCollection.objects.filter(pk=real.pk).exists(), "真实索引的登记被删了"
    assert not VectorCollection.objects.filter(pk=ghost.pk).exists()
    assert AuditEvent.objects.filter(
        action="vector.collection_registration_removed", resource_key="kb-ghost-g1"
    ).exists(), "删登记必须留审计"
    # 真实的 collection 没有被当成"未登记"报告（它本来就有登记）
    assert "kb-real-g1" not in out.getvalue() or "unregistered" in out.getvalue()


def test_kbvacuum_reports_unregistered_collections_without_touching_them(
    db, patched_client, make_library
):
    """Qdrant 里存在但没登记行的 collection：只报告，绝不删。"""
    from apps.knowledge.vectorindex import collection_exists, ensure_collection

    ensure_collection(patched_client, "kb-unregistered-g1", 1024)

    out = io.StringIO()
    call_command("kbvacuum", "--apply", "--json", stdout=out)

    assert "kb-unregistered-g1" in out.getvalue(), "没有报告未登记的 collection"
    assert collection_exists(patched_client, "kb-unregistered-g1"), "把向量删了"


# -------------------------------------------------------------- kbcleanup ----


def test_kbcleanup_dry_run_then_apply_only_removes_expired(
    db, user, library, make_file, grant_member
):
    from datetime import timedelta

    from django.utils import timezone

    from apps.knowledge.models import MemberRole
    from apps.planner import services
    from apps.planner.models import ActionTicket, DeleteGrant, OperationRecord

    now = timezone.now()
    old = ActionTicket.objects.create(
        user=user,
        token_hash="a" * 64,
        scope=ActionTicket.Scope.CONFIRM_STEP_1,
        intent="delete_file",
        expires_at=now - timedelta(days=40),
    )
    fresh = ActionTicket.objects.create(
        user=user,
        token_hash="b" * 64,
        scope=ActionTicket.Scope.CONFIRM_STEP_1,
        intent="delete_file",
        expires_at=now + timedelta(hours=1),
    )
    # DeleteGrant 必须挂在真实操作上（FK 非空）：走一次真实提案
    grant_member(library, user, role=MemberRole.ADMIN)
    record = make_file(library, "准备删除.md")
    proposal = services.propose_operation(
        user, intent="delete_file", params={"file_id": record.pk}
    )
    operation = OperationRecord.objects.get(pk=proposal["operation_id"])
    grant = DeleteGrant.objects.create(
        operation=operation,
        user=user,
        target_kind=DeleteGrant.TargetKind.INGESTED_FILE,
        target_ref=str(record.pk),
        expires_at=now - timedelta(days=40),
    )
    audit_before = AuditEvent.objects.count()

    dry = io.StringIO()
    call_command("kbcleanup", stdout=dry)
    assert "dry-run" in dry.getvalue()
    assert ActionTicket.objects.filter(pk=old.pk).exists()
    assert DeleteGrant.objects.filter(pk=grant.pk).exists()

    applied = io.StringIO()
    call_command("kbcleanup", "--apply", stdout=applied)

    assert not ActionTicket.objects.filter(pk=old.pk).exists(), "过期凭证没清掉"
    assert ActionTicket.objects.filter(pk=fresh.pk).exists(), "未过期的凭证被误删"
    assert not DeleteGrant.objects.filter(pk=grant.pk).exists()
    assert AuditEvent.objects.count() == audit_before, "审计事件被动了"


def test_kbcleanup_retention_window_is_honoured(db, user, make_library):
    """刚过期但还在保留期内的凭证不删（保留期是为了复盘）。"""
    from datetime import timedelta

    from django.utils import timezone

    from apps.planner.models import ActionTicket

    recent = ActionTicket.objects.create(
        user=user,
        token_hash="d" * 64,
        scope=ActionTicket.Scope.CONFIRM_FINAL,
        intent="delete_library",
        expires_at=timezone.now() - timedelta(hours=2),
    )

    call_command("kbcleanup", "--apply", "--retention-days", "30", stdout=io.StringIO())

    assert ActionTicket.objects.filter(pk=recent.pk).exists()
