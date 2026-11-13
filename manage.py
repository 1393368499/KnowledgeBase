#!/usr/bin/env python
"""Django 命令行入口。"""

from __future__ import annotations

import os
import sys


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "knowledgebase.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "无法导入 Django。请先激活项目虚拟环境（.venv）。"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
