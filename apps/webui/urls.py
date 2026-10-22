from __future__ import annotations

from django.urls import path

from .views import (
    KbLoginView,
    KbLogoutView,
    agent_page,
    assistant_page,
    docs_page,
    files_page,
    knowledge_page,
    search_page,
    settings_page,
    tasks_page,
    templates_page,
)

app_name = "webui"

urlpatterns = [
    path("login/", KbLoginView.as_view(), name="login"),
    path("logout/", KbLogoutView.as_view(), name="logout"),
    # 首页：AI 会话 + 操作提案确认 + 派生文档侧栏（P3/P5）
    path("", assistant_page, name="home"),
    path("assistant/", assistant_page, name="assistant"),
    path("files/", files_page, name="files"),
    path("search/", search_page, name="search"),
    # 按原型 NAV 顺序补齐的页面（全部走真实 API，无演示数据）
    path("agent/", agent_page, name="agent"),
    path("settings/", settings_page, name="settings"),
    path("knowledge/", knowledge_page, name="knowledge"),
    path("templates/", templates_page, name="templates"),
    path("tasks/", tasks_page, name="tasks"),
    path("docs/", docs_page, name="docs"),
]
