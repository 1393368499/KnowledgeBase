"""给操作记录加「本次检索范围」。

背景：用户勾选的检索范围此前只作用于背景上下文，模型发起的工具调用
（``search_knowledge`` 等）执行时仍按"全部可读库"检索，勾了库也照样搜出
其他库的资料。范围必须随提案一起持久化，执行层才能照着收敛。
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("planner", "0007_documenttemplate_category_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="operationrecord",
            name="library_scope",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
