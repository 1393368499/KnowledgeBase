"""``manage.py guards``：不可篡改触发器的自检与维护。

用法：

    ./.venv/Scripts/python.exe manage.py guards            # 只检查（默认）
    ./.venv/Scripts/python.exe manage.py guards --ensure   # 缺了就补建

为什么维护动作**只给命令行**：
受控删除通道（三次确认 + 一次性凭证）在 P4 落地，它会在**事务范围内**做精确目标
授权；在那之前，任何"运行时把触发器摘掉"的能力都不允许出现在请求路径上。
命令行是运维/迁移工具，不进 API，也不在 AI 工具白名单里——这条边界写在
``docs/architecture-review.md`` 的 D3 与 ``docs/codex-review.md`` 的 P4 要求里。
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import connection

from governance.registry import CONTROLLED_MUTABLE_FIELDS
from governance.triggers import create_guards, drop_guards, guards_present


class Command(BaseCommand):
    help = "检查（或补建）数据库层不可篡改触发器"

    def add_arguments(self, parser):
        parser.add_argument(
            "--ensure",
            action="store_true",
            help="缺少触发器时补建（幂等；不做删除动作，避免给并发请求开窗口）",
        )
        parser.add_argument(
            "--json", action="store_true", help="以 JSON 输出，方便脚本消费"
        )

    def handle(self, *args, **options):
        vendor = connection.vendor
        present = guards_present()
        rows = self._rows()

        if options["ensure"] and not present:
            created = create_guards()
            present = guards_present()
            rows = self._rows()
            self.stdout.write(f"已补建 {created} 条触发器")

        if options["json"]:
            import json

            self.stdout.write(
                json.dumps(
                    {
                        "vendor": vendor,
                        "guards_present": present,
                        "triggers": rows,
                        "controlled_fields": sorted(
                            CONTROLLED_MUTABLE_FIELDS.get("knowledge.IngestedFile", [])
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            self.stdout.write(f"数据库后端：{vendor}")
            self.stdout.write(f"触发器就位：{'是' if present else '否'}")
            for name in rows:
                self.stdout.write(f"  kb_guard_{name}")
            self.stdout.write(
                "受控可写字段（触发器对这些列的 UPDATE 放行）："
                + ", ".join(
                    sorted(CONTROLLED_MUTABLE_FIELDS.get("knowledge.IngestedFile", []))
                )
            )

        if not present:
            self.stderr.write(
                "警告：触发器不完整。测试/迁移环境下可加 --ensure 补建；"
                "生产环境请排查迁移 0002_immutability_triggers 是否执行过。"
            )
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("不可篡改触发器已全部就位"))

    def _rows(self) -> list[str]:
        """列出当前库里所有 kb_guard_* 触发器（按名字）。"""
        from governance.triggers import PREFIX

        with connection.cursor() as cursor:
            if connection.vendor == "sqlite":
                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE %s"
                    " ORDER BY name",
                    [f"{PREFIX}%"],
                )
            elif connection.vendor == "postgresql":
                cursor.execute(
                    "SELECT tgname FROM pg_trigger WHERE tgname LIKE %s ORDER BY tgname",
                    [f"{PREFIX}%"],
                )
            else:  # pragma: no cover - 其它后端没有触发器层
                return []
            return [row[0][len(PREFIX) :] for row in cursor.fetchall()]


__all__ = ["Command", "drop_guards"]
