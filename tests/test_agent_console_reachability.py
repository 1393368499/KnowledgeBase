"""后端有能力、页面却零入口的接口，现在必须在 Agent 控制台真的有入口（审计 B2/E3）。

审计结论（未修前）：`/knowledge/generations/`、`/knowledge/index-stats/`、
`/audit/events/`、`/audit/verify/` 四个能力**后端齐全**，但 `apps/webui` 下
一处 URL 都没有 → 普通用户唯一触达方式是手写 HTTP 或让模型提工具调用。
这些用例把"入口存在且走受控操作"钉住，防止回归成"只能靠 HTTP"。

注意：本文件只读源码与路由（不起浏览器），真正的端到端由
`artifacts/acceptance/verify-agent-console.py` 负责。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from django.urls import resolve

from apps.planner.operations import OPERATIONS

ROOT = Path(__file__).resolve().parent.parent
PAGES_JS = ROOT / "apps/webui/static/webui/kb-pages.js"


def _script() -> str:
    return PAGES_JS.read_text(encoding="utf-8")


def test_routes_exist_for_the_console_cards():
    """四个接口的 URL 必须真的能解析（页面入口才有意义）。"""
    assert resolve("/api/v1/knowledge/index-stats/").func is not None
    assert resolve("/api/v1/knowledge/generations/").func is not None
    assert resolve("/api/v1/audit/events/").func is not None
    assert resolve("/api/v1/audit/verify/").func is not None


def test_console_calls_index_stats_and_audit_events():
    script = _script()
    assert '"/api/v1/knowledge/index-stats/"' in script
    assert '"/api/v1/audit/events/?page_size=8"' in script
    assert '"/api/v1/audit/verify/"' in script


def test_generation_switch_goes_through_controlled_operation_not_raw_put():
    """切换/回退必须走 planner 受控操作（提案→确认→执行），不能直连 PUT。"""
    script = _script()
    assert 'intent: "activate_generation"' in script
    assert 'intent: "rollback_generation"' in script
    assert '"/api/v1/knowledge/generations/"' not in script, "又绕过受控流程直连 PUT 了"
    assert "activate_generation" in OPERATIONS
    assert "rollback_generation" in OPERATIONS


def test_audit_verify_reports_only_what_backend_says():
    """校验结论只报 ok/checked 事实，不许写"绝对安全"之类推断。"""
    script = _script()
    assert "未发现被改动" in script
    assert "绝对安全" not in script


def test_permission_denied_is_distinguished_from_empty():
    """403 必须与"确实没数据"分开说（否则管理员以外的账号会以为索引丢了）。"""
    script = _script()
    assert "只有系统管理员能查看索引状态" in script
    assert "只有系统管理员能查看审计事件" in script


def test_settings_page_still_marks_denied_rows():
    """系统配置页对 403 的行必须如实标注（此前普通账号看到"未配置"）。"""
    script = _script()
    assert "模型服务配置" in script
    assert "无权限" in script or "403" in script
