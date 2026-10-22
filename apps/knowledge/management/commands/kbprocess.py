"""``manage.py kbprocess``：在进程内推进"待处理"的资料。

用途（用户 2026-10-13 指示："没有 worker 时应明确待处理，不能假装完成"）：

- 队列模式下上传会立刻返回 202 + 待处理；如果没有 Celery worker，
  就用这条命令在进程内把待处理的资料处理掉（本地验收最常用）；
- 也可以定时跑（cron / 计划任务），当作轻量 worker。

用法：

    ./.venv/Scripts/python.exe manage.py kbprocess              # 处理所有待处理/失败
    ./.venv/Scripts/python.exe manage.py kbprocess --limit 5
    ./.venv/Scripts/python.exe manage.py kbprocess --retry-failed
    ./.venv/Scripts/python.exe manage.py kbprocess --file 12
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.knowledge.models import IngestedFile, ParseStatus
from apps.knowledge.pipeline import PipelineError, process_file, retry_file
from apps.knowledge.writegate import WriteFrozen


class Command(BaseCommand):
    help = "在进程内处理待处理（可选：失败）资料；无 worker 时的推进方式"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=50, help="最多处理多少份（默认 50）")
        parser.add_argument("--file", type=int, default=0, help="只处理指定资料 id")
        parser.add_argument(
            "--retry-failed", action="store_true", help="把失败资料也一并重试"
        )

    def handle(self, *args, **options):
        from django.db.models import Q
        from django.utils import timezone

        from apps.knowledge.pipeline import LeaseNotAcquired

        # **只捞"没有租约或租约已过期"的资料**：否则这条命令会和正在跑的
        # worker 抢同一份资料（审查意见 7）。RUNNING 但租约仍有效的，跳过。
        now = timezone.now()
        free_lease = Q(parse_lease_token="") | Q(parse_lease_expires_at__lt=now) | Q(
            parse_lease_expires_at__isnull=True
        )
        statuses = [ParseStatus.PENDING, ParseStatus.RUNNING]
        if options["retry_failed"]:
            statuses.append(ParseStatus.FAILED)

        queryset = IngestedFile._base_manager.filter(parse_status__in=statuses).filter(free_lease)
        if options["file"]:
            queryset = IngestedFile._base_manager.filter(pk=options["file"]).filter(free_lease)
        records = list(queryset.order_by("id")[: options["limit"]])

        if not records:
            self.stdout.write("没有待处理的资料（或都正被其他 worker 持有租约）")
            return

        ok = failed = busy = frozen = 0
        for record in records:
            try:
                step = (
                    retry_file(record)
                    if record.parse_status == ParseStatus.FAILED
                    else process_file(record)
                )
            except LeaseNotAcquired as exc:
                busy += 1
                self.stdout.write(self.style.WARNING(f"[跳过] #{record.pk} {exc}"))
                continue
            except WriteFrozen as exc:
                # 库正在重建/换代：**不是这份资料的问题**，不要计入失败，
                # 也不要让 RuntimeError 冒成 traceback。整库都在冻结窗口内，
                # 后面的资料同样写不进去，直接停止本轮。
                frozen += 1
                self.stdout.write(
                    self.style.WARNING(
                        f"[冻结] #{record.pk} {record.original_name}：{exc}"
                        "；本库处于重建/换代窗口，本轮停止（等其结束后重跑）"
                    )
                )
                break
            except PipelineError as exc:
                failed += 1
                record.refresh_from_db()
                self.stderr.write(
                    self.style.WARNING(
                        f"[失败] #{record.pk} {record.original_name}：{exc}"
                    )
                )
                continue
            ok += 1
            self.stdout.write(
                self.style.SUCCESS(
                    f"[完成] #{record.pk} {record.original_name}：{step.detail}"
                )
            )

        self.stdout.write(
            f"处理完成 {ok} 份，失败 {failed} 份，因租约跳过 {busy} 份（共 {len(records)} 份）"
        )
        if frozen:
            self.stdout.write(
                self.style.WARNING(
                    "本轮因库处于重建/换代冻结窗口而中止；等重建结束后重跑本命令即可"
                )
            )
        if failed:
            # 失败是非零退出码：脚本化使用能立刻发现（但不是异常中断）
            raise SystemExit(1)


__all__ = ["Command"]
