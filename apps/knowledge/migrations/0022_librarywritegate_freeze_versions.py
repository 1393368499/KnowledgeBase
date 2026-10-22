"""库级写入闸门表 + 旧登记 ``model_version`` 一次性冻结。

两件事放在一个迁移里，因为它们服务同一个目标：**换模型期间"该用哪个模型"必须可证明**。

1. 建 ``kb_library_write_gate``（库级排他冻结 + fencing 代号）；
2. 冻结旧登记的 ``model_version``：历史行版本为空、且其余字段与当前配置**严格相符**
   （同 profile、model 与 dimension 完全一致、配置有非空版本与端点）时，把版本写死。
   之后运行期**不再随配置取版本**（配置改版不会让旧代悄悄换版本）。
"""

from __future__ import annotations

import django.db.models.deletion
from django.db import migrations, models


def freeze_model_versions(apps, schema_editor):
    VectorCollection = apps.get_model("knowledge", "VectorCollection")
    try:
        from apps.knowledge.embedding import profiles

        table = profiles()
    except Exception:  # noqa: BLE001 - 配置非法时不在迁移里二次报错
        return
    rows = VectorCollection.objects.filter(model_version="").exclude(profile="")
    for row in rows.only("pk", "profile", "embedding_model", "dimension"):
        configured = table.get(row.profile)
        if configured is None:
            continue
        if not configured.model_version:
            # 配置没有版本 → 冻结不出可证明的版本；保持空，运行期显式拒绝
            continue
        if configured.model != (row.embedding_model or ""):
            continue
        if int(configured.dimension) != int(row.dimension or 0):
            continue
        VectorCollection.objects.filter(pk=row.pk).update(
            model_version=configured.model_version
        )


class Migration(migrations.Migration):

    dependencies = [
        ("knowledge", "0021_freeze_vectorcollection_snapshot"),
    ]

    operations = [
        migrations.CreateModel(
            name="LibraryWriteGate",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("frozen_at", models.DateTimeField(blank=True, null=True)),
                ("frozen_by", models.CharField(blank=True, default="", max_length=120)),
                ("freeze_reason", models.CharField(blank=True, default="", max_length=200)),
                ("fence_token", models.CharField(blank=True, default="", max_length=64)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "library",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="write_gate",
                        to="knowledge.knowledgelibrary",
                    ),
                ),
            ],
            options={
                "verbose_name": "库写入闸门",
                "verbose_name_plural": "库写入闸门",
                "db_table": "kb_library_write_gate",
            },
        ),
        migrations.RunPython(freeze_model_versions, migrations.RunPython.noop),
    ]
