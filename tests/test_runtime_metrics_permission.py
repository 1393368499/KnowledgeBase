"""运行指标的权限必须在**采集之前**生效（Codex 审计 OP-20）。

原状：`KnowledgeRuntimeMetricsView` 是 ``permission_classes = ()`` + 函数体内判权，
而 ``collect()`` 在判权**之前**执行 —— 任何登录用户（乃至匿名请求）都能触发一次
真实指标采集，然后才拿到 403；同时匿名请求拿到的是 403 而不是 401（身份未提供）。

修法：权限做成 DRF 权限类（handler 之前执行），并把匿名未认证统一成 401。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.django_db

METRICS = "/api/v1/knowledge/runtime-metrics/"


def test_anonymous_is_rejected_before_collecting(api, monkeypatch):
    """匿名：401（未认证），且**没有**触发采集。"""
    called = {"n": 0}

    def boom(*args, **kwargs):  # pragma: no cover - 被调用即失败
        called["n"] += 1
        raise AssertionError("未授权请求不应触发 collect()")

    monkeypatch.setattr("apps.knowledge.metrics.collect", boom)
    response = api.get(METRICS)
    assert response.status_code in (401, 403), response.status_code
    assert called["n"] == 0


def test_plain_user_is_rejected_before_collecting(api, user, monkeypatch):
    called = {"n": 0}

    def boom(*args, **kwargs):  # pragma: no cover
        called["n"] += 1
        raise AssertionError("普通账号不应触发 collect()")

    monkeypatch.setattr("apps.knowledge.metrics.collect", boom)
    api.force_authenticate(user=user)
    response = api.get(METRICS)
    assert response.status_code == 403, response.content[:200]
    assert called["n"] == 0


def test_system_admin_can_read_metrics(api, admin_user):
    api.force_authenticate(user=admin_user)
    response = api.get(METRICS)
    assert response.status_code == 200, response.content[:200]
    assert isinstance(response.data, dict)
