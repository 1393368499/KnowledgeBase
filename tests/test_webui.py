"""Web UI 回归测试：登录、页面可达、信息库下拉只用可读库。

界面是验收入口，所以它自己也要有保护：登录必须走 session（不是 Basic），
未登录必须跳登录页，下拉不能泄露无权信息库。
"""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

from apps.knowledge.models import LibraryMember

pytestmark = pytest.mark.django_db


def test_anonymous_is_redirected_to_login():
    """未登录访问文件/检索页 → 跳转到我们的 /app/login/（不是 admin 登录页）。"""
    client = Client()
    for name in ("webui:files", "webui:search"):
        response = client.get(reverse(name))
        assert response.status_code == 302
        assert response["Location"].startswith("/app/login/")


def test_login_page_renders_without_basic_auth():
    response = Client().get("/app/login/")
    assert response.status_code == 200
    body = response.content.decode("utf-8")
    # 面向用户的文案：不出现 session / Basic 这类实现细节
    assert "联系管理员" in body
    assert "session" not in body.lower()
    assert "Basic" not in body
    assert "csrfmiddlewaretoken" in body  # 有 CSRF，说明是真实表单登录


def test_login_with_session_then_pages_render(admin_user):
    client = Client()
    assert client.login(username="admin", password="pw-not-a-secret-123") is True

    files = client.get(reverse("webui:files"))
    assert files.status_code == 200
    files_body = files.content.decode("utf-8")
    assert "文件管理" in files_body
    assert "webkitdirectory" in files_body  # 支持整个文件夹上传
    assert 'id="files-upload"' in files_body
    assert 'id="files-lib"' in files_body
    # 布局：侧栏必须是 .sidebar 容器（否则导航会掉进普通文档流，被顶栏盖住）
    assert 'class="sidebar"' in files_body
    assert "<nav" in files_body
    # 文案里不能出现实现细节
    assert "directory_path" not in files_body
    assert "API" not in files_body
    assert "**" not in files_body  # 不能把 Markdown 星号原样显示

    search = client.get(reverse("webui:search"))
    assert search.status_code == 200
    search_body = search.content.decode("utf-8")
    # 需求覆盖：搜索页不再显示信息库下拉（"除首页右文档栏外不能加控件"），
    # 后台 libraries 过滤接口保留、URL 条件继续支持。
    assert 'id="libraries"' not in search_body
    assert 'value="semantic"' not in search_body, "已按原型收敛为 3 个模式"
    # 原型三模式仍在
    assert all(
        marker in search_body
        for marker in ('value="hybrid"', 'value="filename"', 'value="content"')
    )
    # 结构化筛选控件仍是服务端条件
    assert all(
        marker in search_body
        for marker in ('id="department"', 'id="nature"', 'id="amount_min"')
    )
    assert "**" not in search_body
    # 原型搜索页没有独立的"诊断信息"面板（不再多一个控件）；
    # 但"检索降级"必须如实告诉用户，这一点在脚本里保证。
    from pathlib import Path

    script = (
        Path(__file__).resolve().parents[1]
        / "apps/webui/static/webui/kb-search.js"
    ).read_text(encoding="utf-8")
    assert "degraded" in script, "检索降级必须如实提示，不能静默"
    assert "降级结果" in script


def test_pages_are_served_via_session_cookie_not_basic(admin_user):
    """登录后页面响应里不能有 WWW-Authenticate（那是 Basic 认证的信号）。"""
    client = Client()
    client.login(username="admin", password="pw-not-a-secret-123")
    response = client.get(reverse("webui:files"))
    assert "WWW-Authenticate" not in response.headers


def test_libraries_api_only_lists_readable(api, user, other_user, make_library, grant_member):
    """下拉的数据源（libraries API）只返回可读库——下拉因此不会泄露库名。"""
    mine = make_library(code="mine", name="我的库")
    secret = make_library(code="secret-net", name="别人的库")
    grant_member(mine, user)

    api.force_authenticate(user=user)
    payload = api.get("/api/v1/knowledge/libraries/").json()
    rows = payload.get("results", payload)
    codes = {row["code"] for row in rows}
    assert "mine" in codes
    assert "secret-net" not in codes


def test_generated_js_assets_are_served():
    """静态资源能被 Django 找到（开发态由 staticfiles 提供）。"""
    from django.contrib.staticfiles import finders

    for asset in ("webui/kb-common.js", "webui/kb-files.js", "webui/kb-search.js"):
        assert finders.find(asset) is not None, asset


def test_logout_requires_post_and_redirects(admin_user):
    client = Client()
    client.login(username="admin", password="pw-not-a-secret-123")
    response = client.post(reverse("webui:logout"))
    assert response.status_code == 302
    assert client.get(reverse("webui:files")).status_code == 302


def test_login_lands_on_home_page(admin_user):
    """登录成功后默认落「首页」（/app/），只有带 ?next= 才回到原来那一页。"""
    client = Client()
    response = client.post(
        reverse("webui:login"),
        {"username": "admin", "password": "pw-not-a-secret-123"},
    )
    assert response.status_code == 302
    assert response["Location"] == reverse("webui:home") == "/app/"
    # 带 next 时仍然尊重用户原本要去的页面
    other = Client()
    response = other.post(
        reverse("webui:login") + "?next=" + reverse("webui:search"),
        {"username": "admin", "password": "pw-not-a-secret-123"},
    )
    assert response.status_code == 302
    assert response["Location"] == reverse("webui:search")


def test_already_logged_in_login_page_goes_to_home(admin_user):
    """已登录的人打开登录页 → 直接进首页（不再是文件管理页）。"""
    client = Client()
    client.login(username="admin", password="pw-not-a-secret-123")
    response = client.get(reverse("webui:login"))
    assert response.status_code == 302
    assert response["Location"] == "/app/"


def test_login_redirect_setting_points_at_home():
    """settings 里的登录跳转不要退回文件管理页，避免两处口径不一致。"""
    from django.conf import settings

    assert settings.LOGIN_REDIRECT_URL == "/app/"
