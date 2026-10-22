"""审计读接口的权限口径（Codex 独立审查发现的高危越权）。

原状：``/api/v1/audit/events/`` 与 ``/api/v1/audit/verify/`` 只要求"已登录"，
查询是 ``AuditEvent.objects.all()`` —— 任何登录用户都能读全库审计事件
（payload / actor / resource_key）并触发整条哈希链重算；而 Agent 控制台的审计卡
按 ``status===403`` 假设"只有系统管理员能看"，那段分支是死代码。

现在两个端点都要求系统管理员（``system:read`` 口径，与 planner 的 ``list_audit``
操作一致）。本文件把这四条边界钉死。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.django_db

EVENTS = "/api/v1/audit/events/"
VERIFY = "/api/v1/audit/verify/"


def test_anonymous_cannot_read_audit(api):
    for url in (EVENTS, VERIFY):
        response = api.get(url)
        assert response.status_code in {401, 403}, (url, response.status_code)


def test_plain_user_cannot_read_audit(api, user, admin_user):
    """普通登录用户（非 staff）必须被拒 —— 这正是原来的越权口子。"""
    from apps.audit.services import append_event

    append_event(action="test.audit.secret", actor_id=admin_user.pk,
                 resource_kind="test", resource_key="r1", payload={"secret": "不给你看"})
    api.force_authenticate(user=user)
    events = api.get(EVENTS)
    assert events.status_code == 403, events.content[:200]
    # 连哈希链重算也不许触发
    assert api.get(VERIFY).status_code == 403


def test_plain_user_does_not_see_audit_payload(api, user, admin_user):
    """拒绝必须是"看不到内容"，而不是"返回空列表"（空列表会把越权说成没数据）。"""
    from apps.audit.services import append_event

    append_event(action="test.audit.secret", actor_id=admin_user.pk,
                 resource_kind="test", resource_key="r2", payload={"secret": "机密"})
    api.force_authenticate(user=user)
    response = api.get(EVENTS)
    assert response.status_code == 403
    assert b"secret" not in response.content and b"\xe6\x9c\xba\xe5\xaf\x86" not in response.content


def test_system_admin_can_read_audit(api, admin_user):
    from apps.audit.services import append_event

    append_event(action="test.audit.admin", actor_id=admin_user.pk,
                 resource_kind="test", resource_key="r3", payload={"ok": True})
    api.force_authenticate(user=admin_user)
    response = api.get(EVENTS)
    assert response.status_code == 200, response.content[:200]
    body = response.json()
    rows = body["results"] if isinstance(body, dict) and "results" in body else body
    assert any(
        (row.get("action") == "test.audit.admin" for row in rows)
    ), "系统管理员应当能看到审计事件"
    assert api.get(VERIFY).status_code == 200
