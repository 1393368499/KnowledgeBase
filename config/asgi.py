"""ASGI 入口：生产用 uvicorn / daphne 承载。

`uvicorn config.asgi:application --workers N`
注意：本地向量嵌入模式（qdrant-client path 模式）不支持多进程，多 worker 必须切 Qdrant server。
"""

from __future__ import annotations

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

application = get_asgi_application()
