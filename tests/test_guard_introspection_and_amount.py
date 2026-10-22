"""守卫列的跨库 introspection + 金额字段不可变 + 迁移回退回归。

覆盖本轮审查第 8 条：
- ``existing_columns`` 必须**跨数据库通用**（早先只写 information_schema，
  在 SQLite 上抛异常被兜底吞掉 → 退回按模型生成 → 仍会生成引用已删列的触发器）；
- ``_guardable`` / ``_effective_locked`` 要把不存在的列裁掉；
- ``amount_wan`` 属于**不可变元数据**：只能入库时写入，之后 DB 触发器与 ORM
  都要拒绝修改（不能绕开原有保护）；
- 迁移回退（0012 → 0011）后，触发器函数**不得**再引用已删除的列；
  再正迁移能恢复保护。
"""

from __future__ import annotations

import pytest
from django.db import connection, transaction

from governance import triggers
from governance.registry import CONTROLLED_MUTABLE_FIELDS

pytestmark = pytest.mark.django_db


# ---------------------------------------------------- 跨库 introspection ----


def test_existing_columns_matches_real_schema():
    """真实存在的列：必须能从后端 introspection 拿到（SQLite 与 PG 都要过）。"""
    columns = triggers.existing_columns(triggers.INGESTED_FILE_TABLE)
    assert columns, "取不到任何列：说明 introspection 在该后端不工作"
    assert "id" in columns
    # 模型里有的列，数据库迁移后必须都有
    for field in triggers.IngestedFile._meta.concrete_fields:
        assert field.column in columns, f"{field.column} 不在数据库里"


def test_existing_columns_missing_table_is_empty():
    assert triggers.existing_columns("no_such_table_for_test") == frozenset()


def test_effective_locked_drops_columns_absent_from_db(monkeypatch):
    """不存在的列必须被裁掉，避免生成引用已删列的 SQL。"""
    monkeypatch.setattr(
        triggers,
        "existing_columns",
        lambda table: frozenset({"id", "original_name"}),
    )
    assert triggers._effective_locked(("id", "original_name", "amount_wan")) == (
        "id",
        "original_name",
    )


def test_effective_locked_falls_back_when_introspection_fails(monkeypatch):
    """introspection 失败时保守放行（不裁剪），而不是把守卫清空。"""

    def boom(_table):
        raise RuntimeError("introspection unavailable")

    monkeypatch.setattr(triggers, "existing_columns", boom)
    assert triggers._effective_locked(("id", "amount_wan")) == ("id", "amount_wan")


# ------------------------------------------------ 金额字段不可变保护 ----


def test_amount_wan_is_treated_as_immutable_metadata():
    """金额属于"受控字段之外"的列 → 由补集规则锁定（不是受控可变）。"""
    controlled = CONTROLLED_MUTABLE_FIELDS.get("knowledge.IngestedFile", frozenset())
    assert "amount_wan" not in controlled
    assert "amount_wan" in triggers.locked_columns()


def test_amount_wan_update_is_rejected(library, make_file):
    """入库后修改金额必须被拒（数据库触发器层）。"""
    record = make_file(library)
    with pytest.raises(Exception):
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE kb_ingested_file SET amount_wan = 999 WHERE id = %s",
                    [record.pk],
                )


def test_amount_wan_is_written_on_ingest(library, make_file):
    """入库时允许写入（保护只针对 UPDATE，不影响 INSERT）。"""
    record = make_file(library, amount_wan=123.5)
    from apps.knowledge.models import IngestedFile

    stored = IngestedFile._base_manager.get(pk=record.pk)
    assert float(stored.amount_wan or 0) == pytest.approx(123.5)


# ----------------------------------------------------- 迁移回退演练 ----


def _guard_body() -> str:
    """取"资料表更新守卫"的实际定义文本（PG 取函数体；SQLite 取触发器 SQL）。"""
    with connection.cursor() as cursor:
        if connection.vendor == "postgresql":
            cursor.execute(
                "SELECT pg_get_functiondef(oid) FROM pg_proc WHERE proname = %s",
                [f"{triggers.PREFIX}ingested_file_update"],
            )
            row = cursor.fetchone()
            return row[0] if row else ""
        cursor.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name = %s",
            [f"{triggers.PREFIX}{triggers.INGESTED_FILE_TABLE}_update"],
        )
        row = cursor.fetchone()
        return row[0] if row else ""


def _column_exists(column: str) -> bool:
    return column in triggers.existing_columns(triggers.INGESTED_FILE_TABLE)


@pytest.mark.django_db(transaction=True)
def test_rollback_drops_protection_before_column():
    """回退 0012 后：列被删除，且守卫**不得**再引用它（否则是悬空引用）。

    **为什么不用 subprocess `manage.py migrate`**：那样会读 ``.env``，
    迁移到**正式开发库**上去，并且容易拼出 ``migrate knowledge knowledge``
    这类错命令。这里直接在**当前测试连接**上用 ``MigrationExecutor`` 演练，
    结束后在 ``finally`` 里恢复到最新，SQLite 与 PostgreSQL 都真跑。
    """
    from django.db.migrations.executor import MigrationExecutor

    def migrate_backwards(target: str) -> None:
        # 每次都重新装载图，否则回退后再前进会用旧的图。
        # 注意：MigrationExecutor 只认**完整节点名**（"0011_xxx"），
        # 传 "0011" 会抛 NodeNotFoundError。
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate([("knowledge", target)])

    def migrate_to_latest() -> None:
        """恢复到**全部应用**的最新状态。

        **为什么不能只恢复 knowledge（真实缺陷，2026-10-16 修）**：
        ``executor.migrate([("knowledge", target)])`` 的目标集里只有 knowledge 时，
        Django 会把**不在这条目标路径上的其它应用迁移一并回退**——实测把
        ``planner.0004_libraryrebuildrun`` / ``0005_*`` 也回退了。旧实现这里只把
        knowledge 的叶子迁回来，于是同一测试库里 ``planner_libraryrebuildrun``
        永久缺失，**后续任何用到该表的用例都会炸**（独立 Linux 全量跑到
        ``test_cleanup_vs_activate_and_rollback_on_real_pg_threads`` 时才暴露）。
        正确做法：恢复**全图**叶子，并在恢复后校验关键表确实回来了。
        """
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())
        assert _planner_tables_present(), (
            "恢复到最新后 planner 的表仍缺失 —— 迁移恢复不完整，会污染同库后续用例"
        )

    def _planner_tables_present() -> bool:
        """planner 的关键表在不在（本轮回退会连带回退它，所以要显式确认）。"""
        from django.db import connection as _connection

        with _connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM planner_libraryrebuildrun LIMIT 1"
            )
        return True

    assert _column_exists("amount_wan"), "前置条件：amount_wan 应当存在"
    assert "amount_wan" in _guard_body(), "前置条件：守卫应当保护 amount_wan"

    try:
        migrate_backwards("0011_chunk_delete_guard_uses_grant")
        assert not _column_exists("amount_wan"), "回退后列应当被删除"
        assert "amount_wan" not in _guard_body(), (
            "回退后守卫仍在引用已删除的列 —— 悬空引用，之后的 UPDATE 都会报错"
        )
    finally:
        # 无论断言是否失败，都必须恢复到最新，避免污染同一测试库的其它用例
        migrate_to_latest()

    assert _column_exists("amount_wan")
    assert "amount_wan" in _guard_body(), "恢复后应当重新受保护"
