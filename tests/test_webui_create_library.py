"""「新建信息库」入口的前端回归（源码级断言）。

验收点：
  * 信息库页有「新建信息库」入口，**仅系统管理员可见**（后端仍会再验权限）；
  * 创建**必须**走 planner 受控操作 `create_library`，没有直连建库的旁路；
  * 级别（internal/external）必须可填并提交给后端（后端会校验并落库）；
  * 支持取消与失败分开反馈；成功后刷新列表。
"""

from __future__ import annotations

from pathlib import Path

WEBUI = Path("apps/webui/static/webui")


def _read(name: str) -> str:
    return (WEBUI / name).read_text(encoding="utf-8")


def test_create_entry_exists_and_is_admin_gated():
    pages = _read("kb-pages.js")
    assert "lib-create-open" in pages, "信息库页缺少「新建信息库」入口"
    assert "新建信息库" in pages
    assert "openCreateLibraryDialog" in pages, "缺少新建弹窗实现"
    # 入口与删除按钮同一把门（模板下发 data-system-admin，后端再验）
    assert "isSystemAdmin()" in pages


def test_creation_goes_through_controlled_operation():
    pages = _read("kb-pages.js")
    assert 'intent: "create_library"' in pages, "未走 planner 受控意图"
    assert "KB.controlled(" in pages
    # 级别必须可填且真的提交
    assert 'id="lib-level"' in pages, "缺少级别选择器"
    assert "level: level" in pages, "级别未提交给后端"
    assert 'value="internal"' in pages and 'value="external"' in pages


def test_no_direct_create_bypass_and_refresh_after_success():
    pages = _read("kb-pages.js")
    # 不允许直接 POST 建库（绕过提案/确认/审计）
    for forbidden in (
        'method: "POST"',
    ):
        # 允许其它受控调用存在，但不能是直接打 libraries 集合端点建库
        assert 'KB.api("/api/v1/knowledge/libraries/", {' not in pages, (
            "出现直连建库调用，绕过了受控流程"
        )
    assert "await loadKnowledge()" in pages, "创建成功后未刷新列表"
    # 取消与失败要分开反馈，不能都当成功
    assert "已取消，未创建" in pages
    assert 'result.detail || "创建失败"' in pages
