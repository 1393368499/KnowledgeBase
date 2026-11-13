"""真实 PostgreSQL 双连接负例：**清理旧代次与切换代次互斥**（用户第 2 项的第 3 点）。

背景（独立审查指出的 TOCTOU）：`_reset_library_index` 里的旧代次清理如果只"查一次当快照"，
另一个请求完全可以在查询之后、`_drop_collection` 之前调用 `activate_generation`
把那个集合切成**生效代次**，而清理照样把它删掉。

产品侧的可证明互斥（见 `services._reset_library_index` 的 F 段）：
  1. 清理的临界区在**事务**里 `select_for_update` 锁住同一条 `KnowledgeLibrary` 行；
  2. `vectorindex.activate_generation`（vectorindex.py:657-666）同样先锁这条库行；
  3. 清理在**锁内先删掉 `VectorCollection` 登记行**（激活必须校验登记），提交后才做远端删除。

本文件用**两个真实 PG 连接**验证第 1、2 条（互斥），并在释放锁之后验证第 3 条
（登记行已不存在 → 激活的校验路径必须拒绝）。SQLite 不支持这种行级锁语义，
因此非 PostgreSQL 环境跳过（**不算通过**）。
"""

from __future__ import annotations

import pytest
from django.conf import settings
from django.db import connections, transaction
from django.utils import timezone

from apps.knowledge.models import KnowledgeLibrary, VectorCollection

pytestmark = pytest.mark.django_db(transaction=True)


def _pg_available() -> bool:
    return connections["default"].vendor == "postgresql"


requires_pg = pytest.mark.skipif(
    not _pg_available(),
    reason="需要真实 PostgreSQL：SQLite 没有这种行级锁语义（本用例不计入通过）",
)


def _second_connection():
    """另开一条**独立**到同一个测试库的连接（模拟第二个请求/进程）。"""
    import psycopg

    db = settings.DATABASES["default"]
    return psycopg.connect(
        host=db["HOST"],
        port=int(db["PORT"] or 5432),
        dbname=db["NAME"],
        user=db["USER"],
        password=db["PASSWORD"] or "",
        autocommit=False,
    )


@requires_pg
def test_activation_cannot_lock_library_while_cleanup_holds_it(library):
    """清理持锁期间，第二个连接的 `SELECT … FOR UPDATE` 必须**拿不到锁**。"""
    other = _second_connection()
    try:
        with transaction.atomic():
            # 连接 A（本用例）：模拟清理临界区，锁住库行
            KnowledgeLibrary.objects.select_for_update().get(pk=library.pk)
            with other.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '2s'")
                with pytest.raises(Exception) as exc:
                    cursor.execute(
                        "SELECT id FROM kb_knowledge_library WHERE id = %s FOR UPDATE",
                        [library.pk],
                    )
                assert "lock" in str(exc.value).lower() or "超时" in str(exc.value), exc.value
    finally:
        other.close()


@requires_pg
def test_cleanup_and_activation_race_on_two_connections(library):
    """**真实双连接并发**（`threading.Event` 同步，不靠 sleep 猜时序）：

      1. 线程 A 进入"清理临界区"：独立 DB 连接 + 事务 + `select_for_update` 锁库行，
         把目标代次标记 `retiring_at`，然后 `holding.set()` 等待 `release`；
      2. 主线程等 `holding` 置位后，用**另一条连接**尝试锁同一库行
         （`SET LOCAL lock_timeout`）→ 必须**拿不到锁**（互斥的可证明形式）；
      3. 主线程再验证产品侧校验：退役状态被 `validate_activation` 明确拒绝；
      4. `release.set()` 后线程 A 提交；最终断言该代次仍是"退役待删"、**登记行没丢**
         （可重试）、也不是生效代次。
    """
    import threading

    from django.db import connections

    from apps.knowledge import vectorindex

    stale = VectorCollection.objects.create(
        name=f"kb-{library.pk}-g2", dimension=1024, library=library, is_active=False
    )
    holding = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []
    observed: dict[str, object] = {}

    def cleanup_side() -> None:
        try:
            from django.db import transaction as _tx

            with _tx.atomic():
                KnowledgeLibrary.objects.select_for_update().get(pk=library.pk)
                VectorCollection.objects.filter(pk=stale.pk).update(retiring_at=timezone.now())
                observed["cleanup_locked"] = True
                holding.set()
                if not release.wait(timeout=15):
                    raise AssertionError("主线程没有在 15s 内放行清理侧")
        except BaseException as exc:  # noqa: BLE001 - 线程内异常要带回主线程
            errors.append(exc)
        finally:
            connections.close_all()

    worker = threading.Thread(target=cleanup_side, name="cleanup-side", daemon=True)
    worker.start()
    try:
        assert holding.wait(timeout=15), "清理侧没有拿到锁"
        assert observed.get("cleanup_locked") is True

        other = _second_connection()
        try:
            with other.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '2s'")
                with pytest.raises(Exception) as exc:
                    cursor.execute(
                        "SELECT id FROM kb_knowledge_library WHERE id = %s FOR UPDATE",
                        [library.pk],
                    )
            assert "lock" in str(exc.value).lower() or "超时" in str(exc.value), exc.value
            other.rollback()
        finally:
            other.close()
    finally:
        release.set()
        worker.join(timeout=20)
    assert not errors, errors
    assert not worker.is_alive()

    stale.refresh_from_db()
    assert stale.retiring_at is not None, "清理侧没有落下退役状态"
    assert VectorCollection.objects.filter(pk=stale.pk).exists(), "唯一登记行被丢了（无法重试）"
    assert not stale.is_active

    plan = vectorindex.GenerationPlan(
        library_id=library.pk,
        generation=vectorindex._generation_of(library, stale.name) or 2,
        collection=stale.name,
        dimension=int(stale.dimension or 1024),
        model=stale.embedding_model or "",
        model_version=stale.model_version or "",
        previous_generation=int(library.active_generation or 0),
        previous_collection=library.vector_collection or "",
        created=False,
    )
    with pytest.raises(Exception) as exc:
        vectorindex.validate_activation(library, plan, verify_target_coverage=False)
    assert "退役" in str(exc.value), exc.value


@requires_pg
def test_real_activate_generation_blocks_on_cleanup_lock_then_refuses(library):
    """**锁被真正持有时**并发调用**真实 `activate_generation`**（不 mock 锁/校验）：

      1. 线程 A（独立连接）在事务里 `select_for_update` 锁住库行并置 `retiring_at`，
         `holding` 事件置位后**保持不提交**；
      2. 主线程启动线程 B 调用**真实产品函数** `vectorindex.activate_generation` ——
         它会**阻塞在行锁上**（证明互斥真的生效，而不是靠快照判断）；
      3. 主线程放行 A → A 提交；B 拿到锁后读到退役标记 → 抛错（拒绝激活）；
      4. 最终断言：该代次不是生效代次、库指针没被改、登记行仍在（可重试）。
    """
    import threading
    import time

    from django.db import connections

    from apps.knowledge import vectorindex

    stale = VectorCollection.objects.create(
        name=vectorindex.active_collection_name(library.pk, 1),
        dimension=1024,
        library=library,
        is_active=False,
    )
    holding = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []
    observed: dict[str, object] = {}

    def cleanup_side() -> None:
        try:
            from django.db import transaction as _tx

            with _tx.atomic():
                KnowledgeLibrary.objects.select_for_update().get(pk=library.pk)
                VectorCollection.objects.filter(pk=stale.pk).update(retiring_at=timezone.now())
                observed["locked"] = True
                holding.set()
                release.wait(timeout=30)
            observed["committed"] = True
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            connections.close_all()

    def activate_side() -> None:
        try:
            library_fresh = KnowledgeLibrary.objects.get(pk=library.pk)
            plan = vectorindex.GenerationPlan(
                library_id=library.pk,
                generation=1,
                collection=stale.name,
                dimension=1024,
                model=stale.embedding_model or "",
                model_version=stale.model_version or "",
                previous_generation=int(library_fresh.active_generation or 0),
                previous_collection=library_fresh.vector_collection or "",
                created=False,
            )
            observed["activate_started"] = time.time()
            vectorindex.activate_generation(library_fresh, plan)  # 真实函数，真实锁
            observed["activate_returned"] = True
        except BaseException as exc:  # noqa: BLE001 - 这里**预期**是拒绝
            observed["activate_error"] = f"{type(exc).__name__}: {exc}"
        finally:
            connections.close_all()

    thread_a = threading.Thread(target=cleanup_side, name="cleanup", daemon=True)
    thread_a.start()
    assert holding.wait(timeout=30), "清理侧没有拿到库行锁"
    assert observed.get("locked") is True

    thread_b = threading.Thread(target=activate_side, name="activate", daemon=True)
    thread_b.start()
    # 给 B 一点时间真正撞到锁上（它此时**不应该**已经返回）
    time.sleep(2.0)
    assert "activate_error" not in observed and "activate_returned" not in observed, (
        "清理持锁期间 activate_generation 没有被挡住："
        f"{observed.get('activate_error') or observed.get('activate_returned')}"
    )
    assert not stale.__class__.objects.filter(pk=stale.pk, is_active=True).exists()

    release.set()
    thread_a.join(timeout=30)
    thread_b.join(timeout=60)
    assert not errors, errors
    assert observed.get("committed") is True
    assert "退役" in str(observed.get("activate_error") or ""), observed
    assert observed.get("activate_returned") is not True

    stale.refresh_from_db()
    assert stale.retiring_at is not None, "退役标记丢了"
    assert not stale.is_active, "待删代次变成了生效代次"
    library.refresh_from_db()
    assert library.vector_collection != stale.name, "库指针指向了待删代次"


@requires_pg
def test_after_cleanup_commits_the_collection_is_retiring_not_activatable(library):
    """清理提交后：该代次处于**退役待删**状态（登记行仍在），激活/回退必须拒绝它。

    两件事一起钉住（独立审查第 2 项的正确顺序）：
      * **不先丢唯一登记**：drop 失败时还能继续重试，不会变成谁也找不到的孤儿；
      * 却**依然不可能被切上线**：`validate_activation` / `rollback_generation` 都
        明确拒绝 `retiring_at` 非空的代次 —— 于是"清理 vs 切换"的互斥靠的是
        持久状态 + 同一把库行锁，而不是"查一次当快照"。
    """
    from apps.knowledge import vectorindex

    stale = VectorCollection.objects.create(
        name=f"kb-{library.pk}-g1", dimension=1024, library=library, is_active=False
    )
    with transaction.atomic():
        KnowledgeLibrary.objects.select_for_update().get(pk=library.pk)
        # 清理临界区做的事：锁内标记退役（**不是**删登记行）
        VectorCollection.objects.filter(pk=stale.pk).update(retiring_at=timezone.now())
    stale.refresh_from_db()
    assert stale.retiring_at is not None, "清理没有留下退役状态"
    assert VectorCollection.objects.filter(pk=stale.pk).exists(), "清理提前丢了登记行"

    # 另一个连接读到的事实一致（退役状态已提交）
    other = _second_connection()
    try:
        with other.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM kb_vector_collection "
                "WHERE library_id = %s AND name = %s AND retiring_at IS NOT NULL",
                [library.pk, stale.name],
            )
            row = cursor.fetchone()
            assert row is not None and row[0] == 1
    finally:
        other.close()

    # 激活必须被拒绝（这是"不可能被切上线"的可验证表述）
    library.refresh_from_db()
    plan = vectorindex.GenerationPlan(
        library_id=library.pk,
        generation=vectorindex._generation_of(library, stale.name) or 1,
        collection=stale.name,
        dimension=int(stale.dimension or 1024),
        model=stale.embedding_model or "",
        model_version=stale.model_version or "",
        previous_generation=int(library.active_generation or 0),
        previous_collection=library.vector_collection or "",
        created=False,
    )
    with pytest.raises(Exception) as exc:
        vectorindex.validate_activation(library, plan, verify_target_coverage=False)
    assert "退役" in str(exc.value), exc.value
