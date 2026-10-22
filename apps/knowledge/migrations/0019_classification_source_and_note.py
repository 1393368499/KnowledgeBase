"""新增「性质来源 / 判定说明」两个受控元数据，重刷触发器，并回填历史性质来源。

为什么把"加列"与"刷触发器"放在同一个迁移
----------------------------------------
与 ``0012`` / ``0018`` 同一机制：数据库触发器函数体是按
:func:`governance.triggers.locked_columns`（= 模型全部列 − 受控字段）生成的。
``classification_source`` / ``classification_note`` 一进
:data:`governance.registry.CONTROLLED_MUTABLE_FIELDS`，就**必须**同步重建触发器，
否则会出现"ORM 放行、触发器仍拒绝"的漂移 —— 表现是自动识别时写库直接报错，
而错误只出现在数据库层，代码看起来完全正常。

为什么必须回填（独立审查必改项）
--------------------------------
那两个新列在库里全是空串：``classification`` 已有值的行如果留着
``classification_source=""``，新代码会把它们当成"待识别、可以被自动覆盖"，
于是**自动识别会盖掉历史上的人工选择** —— 那是不可接受的静默篡改。
所以在这里把**所有已有非空 classification 的行**标成 ``manual``：
"来源不明"一律按"人工设置"处理（保守方向），只有确实空白的行才允许自动补识别。
（上传接口与受控 `classify_file` 现在也会显式写 ``manual``。）

**保护窗口（独立审查必改项）**：本迁移只新增**受控**元数据字段，
所以**不需要**"先撤掉全部守卫"那条路径。执行顺序是：

    AddField ×2（Django 迁移操作，各自在自己的事务里）
        → ① 按当前受控字段 ``create_guards()``（幂等；此后新列被排除在锁列外）
        → ② 回填 UPDATE（**包在事务里**，与①原子）

② 失败即回滚：因为①只是"重建同一批触发器"（幂等），回滚后数据库**仍有完整守卫**
（``0018`` 之后的状态），绝不会出现"原件/分段/审计表长期失去数据库保护"的窗口。
**全程不调用 ``drop_guards()``**，回退路径同样不撤全部守卫。
"""

from __future__ import annotations

from django.db import migrations, models, transaction

from governance import triggers
from governance.registry import CONTROLLED_MUTABLE_FIELDS

TABLE = "kb_ingested_file"

#: 本迁移新增的受控列（回退时用来重算锁列）
_ADDED_CONTROLLED = ("classification_source", "classification_note")


def _locked_for_schema(schema_editor) -> tuple[str, ...]:
    """按**该迁移点 schema** 算锁列（不能用运行时模型：回退时它还在）。

    取"历史表实际拥有的列 − 受控字段"。跨库通用：走 schema_editor 的连接做
    introspection（与 :func:`governance.triggers.existing_columns` 同一手段）。
    """
    columns = triggers.existing_columns(TABLE)
    controlled = set(
        CONTROLLED_MUTABLE_FIELDS.get("knowledge.IngestedFile", frozenset())
    )
    if not columns:  # pragma: no cover - 取不到就保守地交给默认实现
        return triggers.locked_columns()
    return tuple(sorted(column for column in columns if column not in controlled))


def add_fields_backfill_and_guards(apps, schema_editor):  # pragma: no cover - 迁移入口
    """加列后：在**同一个事务**里重建守卫 + 回填历史性质来源。

    ## 为什么必须整体放进一个事务（独立审查必改项）

    :func:`governance.triggers.create_guards` 的实现是"**先 DROP 全部守卫触发器，
    再逐条 CREATE**"。而本迁移 ``atomic = False``，若 ``create_guards()`` 在事务
    之外执行，每条 DDL 各自提交 —— 中途失败（或进程被停）就会停在
    "触发器已经 DROP、还没 CREATE"的状态：原件表、分段表、审计表**全部失去数据库
    保护**。那个窗口不可接受，所以：

    - ``create_guards()`` **与**回填 UPDATE 放进同一个 ``with transaction.atomic():``
      （迁移级 ``atomic=False`` 不影响内层 atomic，Django 允许嵌套；
      SQLite 与 PostgreSQL 的 DDL 都支持事务，失败即整体回滚到"改之前"的完整守卫状态）；
    - 全程不调用 ``drop_guards()``（那不是必需的，本迁移只新增**受控**字段）。

    回退路径同样处理（见 :func:`clear_sources_and_restore_guards`）。
    """
    with transaction.atomic(using=schema_editor.connection.alias):
        # ① 幂等重建：新列**不在**锁列里（它们是受控字段），其余列的保护照旧。
        #    与②同一事务：要么"守卫 + 回填"都在，要么都回到改之前的状态。
        triggers.create_guards()

        # ② 回填：与①原子。
        with schema_editor.connection.cursor() as cursor:
            # 已有性质 = 历史上有人（或旧接口）显式写入的选择 → 标为人工设置，
            # 自动识别不得覆盖。空白的行保持 ""（= 待识别，可被自动补识别）。
            cursor.execute(
                f"UPDATE {TABLE} SET classification_source = 'manual' "
                "WHERE classification IS NOT NULL AND classification <> ''"
            )
            # 新列给确定值，避免 NULL 让 "source != manual" 的语义含糊
            cursor.execute(
                f"UPDATE {TABLE} SET classification_source = '' "
                "WHERE classification_source IS NULL"
            )
            cursor.execute(
                f"UPDATE {TABLE} SET classification_note = '' "
                "WHERE classification_note IS NULL"
            )


def clear_sources_and_restore_guards(apps, schema_editor):  # pragma: no cover - 迁移入口
    """回退：在**同一个事务**里重算锁列、重建守卫、清空来源（随后才删列）。

    与正迁移同一口径：``create_guards(locked=...)`` 也会先 DROP 再 CREATE，
    所以它必须与后续 UPDATE 处于同一事务；失败即整体回滚，守卫保持完整。
    """
    locked = _locked_for_schema(schema_editor)
    with transaction.atomic(using=schema_editor.connection.alias):
        triggers.create_guards(locked=locked)
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(f"UPDATE {TABLE} SET classification_source = ''")
            cursor.execute(f"UPDATE {TABLE} SET classification_note = ''")


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("knowledge", "0018_guard_classification"),
    ]

    operations = [
        migrations.AddField(
            model_name="ingestedfile",
            name="classification_source",
            field=models.CharField(
                blank=True, default="", max_length=16, verbose_name="性质来源"
            ),
        ),
        migrations.AddField(
            model_name="ingestedfile",
            name="classification_note",
            field=models.CharField(
                blank=True, default="", max_length=300, verbose_name="性质判定说明"
            ),
        ),
        # 顺序关键：正迁移在加列后**不撤守卫**地重建 + 事务内回填；
        # 回退时先按回退后的锁列重建守卫，再清空来源，最后由 AddField 反向删列。
        migrations.RunPython(
            add_fields_backfill_and_guards, clear_sources_and_restore_guards
        ),
    ]
