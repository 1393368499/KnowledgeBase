"""``manage.py kbpatrol``：立即执行一次**只读**巡检，并把结论留痕。

为什么需要这个命令
------------------
* 周期巡检（``knowledge.patrol``）默认 ``KB_CELERY_BEAT_PATROL_SECONDS``（900）秒一次，
  验收与排障时不想干等；
* 界面上「解析看门狗 / 结果校验」两张状态卡的判定来源就是巡检留痕
  （``knowledge.PatrolRun``）—— 新部署后第一次巡检跑起来之前，它们只能如实显示
  "还没有巡检记录"。这个命令就是那第一枪。

口径
----
* **同一实现**：直接调用 ``knowledgebase.celery.patrol``，不另写一套检查逻辑；
* **只读**：不修改任何资料、不释放/抢占租约、不写向量；
* 结论既写日志也写留痕（``source="manual"``，与 beat 的 ``beat`` 区分开）。

用法：

    ./.venv/Scripts/python.exe manage.py kbpatrol
    ./.venv/Scripts/python.exe manage.py kbpatrol --limit 500
    ./.venv/Scripts/python.exe manage.py kbpatrol --json
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "立即执行一次只读巡检并留痕（「解析看门狗 / 结果校验」的真实来源）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit", type=int, default=200, help="最多检查多少份资料（默认 200）"
        )
        parser.add_argument("--json", action="store_true", help="以 JSON 输出")

    def handle(self, *args, **options):
        from apps.knowledge.integrity import patrol_snapshot, verification_snapshot
        from knowledgebase.celery import patrol as patrol_task

        limit = max(1, int(options["limit"] or 200))
        result = patrol_task(limit=limit, source="manual")
        payload = {
            "patrol": result,
            "watchdog": patrol_snapshot(),
            "verification": verification_snapshot(),
        }

        if options["json"]:
            self.stdout.write(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str)
            )
            return

        counts = dict(result.get("counts") or {})
        self.stdout.write(f"本批检查资料：{result.get('checked')} 份（上限 {limit}，按批轮转）")
        for name, label in (
            ("failed_exhausted", "重试上限用尽"),
            ("stalled", "疑似停滞"),
            ("vector_incomplete", "向量不完整"),
            ("blob_missing", "原件缺失/哈希不符"),
        ):
            count = int(counts.get(name) or 0)
            line = f"  {label}：{count}"
            if count:
                self.stdout.write(self.style.WARNING(line))
            else:
                self.stdout.write(line)
        total = sum(int(v or 0) for v in counts.values())
        if total:
            self.stdout.write(self.style.WARNING(f"巡检结论：发现 {total} 项待处理"))
        else:
            self.stdout.write(self.style.SUCCESS("巡检结论：本批未发现异常"))
        self.stdout.write(
            "只读巡检：未修改任何资料；结论已留痕（source=manual），"
            "界面「解析看门狗 / 结果校验」即以此为依据。"
        )


__all__ = ["Command"]
