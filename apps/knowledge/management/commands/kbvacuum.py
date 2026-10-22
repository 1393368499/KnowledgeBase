"""``manage.py kbvacuum``：清理**指向不存在的 collection**的向量登记行。

为什么需要这个命令
-----------------
向量登记表（``knowledge.VectorCollection``）是"库里有哪些代次"的事实来源，
但历史操作（重建失败、手删 collection、迁移）会留下**登记行指向已经不在 Qdrant
里的 collection** 的陈旧数据：

* 界面「索引状态」会把它算进"已登记集合"，看起来像还有一代索引；
* 整库重置虽然会顺手清理，但那条路径**必须先把整库资料重建成功**
  （真实模型可用），删一条陈旧登记不该被模型可用性绑住。

所以这里提供一条**独立、不依赖模型**的清理入口，判据是**可复核的事实**：
把登记行的名字拿到 Qdrant 里查 ``collection_exists`` —— 不存在才删登记行。
"登记存在、collection 也在"的行**一律不动**（那是真实索引，删登记就是丢事实）。

安全口径
--------
* **默认 dry-run**：不带 ``--apply`` 只列出将要删的行，不写一个字；
* Qdrant 不可达 / 列表读不出来时 **fail-closed**：什么都不删；
* 删除登记行**不碰任何向量数据**（本来就没有对应 collection），也不碰资料/原件；
* 每删一行追加审计事件 ``vector.collection_registration_removed``；
* Qdrant 里存在但**没有登记行**的 collection 只报告、绝不删（删向量不可逆，
  登记缺失是"事实没记全"，不是"数据多余"）。

用法：

    ./.venv/Scripts/python.exe manage.py kbvacuum
    ./.venv/Scripts/python.exe manage.py kbvacuum --apply
    ./.venv/Scripts/python.exe manage.py kbvacuum --apply --json
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError


def _collection_names(client) -> list[str]:
    """列出 Qdrant 里的 collection 名（读不出来就抛错，宁可失败也不误判）。"""
    try:
        response = client.get_collections()
    except Exception as exc:  # noqa: BLE001
        raise CommandError(
            f"Qdrant 不可达，拒绝清理（fail-closed，一个字都没改）：{exc}"
        ) from exc
    collections = getattr(response, "collections", None)
    if collections is None:  # 兼容 qdrant-client 的 result 包装
        result = getattr(response, "result", None)
        collections = getattr(result, "collections", None)
    if collections is None:
        raise CommandError("无法解析 Qdrant 的 collection 列表，拒绝清理（fail-closed）")
    return sorted(
        str(name) for name in (getattr(item, "name", None) for item in collections) if name
    )


class Command(BaseCommand):
    help = "清理指向不存在 collection 的向量登记行（默认 dry-run；不依赖模型）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true", help="真的删除陈旧登记行（默认只列出）"
        )
        parser.add_argument("--json", action="store_true", help="以 JSON 输出")

    def handle(self, *args, **options):
        from apps.knowledge.models import VectorCollection
        from apps.knowledge.vectorindex import build_client, collection_exists

        apply_changes = bool(options["apply"])
        client = build_client()
        try:
            registered = list(
                VectorCollection.objects.select_related("library").order_by("id")
            )
            existing = {
                record.name
                for record in registered
                if collection_exists(client, record.name)
            }
            registered_names = {record.name for record in registered}
            unregistered = [
                name for name in _collection_names(client) if name not in registered_names
            ]
        finally:
            client.close()

        stale = [record for record in registered if record.name not in existing]
        removed: list[dict] = []
        if apply_changes and stale:
            from apps.audit.services import append_event

            for record in stale:
                name, library_id = record.name, record.library_id
                deleted, _ = VectorCollection.objects.filter(pk=record.pk).delete()
                if not deleted:
                    continue
                append_event(
                    action="vector.collection_registration_removed",
                    actor_id=None,
                    resource_kind="vector_collection",
                    resource_key=name,
                    payload={
                        "collection": name,
                        "library_id": library_id,
                        "reason": "collection 在 Qdrant 里不存在（登记陈旧）",
                        "source": "manage.py kbvacuum",
                    },
                )
                removed.append({"name": name, "library_id": library_id})

        payload = {
            "registered": len(registered),
            "stale": [
                {
                    "name": record.name,
                    "library_id": record.library_id,
                    "is_active": record.is_active,
                }
                for record in stale
            ],
            "unregistered_collections": unregistered,
            "removed": removed,
            "applied": apply_changes,
        }

        if options["json"]:
            self.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
            return

        self.stdout.write(f"登记行：{len(registered)} 行")
        self.stdout.write(f"其中 collection 已不存在（陈旧）：{len(stale)} 行")
        for record in stale:
            owner = f"信息库 id={record.library_id}" if record.library_id else "未关联任何信息库"
            self.stdout.write(
                self.style.WARNING(
                    f"  - {record.name}（{owner}，当前生效={record.is_active}）"
                )
            )
        if unregistered:
            self.stdout.write(
                f"Qdrant 里存在但没有登记行的 collection：{len(unregistered)} 个"
                "（只报告，不删向量）"
            )
            for name in unregistered:
                self.stdout.write(f"  · {name}")
        if not stale:
            self.stdout.write(self.style.SUCCESS("没有需要清理的陈旧登记行"))
        elif apply_changes:
            self.stdout.write(
                self.style.SUCCESS(f"已删除 {len(removed)} 行登记（未触碰任何向量与资料）")
            )
        else:
            self.stdout.write(
                "dry-run：一个字都没改。确认无误后加 --apply 执行（只删登记行）。"
            )
        self.stdout.write("本命令不依赖模型服务：判据只有 Qdrant 里 collection 存不存在。")


__all__ = ["Command"]
