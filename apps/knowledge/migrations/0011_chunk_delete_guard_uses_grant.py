"""分段删除守卫也改为「授权感知」（P4 级联删除的必需修复）。

背景（审查指出的实质缺陷）
--------------------------
上一版只把**资料表**的 DELETE 触发器改成查 ``kb_delete_grant``，
``kb_document_chunk`` 的 DELETE 仍是**无条件拒绝**。而受控删除资料时会
**级联**删分段——于是删除永远会在级联这一步被触发器挡住：
授权通道开了，实际却删不掉。

修法：分段删除也按"同一事务里针对 ``OLD.file_id`` 的授权"放行，
与资料本体共用**同一张授权表、同一个目标标识**（资料的 id）。
授权是给"这份资料"的，删本体与删它的分段都凭它放行。

**审计表的删除仍然无条件拒绝**（append-only 哈希链，任何情况下都不删）。
"""

from __future__ import annotations

from django.db import migrations

from governance.triggers import create_guards, drop_guards


def rebuild_guards(apps, schema_editor):
    drop_guards()
    create_guards()


def restore_previous(apps, schema_editor):
    # 回滚同样重建（触发器代码与迁移同源，不做手写 SQL 造成的行为漂移）
    drop_guards()
    create_guards()


class Migration(migrations.Migration):
    dependencies = [
        ("knowledge", "0010_delete_guard_uses_grant"),
        ("planner", "0002_deletionjob_deletegrant"),
    ]

    operations = [
        migrations.RunPython(rebuild_guards, restore_previous),
    ]
