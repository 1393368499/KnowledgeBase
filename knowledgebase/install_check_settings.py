""""从零安装"校验专用配置：**强制 SQLite**，不受 `.env` 影响。

为什么单独一个模块：``knowledgebase/settings.py`` 会 ``load_dotenv(.env, override=True)``，
所以运维/校验脚本没法靠环境变量把 ``DB_ENGINE`` 从 postgres 改回 sqlite——
.env 会把它覆盖回去。这个限制对生产是好事（口径唯一），对"验证全新 SQLite 安装链"
就必须绕开，所以单独一个薄配置：

- 先正常加载 settings（拿到 INSTALLED_APPS / 路径 / 模型等全部口径）；
- 再把数据库**强制**换成一个临时 SQLite 文件（路径由 ``KB_SQLITE_TEST_PATH`` 给）。

它只用于 :command:`kbcheckinstall`，不会被生产或测试路径引用。
"""

from __future__ import annotations

import os

from knowledgebase.settings import *  # noqa: F403,F401

#: 由调用方传入的临时库路径；没给就用一个进程内内存库（不落盘）
_SQLITE_PATH = os.environ.get("KB_SQLITE_TEST_PATH") or ":memory:"

DATABASES = {  # noqa: F405
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": _SQLITE_PATH,
        "OPTIONS": {"timeout": 20},
    }
}

# 校验只关心迁移链与触发器，缓存/队列不认识也别去连
USE_REDIS_CACHE = False  # noqa: F405
CACHES = {  # noqa: F405
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "kb-install-check"}
}
CELERY_TASK_ALWAYS_EAGER = True  # noqa: F405
