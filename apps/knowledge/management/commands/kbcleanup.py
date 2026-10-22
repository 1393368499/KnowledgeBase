"""``manage.py kbcleanup``：清理**已过期的一次性凭证**（不碰业务与审计）。

为什么需要这个命令
-----------------
受控操作每次确认都会签发一次性凭证：

* ``planner.ActionTicket``（三次确认的每一步）；
* ``planner.DeleteGrant``（DELETE 触发器专用的"事务局部删除授权"）。

它们**过期后就没有任何作用**（校验一律先看 ``expires_at``），但会一直留在库里：
删过一批资料的实例里，这两张表会随确认次数线性增长，谁都没有入口去清。

**边界（很重要）**：
* 只删**已过期**且超过保留期的行；未过期的凭证一律不动；
* ``DeleteGrant`` 只在删除事务内可见，事务外留下的行都是**已结束事务的残留**，
  因此过期即可清；
* **不动**审计事件（``AuditEvent`` 是追加型不可篡改记录，永远不删）、
  **不动** ``OperationRecord``（操作历史）与 ``DeletionJob``（删除任务账本）——
  它们是"谁做过什么"的证据，不是缓存；
* 默认 dry-run，``--apply`` 才真删；每轮结果写日志。

用法：

    ./.venv/Scripts/python.exe manage.py kbcleanup
    ./.venv/Scripts/python.exe manage.py kbcleanup --apply
    ./.venv/Scripts/python.exe manage.py kbcleanup --apply --retention-days 7 --json
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "清理已过期的一次性确认凭证（ActionTicket / DeleteGrant；默认 dry-run）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true", help="真的删除（默认只统计）"
        )
        parser.add_argument(
            "--retention-days",
            type=int,
            default=30,
            help="过期后再保留多少天才清（默认 30 天，便于复盘）",
        )
        parser.add_argument("--json", action="store_true", help="以 JSON 输出")

    def handle(self, *args, **options):
        from datetime import timedelta

        from django.utils import timezone

        from apps.planner.models import ActionTicket, DeleteGrant

        apply_changes = bool(options["apply"])
        retention = max(0, int(options["retention_days"] or 0))
        cutoff = timezone.now() - timedelta(days=retention)

        tickets = ActionTicket.objects.filter(expires_at__lt=cutoff)
        grants = DeleteGrant.objects.filter(expires_at__lt=cutoff)
        counts = {"action_tickets": tickets.count(), "delete_grants": grants.count()}

        deleted = {"action_tickets": 0, "delete_grants": 0}
        if apply_changes:
            deleted["action_tickets"] = tickets.delete()[0]
            deleted["delete_grants"] = grants.delete()[0]

        payload = {
            "retention_days": retention,
            "cutoff": cutoff.isoformat(),
            "expired": counts,
            "deleted": deleted,
            "applied": apply_changes,
            "kept": {
                "audit_events": "永不清理（追加型不可篡改记录）",
                "operation_records": "永不清理（操作历史）",
                "deletion_jobs": "永不清理（删除任务账本）",
            },
        }

        if options["json"]:
            self.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
            return

        self.stdout.write(
            f"保留期 {retention} 天（早于 {cutoff.isoformat()} 且已过期的才清）"
        )
        self.stdout.write(f"已过期的一次性确认凭证：{counts['action_tickets']} 张 ActionTicket、"
                          f"{counts['delete_grants']} 张 DeleteGrant")
        if apply_changes:
            self.stdout.write(
                self.style.SUCCESS(
                    f"已删除 {deleted['action_tickets']} 张 ActionTicket、"
                    f"{deleted['delete_grants']} 张 DeleteGrant"
                )
            )
        else:
            self.stdout.write("dry-run：一个字都没改。确认无误后加 --apply 执行。")
        self.stdout.write(
            "未触碰：审计事件、操作记录、删除任务账本（它们是证据，不是缓存）。"
        )


__all__ = ["Command"]
