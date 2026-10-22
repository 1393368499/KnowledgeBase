"""``manage.py kbrebuild``：初始化 / 重建一个信息库的向量索引。

为什么这条命令是必需的（"建库 → 首上传 → 处理"链路的一环）：

全新库 ``active_generation = 0``、没有任何 collection 登记行，
此时第一次上传**无处可写**——所以必须先有一次"索引初建"。

流程（失败安全，任何一步失败都保留旧代次）：

1. 为新代次建 collection 并登记（旧代次不动，检索照旧）；
2. 把库里所有未归档资料重新解析 / 嵌入到新代次；
3. 全部通过真实 Qdrant 逐段 coverage 校验后，原子激活；
4. 任何失败都返回详情、**不切换**，修好后可重跑。

用法：

    ./.venv/Scripts/python.exe manage.py kbrebuild --library contract-archive
    ./.venv/Scripts/python.exe manage.py kbrebuild --all
    ./.venv/Scripts/python.exe manage.py kbrebuild --library x --profile bge1024
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from apps.knowledge.embedding import active_profile, get_profile
from apps.knowledge.models import KnowledgeLibrary
from apps.knowledge.pipeline import rebuild_library
from apps.knowledge.writegate import WriteFrozen


class Command(BaseCommand):
    help = "初始化或重建信息库的向量索引（建新代次 → 全量重建 → 校验 → 原子激活）"

    def add_arguments(self, parser):
        parser.add_argument("--library", action="append", default=[], help="库 code（可重复）")
        parser.add_argument("--all", action="store_true", help="重建所有启用中的库")
        parser.add_argument("--profile", default="", help="嵌入 profile（默认 active）")
        parser.add_argument("--json", action="store_true", help="以 JSON 输出")

    def handle(self, *args, **options):
        if not options["all"] and not options["library"]:
            raise CommandError("请指定 --library <code>（可重复）或 --all")

        libraries = list(
            KnowledgeLibrary.objects.filter(is_active=True).order_by("id")
            if options["all"]
            else KnowledgeLibrary.objects.filter(code__in=options["library"]).order_by("id")
        )
        if not libraries:
            raise CommandError("没有匹配的信息库")

        profile = get_profile(options["profile"]) if options["profile"] else active_profile()
        results = []
        for library in libraries:
            try:
                report = rebuild_library(library, profile=profile)
            except WriteFrozen as exc:
                # **冻结被拒不是"命令写错了"**：库正在重建/换代，或还有活跃租约。
                # 用 CommandError 明确报出原因与处置（等它结束 / kbunfreeze），
                # 绝不让 RuntimeError 冒成一段无法解读的 traceback。
                raise CommandError(
                    f"[{library.code}] 写入被拒绝（库处于冻结窗口）：{exc}"
                    "；请等当前重建结束，或用 manage.py kbunfreeze 查看/解除冻结后重试"
                ) from exc
            results.append(report)
            if options["json"]:
                continue
            status = "已激活" if report["activated"] else "未切换（保留旧代次）"
            self.stdout.write(
                f"[{library.code}] 代次 {report['generation']} / "
                f"{report['collection']} — {status}"
            )
            self.stdout.write(
                f"    资料 {report['files_processed']}/{report['files_total']} 份重建成功"
            )
            for item in report["files_failed"]:
                self.stderr.write(
                    self.style.WARNING(
                        f"    失败：#{item['file_id']} {item['name']} — {item['error']}"
                    )
                )
            if not report["activated"]:
                self.stderr.write(
                    self.style.WARNING(f"    原因：{report.get('reason', '')}")
                )

        if options["json"]:
            self.stdout.write(json.dumps(results, ensure_ascii=False, indent=2))

        if any(not item["activated"] for item in results):
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("索引重建完成并已激活"))


__all__ = ["Command"]
