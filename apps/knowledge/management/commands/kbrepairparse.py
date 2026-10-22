"""修复部署配置后，一次性恢复因已知页数/长度问题失败的资料。"""
from django.core.management.base import BaseCommand
from django.db.models import Q

from apps.audit.services import append_event
from apps.knowledge.models import IngestedFile, ParseStatus
from apps.knowledge.pipeline import LeaseNotAcquired, PipelineError, process_file


class Command(BaseCommand):
    help = "恢复已修复的 OCR 页数、嵌入长度和 OCR 重试差异故障；默认仅列出目标"

    def add_arguments(self, parser):
        parser.add_argument("--execute", action="store_true")
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        errors = (Q(parse_error__contains="超过单份上限") |
                  Q(parse_error__contains="maximum context length is 512") |
                  Q(parse_error__contains="与本次解析结果不一致") |
                  Q(parse_error__contains="OCR 服务不可用") |
                  Q(parse_error__contains="OCR 输出达到长度上限"))
        targets = list(IngestedFile.objects.filter(errors, parse_status=ParseStatus.FAILED)
                       .order_by("id")[:options["limit"]])
        self.stdout.write(f"已知故障待恢复：{len(targets)} 份", ending="\n")
        self.stdout.flush()
        for record in targets:
            if not options["execute"]:
                self.stdout.write(f"#{record.pk}，已尝试 {record.parse_attempts} 次")
                continue
            record.refresh_from_db()
            if record.parse_status != ParseStatus.FAILED:
                self.stdout.write(f"[跳过] #{record.pk} 已由其他任务推进")
                continue
            append_event(action="file.parse_repair_requested", resource_kind="ingested_file",
                         resource_key=str(record.pk), payload={"reason": record.parse_error,
                         "content_sha256": record.content_sha256, "repair": "ocr200-complete-embedding-v1"})
            try:
                # 配置错误耗尽的历史次数保留，不清零；本次有明确审计的一次性修复不再消耗次数。
                result = process_file(record, consume_budget=False)
                self.stdout.write(f"[完成] #{record.pk}: {result.detail}")
            except LeaseNotAcquired:
                self.stdout.write(f"[跳过] #{record.pk} 正由其他任务处理")
            except PipelineError as exc:
                self.stdout.write(f"[失败] #{record.pk}: {exc}")
                if "OCR 服务不可用" in str(exc):
                    self.stdout.write("OCR 服务尚未恢复，停止本轮修复，其余资料保持原状态。")
                    break
            self.stdout.flush()
