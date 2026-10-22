"""在新增列之后，用**当前完整 schema** 刷新不可篡改触发器。

为什么需要这条迁移
------------------
``0002_immutability_triggers`` 只能按"当时历史 schema 的列"建触发器（否则从零安装
会在后续 ``ALTER TABLE`` 时炸，见那条迁移的说明）。但这样一来，0002 之后新增的
列（``parse_attempts`` / ``raw_path`` / ``raw_size_bytes`` / ``embedded_chunk_count``
/ ``vector_generation`` / ``vector_collection_name`` / ``vector_status`` …）就没有被
"受控字段之外一律锁死"这条规则覆盖到。

所以这里在**所有列都已存在之后**重新建一次触发器，用运行时模型算出的完整列集合。
这是"保护范围随 schema 增长"的正式机制：以后再加列，就再加一条同类的刷新迁移，
而不是回头去改 0002。
"""

from __future__ import annotations

from django.db import migrations

from governance import triggers


def refresh_guards(apps, schema_editor):
    """按当前 schema 重建触发器（列集合用运行时模型算，此时列已全部就位）。"""
    triggers.create_guards()


def restore_0002_guards(apps, schema_editor):
    """回退时退回 0002 的列集合，保证与 schema 一致（不留下引用不存在列的触发器）。

    用 importlib 按模块名取，而不是 ``from apps.knowledge.migrations import
    migration_0002_immutability_triggers``：迁移模块名以数字开头，
    ``import`` 语句里不能这么写；而且 ``apps.knowledge.migrations`` 这个包
    并没有把迁移模块挂成属性。用 ``importlib.import_module`` 最稳。
    """
    import importlib

    frozen = importlib.import_module(
        "apps.knowledge.migrations.0002_immutability_triggers"
    )

    triggers.create_guards(locked=frozen.LOCKED_COLUMNS)


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("knowledge", "0006_knowledgelibrary_next_generation_and_more"),
    ]

    operations = [
        migrations.RunPython(refresh_guards, restore_0002_guards),
    ]
