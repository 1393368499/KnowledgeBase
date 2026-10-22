"""``manage.py kbcheckinstall``：在**全新临时库**上跑完整迁移链，验证"从零安装"。

为什么需要这条命令
------------------
有一个只在干净安装时才会炸的缺陷类型：迁移用**运行时模型**的列去建数据库触发器，
而那个列要到更晚的迁移才存在。增量升级的环境永远碰不到（触发器早在列还少时就建好了），
但全新 SQLite 库在后续 ``ALTER TABLE``（remake_table）时会直接失败：

    sqlite3.OperationalError: error in trigger kb_guard_kb_ingested_file_update:
    no such column: OLD.<未来的列>

这个缺陷真的发生过。所以把它固化成一条可复制的命令：任何改动迁移/触发器的提交，
都应该能跑通这条命令。

用法：

    ./.venv/Scripts/python.exe manage.py kbcheckinstall
    ./.venv/Scripts/python.exe manage.py kbcheckinstall --keep   # 保留临时库供排查

命令**只在临时目录里建库**，绝不碰项目里的 db/kb.sqlite3 或 PostgreSQL 主库。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "在全新临时 SQLite 库上跑完整迁移链，验证从零安装与触发器就位"

    def add_arguments(self, parser):
        parser.add_argument("--keep", action="store_true", help="保留临时库文件并打印路径")

    def handle(self, *args, **options):
        import os

        from config.paths import PROJECT_ROOT

        tmpdir = tempfile.mkdtemp(prefix="kb-install-check-")
        db_path = Path(tmpdir) / "fresh.sqlite3"

        # 用子进程跑，确保 settings/连接句柄完全是新的（同一个进程里换库不可靠）
        env = dict(os.environ)
        env["KB_SQLITE_TEST_PATH"] = str(db_path)
        env["DJANGO_DEBUG"] = "1"
        # 关键：用"强制 SQLite"的校验配置。config.settings 会 load_dotenv(override=True)，
        # 单靠 DB_ENGINE 环境变量改不动 .env 里的 postgres 口径。
        env["DJANGO_SETTINGS_MODULE"] = "config.install_check_settings"
        code = (
            "import django;"
            "django.setup();"
            "from django.core.management import call_command;"
            "call_command('migrate', verbosity=0, interactive=False);"
            "from django.db import connection;"
            "from governance.triggers import guards_present, locked_columns;"
            "print('vendor=' + connection.vendor);"
            "print('guards_present=' + str(guards_present()));"
            "print('locked_columns=' + ','.join(locked_columns()))"
        )

        import subprocess
        import sys

        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(PROJECT_ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        output = (completed.stdout or "") + (completed.stderr or "")
        if completed.returncode != 0:
            self.stderr.write(output[-4000:])
            raise SystemExit(f"从零安装失败（临时库 {db_path}）")

        vendor = ""
        guards = ""
        for line in output.splitlines():
            if line.startswith("vendor="):
                vendor = line.split("=", 1)[1]
            if line.startswith("guards_present="):
                guards = line.split("=", 1)[1]
        self.stdout.write(self.style.SUCCESS("从零安装迁移链跑通"))
        self.stdout.write(f"  临时库：{db_path}")
        self.stdout.write(f"  真实后端（connection.vendor）：{vendor}")
        self.stdout.write(f"  不可篡改触发器就位：{guards}")
        if vendor != "sqlite":
            self.stderr.write(
                "警告：这不是 SQLite。请检查 KB_SQLITE_TEST_PATH / DB_ENGINE 是否被 .env 覆盖。"
            )
        if guards != "True":
            raise SystemExit("触发器未就位：从零安装的守卫不完整")

        if options["keep"]:
            self.stdout.write(f"已保留临时库：{db_path}")
        else:
            import shutil

            shutil.rmtree(tmpdir, ignore_errors=True)
            self.stdout.write("已清理临时库")

        # 报告**当前**连接后端，避免"以为在测 SQLite、其实连着 Postgres"
        self.stdout.write(f"当前 shell 的连接后端：{connection.vendor}")


__all__ = ["Command"]
