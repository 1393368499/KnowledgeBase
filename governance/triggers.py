"""数据库级不可篡改触发器。

为什么还要有这一层：ORM 守卫只在"走 Django 模型"时生效。任何人拿到数据库连接
（``cursor.execute("UPDATE kb_ingested_file SET original_name=…")``、psql、备份
脚本、别的语言的微服务）都能绕过它。这里把约束下沉到数据库本身。

三张受保护表：

======================  ====================  =================================
表                      UPDATE                DELETE
======================  ====================  =================================
``kb_ingested_file``    除受控字段外全部锁定   一律禁止
``kb_document_chunk``   一律禁止               一律禁止
``kb_audit_event``      一律禁止               一律禁止
======================  ====================  =================================

"受控字段"来自 :data:`governance.registry.CONTROLLED_MUTABLE_FIELDS`，是唯一真源，
所以不会出现"登记表放行了、触发器还在挡"的漂移。

受控删除通道（三次确认）通过 :func:`drop_guards` / :func:`create_guards` 临时摘掉
触发器；这一步必须发生在 :func:`governance.registry.privileged_channel` 里，
而且调用方要写审计事件。

SQLite 与 PostgreSQL 都支持 ``CREATE TRIGGER`` / ``DROP TRIGGER`` 与
``RAISE(ABORT, …)`` / ``RAISE EXCEPTION``，两边语法各写一份。
"""

from __future__ import annotations

from collections.abc import Iterable
import logging

from django.db import connection, transaction

from apps.audit.models import AuditEvent
from apps.knowledge.models import DocumentChunk, IngestedFile
from governance.registry import CONTROLLED_MUTABLE_FIELDS

logger = logging.getLogger(__name__)

INGESTED_FILE_TABLE = IngestedFile._meta.db_table
CHUNK_TABLE = DocumentChunk._meta.db_table
# 从模型取，不写死字符串：模型表名一改，触发器跟着走，不会出现"守着一张不存在的表"
AUDIT_TABLE = AuditEvent._meta.db_table

# 触发器名的前缀，方便按前缀批量摘除/重建
PREFIX = "kb_guard_"

#: 当前模型下"受控字段"以外的所有列（每次调用现算，避免 import 期快照过期）
def locked_columns() -> tuple[str, ...]:
    controlled = CONTROLLED_MUTABLE_FIELDS.get("knowledge.IngestedFile", frozenset())
    return tuple(
        field.column for field in IngestedFile._meta.concrete_fields if field.column not in controlled
    )


def existing_columns(table: str) -> frozenset[str]:
    """``table`` 在**数据库里实际存在**的列集合。

    为什么必须有它（实测踩到的真实故障）：守卫的列集合是从**当前模型**反推的，
    但"模型里有字段"不等于"数据库里有这一列"——迁移回退把列删掉、而代码还没回退时
    两者就会分叉。此时若按模型建触发器，就会生成一个**引用不存在列**的函数体，
    之后任何 UPDATE 都会报错，而且这个坏触发器会一直留在库里。

    最典型的一条路径：`apps.py` 的 ``post_migrate`` 钩子在 `migrate` 结束后
    无条件重建守卫 —— 回退删列之后再重建，就把已删的列又装回了函数体里。
    （这正是"回退演练显示列已删、函数仍引用该列"的根因。）

    所以凡是"按列生成 SQL"的地方，都要与这里取交集。

    **必须跨数据库通用**（审查指出）：早先这里只写 ``information_schema``，
    那是 PostgreSQL 专用的；在 SQLite 上会抛异常 → 被 ``_guardable`` 的兜底
    catch 吞掉 → **退回按模型生成**，于是 SQLite 上照样会生成引用已删列的触发器。
    所以改用 Django 后端的 introspection，两种库都能拿到真实列名。
    """
    try:
        # 必须放进 savepoint：PostgreSQL 里一条失败的语句会把**整个事务**置为
        # aborted，之后任何查询都报 "current transaction is aborted"。
        # 表不存在时 introspection 正好会抛错，不隔离就会连累调用方。
        with transaction.atomic():
            with connection.cursor() as cursor:
                description = connection.introspection.get_table_description(
                    cursor, table
                )
        names = frozenset(column.name for column in description)
        if names:
            return names
    except Exception:  # noqa: BLE001 - 表不存在/权限不足等，落到下面的兜底
        logger.debug("introspection 取列失败，改用 information_schema：%s", table)
    # PostgreSQL 兜底（introspection 异常时）；同样放进 savepoint 以防污染事务
    if connection.vendor == "postgresql":
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT column_name FROM information_schema.columns
                        WHERE table_name = %s AND table_schema = ANY (current_schemas(false))
                        """,
                        [table],
                    )
                    return frozenset(row[0] for row in cursor.fetchall())
        except Exception:  # noqa: BLE001
            logger.debug("information_schema 兜底也失败：%s", table)
    return frozenset()


def _guardable(columns: tuple[str, ...]) -> tuple[str, ...]:
    """把候选列裁剪成"数据库里确实存在"的列（并保持原顺序）。"""
    try:
        present = existing_columns(INGESTED_FILE_TABLE)
    except Exception:  # noqa: BLE001 - 取不到就保守地不裁剪
        return columns
    if not present:
        return columns
    return tuple(column for column in columns if column in present)


def _effective_locked(columns: tuple[str, ...] | None) -> tuple[str, ...]:
    """所有建触发器路径的统一入口：先决定候选列，再裁成实际存在的列。"""
    return _guardable(columns if columns is not None else locked_columns())


#: 兼容旧调用点（只读用途）
_UPDATE_LOCKED_COLUMNS = locked_columns()

_ALL_TABLES = (INGESTED_FILE_TABLE, CHUNK_TABLE, AUDIT_TABLE)


def _sqlite_statements(columns: tuple[str, ...] | None = None) -> list[str]:
    statements: list[str] = []

    guarded = " OR ".join(
        f"OLD.{column} IS NOT NEW.{column}"
        for column in _effective_locked(columns)
    )
    statements.append(
        f"""
        CREATE TRIGGER {PREFIX}{INGESTED_FILE_TABLE}_update
        BEFORE UPDATE ON {INGESTED_FILE_TABLE}
        FOR EACH ROW
        WHEN {guarded}
        BEGIN
            SELECT RAISE(ABORT, '已入库资料不可篡改：核心字段（文件名/指纹/归属/目录等）不允许修改');
        END
        """
    )
    # 资料表的删除：**唯一**放行条件是"同一事务里签发的 DeleteGrant"。
    # 见 Postgres 分支里同样的说明——不做"摘触发器"的全局窗口。
    statements.append(
        f"""
        CREATE TRIGGER {PREFIX}{INGESTED_FILE_TABLE}_delete
        BEFORE DELETE ON {INGESTED_FILE_TABLE}
        FOR EACH ROW
        WHEN NOT EXISTS (
            SELECT 1 FROM kb_delete_grant g
            WHERE g.target_kind = 'ingested_file'
              AND g.target_ref = CAST(OLD.id AS TEXT)
              AND g.consumed_at IS NULL
              AND g.expires_at > CURRENT_TIMESTAMP
        )
        BEGIN
            SELECT RAISE(ABORT, '已入库资料不可删除：必须走受控删除通道（事务内精确授权）');
        END
        """
    )
    statements.append(
        f"""
        CREATE TRIGGER {PREFIX}{CHUNK_TABLE}_update
        BEFORE UPDATE ON {CHUNK_TABLE}
        FOR EACH ROW
        BEGIN
            SELECT RAISE(ABORT, '解析分段不可修改（append-only）');
        END
        """
    )
    statements.append(
        f"""
        CREATE TRIGGER {PREFIX}{CHUNK_TABLE}_delete
        BEFORE DELETE ON {CHUNK_TABLE}
        FOR EACH ROW
        WHEN NOT EXISTS (
            SELECT 1 FROM kb_delete_grant g
            WHERE g.target_kind = 'ingested_file'
              AND g.target_ref = CAST(OLD.file_id AS TEXT)
              AND g.consumed_at IS NULL
              AND g.expires_at > CURRENT_TIMESTAMP
        )
        BEGIN
            SELECT RAISE(ABORT, '解析分段不可删除：只允许随受控删除的资料级联（事务内精确授权）');
        END
        """
    )
    statements.append(
        f"""
        CREATE TRIGGER {PREFIX}{AUDIT_TABLE}_update
        BEFORE UPDATE ON {AUDIT_TABLE}
        FOR EACH ROW
        BEGIN
            SELECT RAISE(ABORT, '审计事件不可修改（append-only 哈希链）');
        END
        """
    )
    statements.append(
        f"""
        CREATE TRIGGER {PREFIX}{AUDIT_TABLE}_delete
        BEFORE DELETE ON {AUDIT_TABLE}
        FOR EACH ROW
        BEGIN
            SELECT RAISE(ABORT, '审计事件不可删除（append-only 哈希链）');
        END
        """
    )
    return statements


def _postgres_statements(columns: tuple[str, ...] | None = None) -> list[str]:
    statements: list[str] = []
    guarded = " OR ".join(
        f"OLD.{column} IS DISTINCT FROM NEW.{column}"
        for column in _effective_locked(columns)
    )
    statements.append(
        f"""
        CREATE OR REPLACE FUNCTION {PREFIX}ingested_file_update() RETURNS trigger AS $$
        BEGIN
            IF {guarded} THEN
                RAISE EXCEPTION '已入库资料不可篡改：核心字段（文件名/指纹/归属/目录等）不允许修改';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    statements.append(
        f"""
        CREATE TRIGGER {PREFIX}{INGESTED_FILE_TABLE}_update
        BEFORE UPDATE ON {INGESTED_FILE_TABLE}
        FOR EACH ROW EXECUTE FUNCTION {PREFIX}ingested_file_update();
        """
    )
    statements.append(
        f"""
        CREATE OR REPLACE FUNCTION {PREFIX}append_only_guard() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '受保护表 % 只允许追加，不允许 %', TG_TABLE_NAME, TG_OP;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    # 删除守卫函数必须**先定义**，触发器才能引用它（顺序错了会报
    # "function ... does not exist"——实测踩到）。
    # 授权表由 planner 迁移创建（kb_delete_grant）。
    statements.append(
        f"""
        CREATE OR REPLACE FUNCTION {PREFIX}ingested_file_delete_guard() RETURNS trigger AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM kb_delete_grant g
                WHERE g.target_kind = 'ingested_file'
                  AND g.target_ref = OLD.id::text
                  AND g.consumed_at IS NULL
                  AND g.expires_at > now()
            ) THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION '已入库资料不可删除：必须走受控删除通道（事务内精确授权）';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    # 分段表的删除：**必须**也按"同一事务里针对 OLD.file_id 的授权"放行。
    #
    # 审查指出的实质缺陷：早期分段删除是无条件拒绝，而资料删除会**级联**删分段，
    # 于是受控删除永远会在级联这一步被挡。两侧现在共用同一张授权表与同一个
    # 目标标识（资料的 id）：授权是给"这份资料"的，删本体与删它的分段都凭它放行。
    statements.append(
        f"""
        CREATE OR REPLACE FUNCTION {PREFIX}chunk_delete_guard() RETURNS trigger AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM kb_delete_grant g
                WHERE g.target_kind = 'ingested_file'
                  AND g.target_ref = OLD.file_id::text
                  AND g.consumed_at IS NULL
                  AND g.expires_at > now()
            ) THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION '解析分段不可删除：只允许随受控删除的资料级联（事务内精确授权）';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    # 资料表的删除：**唯一**放行条件是"同一事务里签发的 DeleteGrant"。
    #
    # 为什么用一张授权表而不是"临时摘掉触发器"（审查明确否掉摘触发器）：
    # 摘/建之间会有**全局窗口**，任何并发请求都能在窗口期删除任意资料。
    # 授权表方案没有窗口：授权行由服务层在**删除的同一事务**里插入，
    # 其他事务在提交前看不到它（READ COMMITTED），回滚则授权一并消失。
    # 因此这是一条**事务局部**通道，只能删除"被精确授权的那一行"。
    statements.append(
        f"""
        CREATE TRIGGER {PREFIX}{INGESTED_FILE_TABLE}_delete
        BEFORE DELETE ON {INGESTED_FILE_TABLE}
        FOR EACH ROW EXECUTE FUNCTION {PREFIX}ingested_file_delete_guard();
        """
    )
    # 分段：走自己的守卫（按 file_id 授权）；审计表仍然**无条件拒绝删除**
    statements.append(
        f"""
        CREATE TRIGGER {PREFIX}{CHUNK_TABLE}_delete
        BEFORE DELETE ON {CHUNK_TABLE}
        FOR EACH ROW EXECUTE FUNCTION {PREFIX}chunk_delete_guard();
        """
    )
    statements.append(
        f"""
        CREATE TRIGGER {PREFIX}{AUDIT_TABLE}_delete
        BEFORE DELETE ON {AUDIT_TABLE}
        FOR EACH ROW EXECUTE FUNCTION {PREFIX}append_only_guard();
        """
    )
    for table in (CHUNK_TABLE, AUDIT_TABLE):
        statements.append(
            f"""
            CREATE TRIGGER {PREFIX}{table}_update
            BEFORE UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION {PREFIX}append_only_guard();
            """
        )
    return statements


def statements(*, locked: tuple[str, ...] | None = None) -> list[str]:
    """按当前连接的后端返回建触发器语句。

    ``locked`` 用来**冻结列集合**：迁移文件必须传它当时历史 schema 实际拥有的列，
    不能用运行时模型的列。否则会出现"迁移在早期 schema 上建了一个引用未来列的
    触发器"，随后 SQLite 的 ``ALTER TABLE``（remake_table）就会炸：
    ``error in trigger kb_guard_...: no such column: OLD.<future_column>``。
    这正是"从零安装"会踩到、而增量迁移踩不到的缺陷。
    """
    vendor = connection.vendor
    if vendor == "sqlite":
        return _sqlite_statements(locked)
    if vendor == "postgresql":
        return _postgres_statements(locked)
    return []  # 其他后端：只保留 ORM 层守卫，并在文档里标注未覆盖


def _exec_all(statements_to_run: Iterable[str]) -> int:
    count = 0
    with connection.cursor() as cursor:
        for statement in statements_to_run:
            cursor.execute(statement)
            count += 1
    return count


def _drop_names() -> list[str]:
    names: list[str] = []
    for table in _ALL_TABLES:
        names.append(f"{PREFIX}{table}_update")
        names.append(f"{PREFIX}{table}_delete")
    return names


def drop_guards() -> int:
    """摘掉触发器（受控删除通道专用，必须先进入 privileged_channel）。"""
    vendor = connection.vendor
    dropped = 0
    with connection.cursor() as cursor:
        for name in _drop_names():
            try:
                if vendor == "postgresql":
                    cursor.execute(f"DROP TRIGGER IF EXISTS {name} ON {_table_of(name)}")
                else:
                    cursor.execute(f"DROP TRIGGER IF EXISTS {name}")
                dropped += 1
            except Exception:  # pragma: no cover - 触发器本就不存在
                continue
    return dropped


def create_guards(*, locked: tuple[str, ...] | None = None) -> int:
    """重建触发器（幂等）。

    ``locked`` 见 :func:`statements`：迁移必须用"当时历史 schema 的列"来建触发器。
    """
    vendor = connection.vendor
    if vendor in {"sqlite", "postgresql"}:
        # 先摘再建，避免 "trigger already exists"
        if vendor == "postgresql":
            with connection.cursor() as cursor:
                for name in _drop_names():
                    cursor.execute(f"DROP TRIGGER IF EXISTS {name} ON {_table_of(name)}")
        else:
            with connection.cursor() as cursor:
                for name in _drop_names():
                    cursor.execute(f"DROP TRIGGER IF EXISTS {name}")
    return _exec_all(statements(locked=locked))


def _table_of(trigger_name: str) -> str:
    body = trigger_name[len(PREFIX) :]
    for suffix in ("_update", "_delete"):
        if body.endswith(suffix):
            return body[: -len(suffix)]
    return body


def guards_present() -> bool:
    """当前连接上是否已经装好全部触发器（供启动自检与测试断言）。"""
    vendor = connection.vendor
    if vendor == "sqlite":
        query = "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE %s"
    elif vendor == "postgresql":
        query = "SELECT tgname FROM pg_trigger WHERE tgname LIKE %s"
    else:
        return False
    with connection.cursor() as cursor:
        cursor.execute(query, [f"{PREFIX}%"])
        found = {row[0] for row in cursor.fetchall()}
    return set(_drop_names()) <= found
