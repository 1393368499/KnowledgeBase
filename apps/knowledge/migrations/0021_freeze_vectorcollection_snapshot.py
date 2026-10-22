"""一次性冻结兼容：为**历史登记行**补齐端点快照（``base_url``）。

背景：``base_url`` 是 0020 新增字段。历史行没有端点快照，而"该代的查询与增量写入
必须固定使用**它自己建立时**的服务"，所以本迁移只把**能严格证明**的端点冻结下来：

* 登记有 ``profile``，且该 profile 在当前配置表里；
* 登记的 ``model`` 与配置**完全一致**，``dimension`` **完全一致**；
* ``model_version`` 要么为空，要么与配置一致；
* 配置本身有 ``base_url``。

任何一条不满足的行**保持空**——之后由 ``embedding.profile_for_registration``
**显式拒绝**并提示"重建该代"，绝不猜一个端点填进去（那正是"旧代悄悄转向新服务"的来源）。

反向迁移：noop（回滚只丢快照，不影响任何不可篡改数据）。
"""

from __future__ import annotations

from django.db import migrations


def freeze_snapshots(apps, schema_editor):
    VectorCollection = apps.get_model("knowledge", "VectorCollection")
    try:
        from apps.knowledge.embedding import profiles

        table = profiles()
    except Exception:  # noqa: BLE001 - 配置非法时不在迁移里二次报错
        # 保持空快照：运行期会显式拒绝并提示修复配置，而不是拿一个"猜测的端点"顶上去
        return
    rows = VectorCollection.objects.exclude(profile="").filter(base_url="")
    for row in rows.only("pk", "profile", "embedding_model", "model_version", "dimension"):
        configured = table.get(row.profile)
        if configured is None or not configured.base_url:
            continue
        if configured.model != (row.embedding_model or ""):
            continue
        if int(configured.dimension) != int(row.dimension or 0):
            continue
        if row.model_version and configured.model_version != row.model_version:
            continue
        VectorCollection.objects.filter(pk=row.pk).update(base_url=configured.base_url)


class Migration(migrations.Migration):

    dependencies = [
        ("knowledge", "0020_vectorcollection_base_url"),
    ]

    operations = [
        migrations.RunPython(freeze_snapshots, migrations.RunPython.noop),
    ]
