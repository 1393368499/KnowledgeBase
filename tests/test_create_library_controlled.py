"""「新建信息库」的受控创建回归（信息库页新增入口）。

口径（现行策略：只有删除保留人工确认，其它非删除动作提案即执行）：
  * 只能走 planner 受控操作 `create_library`（一次提案即执行），
    **没有**直连建库的旁路；审计、权限复核与目标身份核验仍然生效；
  * `level` 必须真的落库（internal/external），非法值明确报错；
  * 仅**系统管理员**可建（非管理员即便给了库标识也会被拒）；
  * 库标识重复 → 明确报错，不静默改写成别的标识；
  * 写操作留审计（谁/何时/哪个库/什么级别）。
"""

from __future__ import annotations

import pytest

from apps.audit.models import AuditEvent
from apps.knowledge.models import KnowledgeLibrary
from apps.planner import services
from apps.planner.models import OperationRecord

pytestmark = pytest.mark.django_db


def _create_via_controlled(user, *, code: str, name: str, level: str):
    """走现行受控流程：一次提案即执行（非删除动作零人工确认）。"""
    proposal = services.propose_operation(
        user, intent="create_library", params={"code": code, "name": name, "level": level}
    )
    assert proposal["auto_executed"] is True, proposal
    assert proposal["confirm_token"] == "", "非删除动作不应签发确认凭证"
    assert proposal["required_confirmations"] == 0
    operation = OperationRecord.objects.get(pk=proposal["operation_id"])
    assert operation.status == OperationRecord.Status.EXECUTED, operation.status
    assert operation.confirmations == 0
    return proposal["result"]


def test_admin_creates_library_with_level(admin_user):
    result = _create_via_controlled(
        admin_user, code="ui-create-internal", name="界面新建内部库", level="internal"
    )
    assert result["code"] == "ui-create-internal"
    assert result["level"] == "internal"
    library = KnowledgeLibrary.objects.get(code="ui-create-internal")
    assert library.name == "界面新建内部库"
    assert library.level == "internal", "level 未生效"


def test_external_level_is_honored(admin_user):
    result = _create_via_controlled(
        admin_user, code="ui-create-external", name="界面新建外部库", level="external"
    )
    assert result["level"] == "external"
    assert KnowledgeLibrary.objects.get(code="ui-create-external").level == "external"


def test_invalid_level_is_rejected_without_creating(admin_user):
    """非法 level 在**执行时**被拒（提案即执行 → 拒绝直接抛回调用方），不建库。"""
    with pytest.raises(services.PlannerError) as exc:
        services.propose_operation(
            admin_user,
            intent="create_library",
            params={"code": "ui-bad-level", "name": "非法级别库", "level": "super-secret"},
        )
    assert "level" in str(exc.value)
    assert not KnowledgeLibrary.objects.filter(code="ui-bad-level").exists(), "非法级别却建了库"
    # 失败必须如实落成 failed，不能假装成功
    operation = OperationRecord.objects.get(intent="create_library")
    assert operation.status == OperationRecord.Status.FAILED
    assert "level" in operation.error


def test_duplicate_code_is_rejected(admin_user, make_library):
    make_library(code="ui-dup", name="已存在的库")
    with pytest.raises(services.PlannerError) as exc:
        services.propose_operation(
            admin_user,
            intent="create_library",
            params={"code": "ui-dup", "name": "想重名建库", "level": "internal"},
        )
    assert "已存在" in str(exc.value)
    # 原有库未被改名
    assert KnowledgeLibrary.objects.get(code="ui-dup").name == "已存在的库"
    operation = OperationRecord.objects.get(intent="create_library")
    assert operation.status == OperationRecord.Status.FAILED
    assert "已存在" in operation.error


def test_non_admin_cannot_create_library(user):
    """普通用户（非系统管理员）不得建库（新库不存在，无法通过库级写权限校验）。"""
    with pytest.raises(services.PlannerError):
        services.propose_operation(
            user,
            intent="create_library",
            params={"code": "ui-nonadmin", "name": "无权限建库", "level": "internal"},
        )
    assert not KnowledgeLibrary.objects.filter(code="ui-nonadmin").exists()


def test_creation_is_audited(admin_user):
    _create_via_controlled(
        admin_user, code="ui-audit-lib", name="审计留痕库", level="internal"
    )
    event = AuditEvent.objects.filter(action="knowledge.library_created").order_by("-id").first()
    assert event is not None, "建库没有审计事件"
    assert event.actor_id == admin_user.pk
    assert event.resource_key == "ui-audit-lib"
    assert event.payload.get("level") == "internal"
    assert event.payload.get("name") == "审计留痕库"
