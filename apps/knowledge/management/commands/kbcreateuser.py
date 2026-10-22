"""``manage.py kbcreateuser``：给浏览器验收建账号（比 createsuperuser 更适合脚本化）。

用法：

    ./.venv/Scripts/python.exe manage.py kbcreateuser --username demo --password demo-pass-123 --staff
    ./.venv/Scripts/python.exe manage.py kbcreateuser --username reader --password pw --library contract-archive --role reader

为什么需要它：
- ``createsuperuser`` 是交互式的，不方便在验收脚本里复制粘贴；
- 浏览器界面需要**普通用户**（用来验证"看不到无权信息库"），不全是管理员；
- 需要能顺手把用户加进某个信息库并给角色（库级权限是需求 5 的核心）。

安全：口令只从命令行/交互输入进来，**不落日志、不进代码、不写进任何文件**。
"""

from __future__ import annotations

import getpass

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.knowledge.models import KnowledgeLibrary, LibraryMember, MemberRole


class Command(BaseCommand):
    help = "创建一个验收用账号，可选加入某个信息库并指定角色"

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True)
        parser.add_argument(
            "--password",
            default="",
            help="不传则交互式输入（推荐，避免口令进 shell 历史）",
        )
        parser.add_argument("--email", default="")
        parser.add_argument("--staff", action="store_true", help="标记 is_staff")
        parser.add_argument("--superuser", action="store_true", help="标记 is_superuser")
        # 默认必须是 None（不是 ""）：argparse 的 append 动作会对默认值调 .append()，
        # 默认给字符串会在 Python 3.13 的 argparse 里直接
        # `AttributeError: 'str' object has no attribute 'append'`（踩过）。
        parser.add_argument("--library", default=None, help="信息库 code（可重复）", action="append")
        parser.add_argument(
            "--role",
            default=MemberRole.WRITER,
            choices=[choice.value for choice in MemberRole],
            help="加入信息库时的角色",
        )
        parser.add_argument(
            "--update",
            action="store_true",
            help="用户已存在时更新口令/权限（默认遇到已存在就报错，避免误改）",
        )

    def handle(self, *args, **options):
        User = get_user_model()
        username = options["username"]
        password = options["password"]

        existing = User.objects.filter(username=username).first()
        if existing is not None and not options["update"]:
            raise CommandError(
                f"用户 {username} 已存在。要更新请加 --update（避免误改既有账号）。"
            )

        if not password:
            password = getpass.getpass(f"请输入 {username} 的密码：")
            confirm = getpass.getpass("再输一次：")
            if password != confirm:
                raise CommandError("两次输入不一致")
        if len(password) < 8:
            raise CommandError("口令至少 8 位（Django 的 MinimumLengthValidator 要求）")

        with transaction.atomic():
            user = existing or User(username=username)
            user.email = options["email"]
            user.is_staff = bool(options["staff"]) or bool(options["superuser"]) or user.is_staff
            user.is_superuser = bool(options["superuser"]) or user.is_superuser
            user.set_password(password)
            user.save()

            memberships = []
            # ``options["library"]`` 正常来自 argparse 的 action="append"（列表），
            # 但**直接调 call_command("kbcreateuser", library="x")** 时会拿到字符串。
            # 如果不归一化，`for code in "contract-archive"` 会逐字符迭代，
            # 报出"信息库不存在：c"这种莫名其妙的错误（踩过）。
            requested = options["library"] or []
            if isinstance(requested, str):
                requested = [requested] if requested else []
            for code in requested:
                code = str(code).strip()
                if not code:
                    continue
                library = KnowledgeLibrary.objects.filter(code=code).first()
                if library is None:
                    raise CommandError(f"信息库不存在：{code}")
                LibraryMember.objects.update_or_create(
                    library=library, user=user, defaults={"role": options["role"]}
                )
                memberships.append(f"{code}:{options['role']}")

        created = "已更新" if existing is not None else "已创建"
        self.stdout.write(
            self.style.SUCCESS(
                f"{created}用户 {username}"
                f"（staff={user.is_staff} superuser={user.is_superuser}）"
                + (f"；加入信息库 {', '.join(memberships)}" if memberships else "")
            )
        )
        self.stdout.write(
            "浏览器入口：/app/files/ 与 /app/search/（用这个账号登录即可）"
        )


__all__ = ["Command"]
