"""新增 ``amount_wan``（金额/万元）字段，并**在同一个迁移里**纳入不可变保护。

为什么把"加列"和"刷新触发器"放在**同一个迁移**
------------------------------------------------
审查要求：金额字段必须纳入原有的不可变元数据约束（不能绕开保护），
并且**回退时要先撤掉新列的保护、再删列**，不能留下引用不存在列的触发器。

踩过的坑（实测）：把两件事拆成 `0012`(加列) + `0013`(刷新保护) 两个迁移后，
回退演练出现"**正迁移后函数不含新列、回退后反而含新列**"的悬空引用
（列已删、函数体仍引用它）。原因是保护逻辑依赖"当前模型"，
而模型与迁移进度并不总是一致，跨迁移就难以保证顺序。

放在**同一个迁移**里，顺序由 Django 保证：

- 正迁移：``AddField`` → ``RunPython(加保护)``；
- 回退：``RunPython.reverse(撤保护)`` → ``RemoveField.reverse(删列)``。

即"先撤保护、再删列"，天然不会留下悬空引用。
"""

from __future__ import annotations

from django.db import migrations, models

from governance.triggers import create_guards, drop_guards, locked_columns

#: 本迁移新增保护的列；回退时先从锁列里摘掉
_ADDED_COLUMNS = ("amount_wan",)


def add_protection(apps, schema_editor):
    """把新列纳入锁列（此时列已存在）。"""
    drop_guards()
    create_guards()


def remove_protection(apps, schema_editor):
    """**先撤掉新列的保护**，让触发器不再引用它，随后才由 RemoveField 删列。

    必须显式排除：``locked_columns()`` 从**当前模型**反推，而回退时代码里字段还在，
    直接用它会生成引用 ``amount_wan`` 的函数体，列一删就成了悬空引用。
    """
    remaining = tuple(col for col in locked_columns() if col not in _ADDED_COLUMNS)
    drop_guards()
    create_guards(locked=remaining)


class Migration(migrations.Migration):
    dependencies = [
        ("knowledge", "0011_chunk_delete_guard_uses_grant"),
    ]

    operations = [
        migrations.AddField(
            model_name="ingestedfile",
            name="amount_wan",
            field=models.FloatField(
                blank=True, db_index=True, null=True, verbose_name="金额（万元）"
            ),
        ),
        # 顺序关键：正迁移加保护；回退时先撤保护，再由上面的 AddField 反向删列
        migrations.RunPython(add_protection, remove_protection),
    ]
