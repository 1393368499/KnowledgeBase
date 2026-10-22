"""模板上下文：把静态资源版本号交给模板。

Django 默认**不会**把 settings 暴露给模板（``{{ settings.X }}`` 需要
context processor），所以这里显式提供 ``KB_ASSET_VERSION``——
模板用它给 ``/static/webui/kb-*.js`` 加 ``?v=``，实现可靠的缓存失效。
"""

from __future__ import annotations

from django.conf import settings


def asset_version(request):  # noqa: ARG001 - 接口要求接收 request
    return {"KB_ASSET_VERSION": getattr(settings, "KB_ASSET_VERSION", "")}
