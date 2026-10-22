"""占位迁移：保护逻辑已并入 `0012`（保留本文件只为已应用的库状态一致）。

`0013` 曾经承担"把 amount_wan 纳入不可变保护"。回退演练证明：把"加列"与
"刷新触发器"拆成两个迁移会导致顺序不可控（实测出现"正迁移后函数不含新列、
回退后反而含新列"的悬空引用）。

因此保护逻辑已**合并进 `0012_ingestedfile_amount_wan`**（同一迁移内保证
"正迁移加保护 / 回退先撤保护再删列"）。本迁移保留为一个**显式的空操作**，
这样已经应用过 `0013` 的数据库迁移状态仍然连续，不需要手工改表。

新库从零安装时：`0012` 加列并加保护 → `0013` 空操作，结果一致。
"""

from __future__ import annotations

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("knowledge", "0012_ingestedfile_amount_wan"),
    ]

    operations = [
        # 故意留空：见模块文档说明
    ]
