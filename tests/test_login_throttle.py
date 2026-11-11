"""F-10：登录失败限速（/app/login/ 与 /admin/login/ 共用口径）。

达到 KB_LOGIN_MAX_ATTEMPTS 后锁定 KB_LOGIN_LOCKOUT_SECONDS；成功登录清零。
"""

from __future__ import annotations

import pytest
from django.core.cache import cache
from django.test import override_settings

pytestmark = pytest.mark.django_db

PASSWORD = "Correct-Horse-Battery-9"


@pytest.fixture(autouse=True)
def _clear_throttle_cache():
    cache.clear()
    yield
    cache.clear()


def _fail(client, path: str, username: str, times: int) -> list[int]:
    codes = []
    for _ in range(times):
        response = client.post(path, {"username": username, "password": "definitely-wrong"})
        codes.append(response.status_code)
    return codes


@override_settings(KB_LOGIN_MAX_ATTEMPTS=3, KB_LOGIN_LOCKOUT_SECONDS=60)
def test_app_login_is_throttled_after_repeated_failures(client, user):
    user.set_password(PASSWORD)
    user.save()
    path = "/app/login/"
    assert _fail(client, path, user.username, 3) == [200, 200, 200]
    blocked = client.post(path, {"username": user.username, "password": PASSWORD})
    assert blocked.status_code == 429, "锁定后即使口令正确也应先拒绝"
    assert blocked["Retry-After"].isdigit()


@override_settings(KB_LOGIN_MAX_ATTEMPTS=3, KB_LOGIN_LOCKOUT_SECONDS=60)
def test_admin_login_is_throttled_too(client, admin_user):
    admin_user.set_password(PASSWORD)
    admin_user.save()
    path = "/admin/login/"
    _fail(client, path, admin_user.username, 3)
    blocked = client.post(path, {"username": admin_user.username, "password": PASSWORD})
    assert blocked.status_code == 429


@override_settings(KB_LOGIN_MAX_ATTEMPTS=3, KB_LOGIN_LOCKOUT_SECONDS=60)
def test_successful_login_clears_failure_count(client, user):
    user.set_password(PASSWORD)
    user.save()
    path = "/app/login/"
    _fail(client, path, user.username, 2)
    ok = client.post(path, {"username": user.username, "password": PASSWORD})
    assert ok.status_code == 302
    # 已登录状态下登录页会重定向；先登出再继续验证计数已清零。
    client.logout()
    # 计数已清零：再失败两次仍不应锁定（阈值 3）
    assert _fail(client, path, user.username, 2) == [200, 200]
    third = client.post(path, {"username": user.username, "password": "definitely-wrong"})
    assert third.status_code == 200
    locked = client.post(path, {"username": user.username, "password": PASSWORD})
    assert locked.status_code == 429
