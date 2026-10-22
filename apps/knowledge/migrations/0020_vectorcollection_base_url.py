"""向量集合登记行增加**端点快照** ``base_url``。

为什么要加：换模型/换服务时，旧代次的查询与增量写入必须回到**该代次登记的**
模型、维度与端点；只按全局 active profile 取模型会导致"旧集合被新模型查询/写入"
的串模型事故。字段默认空串，历史行为不变（空快照时才允许后续用配置兜底，
且由 ``embedding.profile_for_registration`` 显式裁决，不会静默回落）。
"""

from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("knowledge", "0019_classification_source_and_note"),
    ]

    operations = [
        migrations.AddField(
            model_name="vectorcollection",
            name="base_url",
            field=models.CharField(blank=True, default="", max_length=300),
        ),
    ]
