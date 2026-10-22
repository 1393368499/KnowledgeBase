"""精确解除某库的写冻结（重建崩溃/被强杀后的恢复入口）。

**不要**用"直接改数据库"的办法：那会跳过 fence_token 校验，可能把一次
**正在进行**的重建窗口误解除。本命令要求显式确认，并打印冻结原因/操作者/时间。
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.knowledge.models import KnowledgeLibrary
from apps.knowledge.writegate import WriteFrozen, gate_state, unfreeze


class Command(BaseCommand):
    help = "解除某信息库的写冻结（仅在确认重建已终止时使用）"

    def add_arguments(self, parser):
        parser.add_argument("--library", required=True, help="信息库 code")
        parser.add_argument(
            "--yes",
            action="store_true",
            help="确认执行（缺省只打印当前冻结状态，不做任何修改）",
        )

    def handle(self, *args, **options):
        code = options["library"]
        library = KnowledgeLibrary.objects.filter(code=code).first()
        if library is None:
            raise CommandError(f"信息库不存在：{code}")
        state = gate_state(library.pk)
        if not state["frozen"]:
            self.stdout.write(f"信息库 {code}（#{library.pk}）当前**未冻结**，无需操作")
            return
        self.stdout.write(
            f"信息库 {code}（#{library.pk}）处于冻结："
            f"原因={state['reason']!r} 操作者={state['frozen_by']!r} 时间={state['frozen_at']}"
        )
        if not options["yes"]:
            self.stdout.write(
                "只读检查结束。确认该重建已终止后，加 `--yes` 执行解除；"
                "若重建仍在进行，解除会让写入与构建并发，请勿执行。"
            )
            return
        # token 留空 = 运维显式解除（此时不再校验 fence_token）。
        # 仍然把 WriteFrozen 转成 CommandError：万一将来语义收紧（例如要求
        # 运维令牌），这里必须给出一句可执行的话，而不是 traceback。
        try:
            unfreeze(library.pk)
        except WriteFrozen as exc:
            raise CommandError(
                f"解除失败：{exc}；请核对冻结状态（可能已被新的重建接管），"
                "必要时先停止对应 worker 再重试"
            ) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"已解除信息库 {code} 的写冻结"
                "（闸门令牌已**再旋转一次**：旧重建 job 与旧 writer 手里的令牌全部失效，"
                "旧 job 醒来后的写入会被 assert_fence 拒绝）"
            )
        )
