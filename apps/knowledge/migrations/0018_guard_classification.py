"""把「资料性质」纳入受控可变字段之后，用当前受控表重刷不可篡改触发器。

为什么需要这条迁移（与 ``0007`` 同一机制）
------------------------------------------
数据库触发器函数体是按 :func:`governance.triggers.locked_columns`
（= 模型全部列 − ``CONTROLLED_MUTABLE_FIELDS``）生成的。在这次改动之前
``classification`` 属于"锁定列"，所以**即使 ORM 放行**，一条
``UPDATE kb_ingested_file SET classification=…`` 也会被数据库拒绝。

反过来，只改登记表而不刷触发器，就会留下"登记表说可以改、数据库说不行"的漂移：
用户要求的「性质可以按行改」会表现为"点了没反应"（真正的原因在库里，不在界面）。

所以规则是：**受控字段集合一变，就补一条刷新迁移**（0007 的模块文档写了这条
机制），而不是回头去改 0002/0007。

回退
----
回退时把 ``classification`` 重新锁回去：用"当前模型列 − 去掉 classification 的
受控集合"作为锁定列，保证回退后的库状态与代码一致（不会留下"触发器不锁性质"
而代码已经回退的悬空状态）。
"""

from __future__ import annotations

from django.db import migrations

from governance import triggers
from governance.registry import CONTROLLED_MUTABLE_FIELDS


def _locked_without_classification() -> tuple[str, ...]:
    """只把 `classification` 放回锁定集合（其余按当前登记表算）。"""
    from apps.knowledge.models import IngestedFile

    controlled = set(
        CONTROLLED_MUTABLE_FIELDS.get("knowledge.IngestedFile", frozenset())
    )
    controlled.discard("classification")
    return tuple(
        field.column
        for field in IngestedFile._meta.concrete_fields
        if field.column not in controlled
    )


def refresh_guards(apps, schema_editor):  # pragma: no cover - 迁移入口
    """按当前受控表重建触发器（性质不再被锁）。"""
    triggers.create_guards()


def restore_locked_classification(apps, schema_editor):  # pragma: no cover - 迁移入口
    """回退：把性质重新锁回数据库层。"""
    triggers.create_guards(locked=_locked_without_classification())


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("knowledge", "0017_vectorcollection_retiring_at"),
    ]

    operations = [
        migrations.RunPython(refresh_guards, restore_locked_classification),
    ]
