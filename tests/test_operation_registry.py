"""操作登记表（`apps/planner/operations.py`）的**不变量**回归。

这些不变量是受控流程的前提条件，任何一条破了，界面上就会出现
"看起来需要确认、其实不需要"或"零确认就写库"的动作：

1. **会写库的动作必须登记为需要确认**（独立审计 A2）：`requires_confirmation`
   只是风险提示（真正的次数由服务端定死：破坏性 3 次、其余 1 次），但它必须是
   诚实的 —— 一个会写库的动作却写着"无需确认"，会让人以为有自动执行通道；
2. **破坏性动作必须三次确认 + 独立 ticket scope**：否则"三次独立确认"是空话；
3. **真实提案的确认次数只能是 3（破坏性）或 1（其余）**；
4. **参数声明只允许受支持的 kind**（`int`/`str`/`list`/`refs`，可带 `?` 表可选），
   否则 `_validate_params` 会因为不认识的 kind 而静默放过类型校验。反过来，
   校验器里也不许留"没有任何操作在用"的死分支。
"""

from __future__ import annotations

import pytest

from apps.planner import services
from apps.planner.operations import DESTRUCTIVE_SCOPES, all_specs

pytestmark = pytest.mark.django_db


def test_only_deletions_require_confirmation():
    from apps.planner.operations import is_deletion
    for spec in all_specs():
        assert spec.requires_confirmation == is_deletion(spec.intent), spec.intent
        assert spec.auto_execute != spec.requires_confirmation, spec.intent
        if spec.auto_execute:
            assert not spec.requires_destructive_flow


def test_destructive_specs_require_three_confirmations_and_scope():
    destructive = [spec for spec in all_specs() if spec.requires_destructive_flow]
    assert destructive, "至少应有删除类动作登记为破坏性流程"
    for spec in destructive:
        assert spec.requires_confirmation is True, spec.intent
        assert spec.ticket_scope in DESTRUCTIVE_SCOPES, spec.intent
        assert spec.ticket_scope, spec.intent


def test_ticket_scopes_are_unique_per_destructive_intent():
    scopes = [spec.ticket_scope for spec in all_specs() if spec.requires_destructive_flow]
    assert len(scopes) == len(set(scopes)), f"破坏性动作的 ticket scope 必须唯一：{scopes}"


def test_param_kinds_are_supported():
    """登记表里只允许出现**校验器真正支持**的 kind。

    早先这条用例是 ``@pytest.mark.parametrize("kind", [...])``，但函数体根本没用
    ``kind`` —— 三次重复跑同一件事，看起来覆盖了三种 kind 其实只断言了一个集合。
    改成直接枚举：既挡住没实现的 kind（会静默落到 str 分支），也挡住"某个 kind
    已经没有任何操作在用"（说明校验器里留了死分支）。
    """
    supported = {"int", "uint", "str", "refs"}
    used: set[str] = set()
    for spec in all_specs():
        for name, declared in spec.params.items():
            base = declared.rstrip("?")
            assert base in supported, f"{spec.intent}.{name} 声明了不支持的 kind：{declared}"
            used.add(base)
    assert used == supported, f"有 kind 没有任何操作在用：{sorted(supported - used)}"


def test_required_confirmations_are_zero_or_deletion_strength(admin_user, library, make_file):
    """真实提案：破坏性=3，其余=1（把口径钉死在服务端，而不是文档里）。"""
    record = make_file(library=library, name="确认口径.md")
    plain = services.propose_operation(
        admin_user, intent="classify_file", params={"file_id": record.pk, "classification": "合同"}
    )
    assert plain["required_confirmations"] == 0
    assert plain["status"] == "executed"
    assert plain["requires_destructive_flow"] is False

    heavy = services.propose_operation(
        admin_user, intent="delete_library", params={"library": library.code}
    )
    assert heavy["required_confirmations"] == services.DESTRUCTIVE_CONFIRMATIONS == 3
    assert heavy["requires_destructive_flow"] is True
