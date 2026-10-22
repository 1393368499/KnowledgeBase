"""测试包的最早期初始化：在任何 Django 导入之前把测试库开关落到环境里。

为什么需要这个文件（而不是只在 ``conftest.pytest_configure`` 里设）
------------------------------------------------------------------
``tests/conftest.py`` 顶层就 ``from apps.knowledge.models import ...``，
这会触发 ``django.setup()`` → 加载 ``config.test_settings`` → 决定用哪个数据库。
而 ``pytest_configure`` 是**之后**才跑的，那时候改环境变量已经晚了
（审查方实测：标志可能失效）。

pytest 收集测试时会先 import 包（``tests/__init__.py``），再 import ``conftest``，
所以这里是最早能拿到 ``--kb-db`` 值的地方——用 ``sys.argv`` 直接解析，
不依赖任何 pytest 内部 API。

注意变量名用 ``KB_PYTEST_DB``：``.env`` 里不存在这个名字，因此
``load_dotenv(..., override=True)`` 不会把它冲掉。
"""

from __future__ import annotations

import os
import sys


def _argv_option(name: str) -> str:
    """从命令行里取 ``--name=value`` 或 ``--name value``。"""
    argv = sys.argv
    for index, item in enumerate(argv):
        if item.startswith(f"{name}="):
            return item.split("=", 1)[1]
        if item == name and index + 1 < len(argv):
            return argv[index + 1]
    return ""


_requested = _argv_option("--kb-db")
if _requested:
    # **命令行优先于环境变量**：命令行是"这一次运行"的明确意图。
    #
    # 踩过的坑：shell/CI 里残留 ``KB_PYTEST_DB=sqlite`` 时，如果这里因为
    # "环境变量已存在"而跳过赋值，``--kb-db=postgres`` 就会被静默忽略——
    # 测试实际跑在 SQLite 上，却报告成"在 Postgres 上验证过"（危险的假结论）。
    os.environ["KB_PYTEST_DB"] = _requested
# 兼容直接设环境变量的用法（例如 CI 里 export KB_TEST_DB=postgres）
elif os.environ.get("KB_TEST_DB") and not os.environ.get("KB_PYTEST_DB"):
    os.environ["KB_PYTEST_DB"] = os.environ["KB_TEST_DB"]
