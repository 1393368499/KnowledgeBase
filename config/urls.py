"""根路由。"""

from __future__ import annotations

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include("apps.knowledge.urls")),
    path("api/v1/", include("apps.planner.urls")),
    path("api/v1/", include("apps.audit.urls")),
    path("api/v1/health/", include("config.health_urls")),
    # 浏览器验收界面：/app/login/ 登录，/app/files/ 文件管理，/app/search/ 检索
    path("app/", include("apps.webui.urls")),
]
