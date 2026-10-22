"""``manage.py kbclassify``：给**已入库但性质空白**的资料补做自动识别。

用户 2026-10-16 的要求
----------------------
「性质」改成按正文自动识别之后，上传流水线只覆盖"从今往后"的资料；库里**已经**
存在、性质还是空白的资料也要能补识别，并且与解析时走**同一条流水线**
（``apps.knowledge.classification``：先规则、后有界模型、失败如实报"待识别"）。

三条硬口径
----------

1. **只补空白**：只处理 ``classification == ""`` **且** ``classification_source != "manual"``
   的资料。人工设置过（含"人工清空"）的一律不碰 —— 人工优先；
2. **按当前执行者权限范围**：默认只有系统管理员能跑（不受限）；显式
   ``--actor <用户名>`` 时，只处理该用户是**库管理员**（``library:admin``，与受控
   写入 ``classify_file`` 同一门槛）的那些库；
3. **逐份如实输出**：每份一行，写清"已识别（值 + 依据）/ 待识别（失败原因）/ 失败"，
   绝不把失败汇总成一句"全部完成"。默认只预演不写库，要真正落库必须显式 ``--apply``。

用法：

    ./.venv/Scripts/python.exe manage.py kbclassify                      # 预演（只报告）
    ./.venv/Scripts/python.exe manage.py kbclassify --apply              # 真正补识别
    ./.venv/Scripts/python.exe manage.py kbclassify --apply --limit 50
    ./.venv/Scripts/python.exe manage.py kbclassify --apply --ids 12,15,20
    ./.venv/Scripts/python.exe manage.py kbclassify --apply --actor alice
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.knowledge.classification import (
    auto_classify_file,
    classify_content,
    file_content_text,
)
from apps.knowledge.models import (
    ClassificationSource,
    IngestedFile,
    KnowledgeLibrary,
    MemberRole,
)
from apps.knowledge.permissions import is_system_admin


class Command(BaseCommand):
    help = "给已入库且性质空白（非人工设置）的资料按正文补做自动识别"

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="真正写入性质（默认只预演/报告，不改任何一行）",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="显式预演：与默认行为一致只报告不写库（与 --apply 同时给会报错）",
        )
        parser.add_argument(
            "--ids", default="", help="只处理指定资料 id（逗号分隔），默认全部空白资料"
        )
        parser.add_argument("--limit", type=int, default=200, help="最多处理多少份（默认 200）")
        parser.add_argument(
            "--actor",
            default="",
            help="以该用户的权限范围执行（必须是他任库管理员的库）；不传=系统管理员不受限",
        )
        parser.add_argument("--library", default="", help="只处理该库 code")

    # ------------------------------------------------------------------ 权限 ----

    @staticmethod
    def _admin_library_ids(user) -> set[int]:
        """该用户担任**库管理员**的库 id（系统管理员不受限，由调用方另行处理）。"""
        return set(
            KnowledgeLibrary.objects.filter(
                members__user=user, members__role=MemberRole.ADMIN
            ).values_list("id", flat=True)
        )

    def handle(self, *args, **options):
        apply_changes = bool(options["apply"])
        if options["dry_run"] and apply_changes:
            raise CommandError("--dry-run 与 --apply 不能同时给（预演与写库是两件事）")

        actor = None
        actor_is_admin = False
        if options["actor"]:
            actor = get_user_model().objects.filter(username=options["actor"]).first()
            if actor is None:
                raise CommandError(f"用户不存在：{options['actor']}")
            # **显式管理员（staff/superuser）拥有全库范围**：他们通常没有
            # `LibraryMember` 行（成员表是"额外授权"，不是管理员身份的载体），
            # 只按成员表判定会把管理员误拒。审计里仍然记**被指定的那个 actor**，
            # 不是 "system" —— 谁下的命令必须能追溯。
            actor_is_admin = is_system_admin(actor)

        # 只补**空白**，且**排除人工设置**（含"人工清空"这一终态）：
        # 人工优先是硬约束，不能靠"命令里顺手改一下"来绕过。
        queryset = IngestedFile._base_manager.filter(classification="").exclude(
            classification_source=ClassificationSource.MANUAL
        )
        if options["library"]:
            queryset = queryset.filter(library__code=options["library"])
        if options["ids"]:
            try:
                wanted = [
                    int(item) for item in str(options["ids"]).split(",") if item.strip()
                ]
            except ValueError as exc:
                raise CommandError(f"--ids 只能是逗号分隔的整数：{exc}") from exc
            queryset = queryset.filter(pk__in=wanted)

        if actor is not None and not actor_is_admin:
            allowed = self._admin_library_ids(actor)
            if not allowed:
                raise CommandError(
                    f"用户 {options['actor']} 既不是系统管理员，也不是任何库的库管理员："
                    "无权补识别任何资料"
                    "（补识别写的是受控字段，门槛与受控操作 classify_file 一致）"
                )
            queryset = queryset.filter(library_id__in=allowed)

        records = list(queryset.order_by("id")[: max(0, int(options["limit"]))])
        if not records:
            self.stdout.write("没有需要补识别的资料（性质非空，或已由人工设置）")
            return

        mode = "写入" if apply_changes else "预演（不写库，加 --apply 才会写入）"
        if actor is None:
            scope = "系统管理员（不受限）"
        elif actor_is_admin:
            scope = f"用户 {options['actor']} 是系统管理员（全库范围）"
        else:
            scope = f"用户 {options['actor']} 的库管理员范围"
        self.stdout.write(f"待补识别 {len(records)} 份；模式：{mode}；权限范围：{scope}")

        applied = pending = failed = 0
        for record in records:
            value, source, note, error = self._classify_one(
                record, actor=actor, apply_changes=apply_changes
            )
            line = f"#{record.pk} {record.original_name}"
            if error:
                failed += 1
                self.stderr.write(self.style.ERROR(f"[失败] {line}：{error}"))
            elif value:
                applied += 1
                self.stdout.write(
                    self.style.SUCCESS(f"[已识别] {line} → 「{value}」（{source}）：{note}")
                )
            else:
                pending += 1
                self.stdout.write(self.style.WARNING(f"[待识别] {line} → 保持空白：{note}"))

        summary = (
            f"补识别完成：已识别 {applied} 份，待识别 {pending} 份，失败 {failed} 份"
            f"（共 {len(records)} 份）"
        )
        if not apply_changes:
            summary += "；本次为预演，未写入任何一行"
        self.stdout.write(summary)
        if failed:
            # 失败必须让脚本化使用看见（非零退出），但不当成异常中断
            raise SystemExit(1)

    # ------------------------------------------------------------------ 单份 ----

    def _classify_one(self, record: IngestedFile, *, actor, apply_changes: bool):
        """处理一份：返回 ``(值, 来源, 说明, 错误原因)``。**不抛异常**。"""
        try:
            if apply_changes:
                outcome = auto_classify_file(
                    record,
                    text=file_content_text(record),
                    actor=actor,
                    source_event="manage.kbclassify",
                )
                return outcome.value, outcome.source, outcome.note, ""
            # 预演：只做**纯判定**，绝不写库（连受控通道都不进）
            preview = classify_content(
                file_content_text(record), original_name=record.original_name
            )
            return preview.value, preview.source, preview.note, ""
        except Exception as exc:  # noqa: BLE001 - 单份失败不阻断整体，但必须如实报出
            return "", "", "", f"{type(exc).__name__}: {exc}"


__all__ = ["Command"]
