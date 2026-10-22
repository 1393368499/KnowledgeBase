"""退役待删登记行的**回收路径**：远端已删、登记还在时，kbvacuum 必须能收掉它。

为什么值得单独钉住：清理旧代次的顺序是"锁内标记退役 → 锁外远端删除 → 成功才删登记行"。
`_drop_collection` 成功、但紧接着的登记行删除失败（DB 抖动/进程被杀）时，会留下
"远端已不存在、登记仍带 retiring_at"的行。这条**必须**能被现有工具回收，
否则就是我们自己造的孤儿（独立审查反复强调"不要先丢登记、也不要有回收不了的残留"）。

`kbvacuum --apply` 的判据恰好就是"登记行的 collection 在 Qdrant 里不存在"，
所以它天然能回收这种行 —— 本用例用假客户端把这条路径跑实，并核对审计留痕。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.knowledge.models import KnowledgeLibrary, VectorCollection


class _FakeClient:
    """最小假客户端：只提供 kbvacuum 用到的两个能力。"""

    def __init__(self, names: list[str]) -> None:
        self._names = names

    def get_collections(self):
        return SimpleNamespace(collections=[SimpleNamespace(name=n) for n in self._names])

    def close(self) -> None:  # pragma: no cover - 只是接口对齐
        return None


@pytest.mark.django_db
def test_vacuum_apply_reclaims_retiring_registration_when_collection_is_gone(
    library, monkeypatch
):
    name = f"kb-{library.pk}-g1"
    row = VectorCollection.objects.create(
        name=name, dimension=1024, library=library, is_active=False, retiring_at=timezone.now()
    )
    # 远端已经没有这个 collection（模拟"drop 成功但登记删除失败"）
    monkeypatch.setattr(
        "apps.knowledge.vectorindex.build_client", lambda *a, **k: _FakeClient([])
    )
    monkeypatch.setattr(
        "apps.knowledge.vectorindex.collection_exists", lambda client, target: False
    )

    call_command("kbvacuum", "--apply", verbosity=0)

    assert not VectorCollection.objects.filter(pk=row.pk).exists(), (
        "退役登记行在远端已消失后仍然回收不掉 → 自己造了孤儿"
    )
    assert AuditEvent.objects.filter(
        action="vector.collection_registration_removed", resource_key=name
    ).exists(), "回收必须留审计痕"


@pytest.mark.django_db
def test_vacuum_dry_run_keeps_retiring_registration(library, monkeypatch):
    """默认 dry-run 一个字都不改（退役行也不许被顺手删掉）。"""
    name = f"kb-{library.pk}-g2"
    row = VectorCollection.objects.create(
        name=name, dimension=1024, library=library, is_active=False, retiring_at=timezone.now()
    )
    monkeypatch.setattr(
        "apps.knowledge.vectorindex.build_client", lambda *a, **k: _FakeClient([])
    )
    monkeypatch.setattr(
        "apps.knowledge.vectorindex.collection_exists", lambda client, target: False
    )

    call_command("kbvacuum", verbosity=0)

    assert VectorCollection.objects.filter(pk=row.pk).exists(), "dry-run 不应删除任何登记行"
    assert KnowledgeLibrary.objects.filter(pk=library.pk).exists()
