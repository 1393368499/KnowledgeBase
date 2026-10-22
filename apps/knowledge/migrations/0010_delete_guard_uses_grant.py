"""把资料表的 DELETE 触发器升级为「授权感知」版本（P4 受控删除）。

背景
----
原触发器对 ``kb_ingested_file`` 的 DELETE 是**无条件拒绝**。P4 要求受控删除，
但审查明确否掉了"临时摘掉触发器"的做法——摘/建之间存在**全局窗口**，
任何并发请求都能在窗口期删除任意资料。

所以改成：触发器自己去查一张**授权表** ``kb_delete_grant``，
只有"同一事务签发、未核销、未过期、目标精确匹配"的授权存在时才放行。

- 授权行由服务层在**删除的同一个事务**里插入；
- 其他事务在提交前看不到它（READ COMMITTED），回滚则授权一并消失；
- 因此这是一条**事务局部**通道，没有全局窗口，也无法跨目标挪用。

依赖：``planner.0002_deletionjob_deletegrant`` 创建了 ``kb_delete_grant``。
"""

from __future__ import annotations

from django.db import migrations

from governance.triggers import create_guards, drop_guards


def upgrade_delete_guard(apps, schema_editor):
    """按**当前 schema 的真实列**重建全部守卫（此时所有列都已存在）。"""
    drop_guards()
    create_guards()


def restore_previous_shape(apps, schema_editor):
    """回滚：同样重建（旧版本的触发器代码已不在本文件里）。

    这里不做"还原成无条件拒绝"的手写 SQL——那会让回滚后的行为与代码不一致。
    回滚到本迁移之前请一并回滚代码版本（迁移与代码同源）。
    """
    drop_guards()
    create_guards()


class Migration(migrations.Migration):
    dependencies = [
        ("knowledge", "0009_vectorcollection_build_error_and_more"),
        # 授权表必须先存在，触发器才能引用它
        ("planner", "0002_deletionjob_deletegrant"),
    ]

    operations = [
        migrations.RunPython(upgrade_delete_guard, restore_previous_shape),
    ]
