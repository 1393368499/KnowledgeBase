"""安装数据库级不可篡改触发器。

用 ``RunPython`` 执行 DDL，并且 ``atomic = False``：

- PostgreSQL 上 ``CREATE TRIGGER`` 在事务里也能跑，但 ``CREATE OR REPLACE
  FUNCTION`` 混在同一个长事务里有回滚风险，独立执行更稳；
- SQLite 上 ``cursor.execute`` 直接执行 DDL 本来就不受 ORM 事务管理。

**关键：列集合必须冻结在"这个迁移点的 schema"上**
--------------------------------------------------
最初这里直接调 ``triggers.create_guards()``，而那个函数用**运行时模型**的字段
去拼触发器。后果是"从零安装"（全新 SQLite 库）会直接失败：

- 后续迁移（0004/0005/…）给 ``kb_ingested_file`` 加列时，SQLite 走
  ``remake_table``（建新表 → 拷数据 → 删旧表），这个过程会重新校验既有触发器；
- 如果触发器里引用了当时**还不存在**的列，SQLite 直接报
  ``error in trigger kb_guard_kb_ingested_file_update: no such column: OLD.<列名>``，
  整个迁移链就断在中间。

增量升级的环境不会暴露这个问题（触发器是更早、在列还少的时候建的），
所以它是典型的"只在干净安装时炸"的缺陷。

修法：迁移把**当时历史 schema 实际拥有的列**写死传进去（下面 ``LOCKED_COLUMNS``）。
以后再给 ``kb_ingested_file`` 加列，要由一条**专门的迁移**在加列之后刷新触发器
（见 ``0007_refresh_immutability_triggers.py``），让新列也进入保护范围。
"""

from __future__ import annotations

from django.db import migrations

from governance import triggers

#: 0001/0002 时点 ``kb_ingested_file`` 的列（受控字段除外）。
#: 这份清单是**历史事实的冻结**：不要因为以后加列就来改它——
#: 那会把"新列保护"提前塞进一个早于该列的迁移里，重新制造同类故障。
LOCKED_COLUMNS: tuple[str, ...] = (
    "id",
    "library_id",
    "original_name",
    "content_sha256",
    "size_bytes",
    "file_type",
    "directory_path",
    "classification",
    "customer",
    "department",
    "owner_id",
    "parse_status",
    "vector_point_id",
    "source",
    "created_at",
    "chunk_count",
)


def create_guards(apps, schema_editor):
    triggers.create_guards(locked=LOCKED_COLUMNS)


def drop_guards(apps, schema_editor):
    triggers.drop_guards()


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("knowledge", "0001_initial"),
        # knowledge.0001 依赖 audit.0003，所以这里必须点名 audit.0003 而不是 0001：
        # 迁移图不会自动等"被间接依赖方的更晚迁移"，点名才是显式保证。
        ("audit", "0003_auditchainhead"),
    ]

    operations = [
        migrations.RunPython(create_guards, drop_guards),
    ]
