"""真实 PostgreSQL 并发负例：库级写入闸门（增量审查四条要求）。

验证点：
1. 冻结后写入被**显式拒绝**（不是静默落库）；
2. **令牌只旋转不清空**：``unfreeze`` 后旧令牌永久失效（防 ABA），且比较按"是否提供"
   （``None`` 才算未提供），空串旧令牌不跳过校验；
3. **租约检查与冻结同锁**：存在活跃租约时 ``freeze`` 拒绝且**不写冻结节**；
4. **两连接并发**：写者持有闸门行锁时，另一连接的 ``freeze`` 不会"检查后才插入"——
   它会在行锁上等待（这里用 ``statement_timeout`` 证明它确实被锁住，而不是穿透）。

SQLite 没有这种行级锁语义，因此非 PostgreSQL 环境**跳过（不计入通过）**。
"""

from __future__ import annotations

import pytest
from django.conf import settings
from django.db import connections, transaction
from django.utils import timezone

from apps.knowledge import writegate
from apps.knowledge.embedding import EmbeddingError
from apps.knowledge.models import IngestedFile, KnowledgeLibrary, ParseStatus

pytestmark = pytest.mark.django_db(transaction=True)

requires_pg = pytest.mark.skipif(
    connections["default"].vendor != "postgresql",
    reason="需要真实 PostgreSQL：SQLite 没有这种行级锁语义（本用例不计入通过）",
)


def _second_connection():
    """另开一条独立连接到同一个测试库（模拟第二个请求/进程）。"""
    import psycopg

    db = settings.DATABASES["default"]
    return psycopg.connect(
        host=db["HOST"],
        port=db["PORT"],
        dbname=db["NAME"],
        user=db["USER"],
        password=db["PASSWORD"],
        autocommit=True,
    )


@pytest.fixture
def library(db):
    return KnowledgeLibrary.objects.create(code="gate-lib", name="闸门库")


@requires_pg
def test_freeze_rejects_write_and_keeps_frozen_until_unfreeze(library):
    token = writegate.freeze(library.pk, reason="测试重建", actor="tester")
    assert token and writegate.gate_state(library.pk)["frozen"] is True

    with pytest.raises(writegate.WriteFrozen):
        writegate.assert_writable(library.pk, action="写入")

    writegate.unfreeze(library.pk, fence_token=token)
    assert writegate.gate_state(library.pk)["frozen"] is False
    # 解冻后普通写入恢复
    writegate.assert_writable(library.pk, action="写入")


@requires_pg
def test_fence_token_rotates_and_never_cleared_on_unfreeze(library):
    before = writegate.current_token(library.pk)
    assert before, "闸门初建就必须有非空令牌（防 ABA）"

    token = writegate.freeze(library.pk, reason="测试重建")
    assert token != before
    writegate.unfreeze(library.pk, fence_token=token)

    after = writegate.current_token(library.pk)
    assert after, "unfreeze 不得清空令牌"
    assert after != before, "unfreeze 后旧令牌必须永久失效"
    # 旧令牌（冻结前）与本次冻结令牌都必须被拒绝
    with pytest.raises(writegate.WriteFrozen):
        writegate.assert_fence(library.pk, before, action="复核")
    with pytest.raises(writegate.WriteFrozen):
        writegate.assert_fence(library.pk, token, action="复核")
    # **空串也必须被比较**（按"是否提供"判断，而不是真值判断）
    with pytest.raises(writegate.WriteFrozen):
        writegate.assert_writable(library.pk, fence_token="", action="写入")


@requires_pg
def test_freeze_refuses_when_active_lease_exists_and_writes_no_freeze(library):
    now = timezone.now()
    IngestedFile.objects.create(
        library=library,
        original_name="busy.pdf",
        content_sha256="a" * 64,
        size_bytes=1,
        parse_status=ParseStatus.RUNNING,
        parse_lease_token="busy-token",
        parse_lease_started_at=now,
        parse_lease_expires_at=now + timezone.timedelta(minutes=10),
    )
    with pytest.raises(writegate.WriteFrozen):
        writegate.freeze(library.pk, reason="测试重建")
    # 拒绝必须**不留冻结节**（否则库被永久冻死）
    assert writegate.gate_state(library.pk)["frozen"] is False


@requires_pg
def test_freeze_blocks_on_gate_row_lock_held_by_writer(library):
    """写者持锁期间，**业务函数 ``freeze()``** 必须在行锁上等待（不是穿透）。

    注意：Django 会把游标异常包装为 ``django.db.OperationalError``，底层 cause 才是
    ``psycopg.errors.QueryCanceled``（statement_timeout）。两个都要断言。
    """
    import psycopg
    from django.db import OperationalError

    token = writegate.current_token(library.pk)
    conn = _second_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("BEGIN")
            cur.execute(
                "SELECT fence_token FROM kb_library_write_gate WHERE library_id = %s FOR UPDATE",
                [library.pk],
            )
            with pytest.raises(OperationalError) as excinfo:
                with transaction.atomic():
                    with connections["default"].cursor() as cur2:
                        cur2.execute("SET LOCAL statement_timeout = 800")
                        # **真正调用业务函数**：证明"冻结 + 锁内检查租约"这条路径
                        # 会被写者持有的行锁挡住，而不是"查完再插"。
                        writegate.freeze(library.pk, reason="并发重建（应被锁住）")
            assert isinstance(excinfo.value.__cause__, psycopg.errors.QueryCanceled)
            cur.execute("COMMIT")
    finally:
        conn.close()
    # 锁释放后：同一业务函数正常成功并旋转令牌；解冻再旋转一次
    new_token = writegate.freeze(library.pk, reason="锁释放后重建")
    assert new_token and new_token != token
    writegate.unfreeze(library.pk, fence_token=new_token)
    assert writegate.current_token(library.pk) not in {"", new_token}


@requires_pg
def test_writegate_module_documents_no_self_transaction_for_writers():
    """回归护栏：写路径必须用 ``assert_writable_locked``（要求外层事务）。

    这条用例防止有人把写路径改回"短检查各自开事务"，从而退回 TOCTOU。
    """
    with pytest.raises(RuntimeError, match="外层 transaction.atomic"):
        writegate.assert_writable_locked(1)
    assert EmbeddingError  # 保持导入稳定（同一测试模块复用到指纹/快照用例）


@requires_pg
def test_frozen_window_refuses_external_activate_and_rollback(library):
    """**冻结期内，外部激活/回退必须被拒**（审查 P1：能闯进别人的重建窗口）。

    语义：``activate_generation(..., freeze_token=None)``（HTTP 受控激活、
    planner 受控意图、``rollback_generation`` 全都走这条默认路径）必须先过
    ``assert_writable_locked``。冻结中 → ``WriteFrozen``，**一个指针都不许动**。

    另一条同样重要：**传了 rebuild 的令牌**时才允许发布（那条路径在下面用
    ``assert_freeze_owner`` 验证"我仍是当前 owner"）。这里一并证明
    "随便传一个自报令牌"也没用 —— 令牌对不上照样拒绝。
    """
    from apps.knowledge import vectorindex
    from apps.knowledge.embedding import get_profile
    from apps.knowledge.models import VectorCollection
    from tests.test_vector_profiles import FakeQdrant

    profile = get_profile("bge1024")
    # 用 FakeQdrant：这条用例要证明的是**闸门**，不该顺带依赖任何真实/嵌入式向量库
    plan = vectorindex.prepare_generation(
        library, profile=profile, client=FakeQdrant()
    )
    # 登记行必须存在（否则"被拒"的原因会是"没登记"，而不是闸门）
    assert VectorCollection.objects.filter(library=library, name=plan.collection).exists()

    token = writegate.freeze(library.pk, reason="重建中（测试）", actor="tester")
    try:
        with pytest.raises(writegate.WriteFrozen):
            vectorindex.activate_generation(library, plan, freeze_token=None)
        # rollback 也不接受自报令牌：它走的是默认路径 → 同样被拒
        with pytest.raises(writegate.WriteFrozen):
            vectorindex.rollback_generation(library)
        with pytest.raises(writegate.WriteFrozen):
            # 冒充 rebuild 也不行：令牌不匹配就是不匹配
            vectorindex.activate_generation(
                library, plan, freeze_token="forged-token"
            )
        # 拒绝必须是"什么都没发生"：库指针与登记行的 active 标记都没动
        library.refresh_from_db()
        assert library.vector_collection == ""
        assert library.active_generation == 0
        assert VectorCollection.objects.filter(library=library, is_active=True).count() == 0
    finally:
        writegate.unfreeze(library.pk, fence_token=token)


@requires_pg
def test_archive_must_wait_for_gate_and_does_no_remote_delete_when_frozen(
    django_user_model, monkeypatch
):
    """受控归档的远端删点与状态提交必须在**同一把 gate 锁**下（审查 P1）。

    两个断言，对准两个方向：

    1. **必须等待**：归档事务持锁期间，**第二个连接**上的 ``freeze()`` 被行锁挡住
       （在真实 ``delete_points`` 回调里发起，用 ``statement_timeout`` 证明它确实
       在等锁，而不是"检查完就放"）；
    2. **已冻结则一个远端删点都不发生**：冻结先生效时归档直接拒绝，
       远端 ``delete_points`` 一次都不许被调用 —— 这是"点已删、归档却被 409 拒绝"
       那个中间态的直接反例。
    """
    import threading

    import psycopg
    from django.db import OperationalError

    from apps.knowledge.models import DocumentChunk
    from apps.planner import services as planner_services

    user = django_user_model.objects.create_user(
        username="gate-archiver", password="x" * 12
    )
    library = KnowledgeLibrary.objects.create(
        code="gate-archive-lib", name="归档闸门库", vector_collection="kb-arch-g1"
    )
    record = IngestedFile.objects.create(
        library=library,
        original_name="归档用例.md",
        content_sha256="b" * 64,
        size_bytes=10,
        parse_status=ParseStatus.DONE,
        vector_collection="kb-arch-g1",
        vector_collection_name="kb-arch-g1",
        chunk_count=1,
        embedded_chunk_count=1,
        vector_status="indexed",
    )
    DocumentChunk.objects.create(file=record, ordinal=1, text="内容", token_estimate=2)
    # 闸门行**先建好并提交**：否则"第二个连接上的 freeze"会退化成"插入一行"，
    # 锁等待发生在唯一索引上，报出来的错就不是我们要证明的"gate 行锁"。
    writegate.current_token(library.pk)

    deleted: list[str] = []

    class _FakeClient:
        def close(self):
            return None

    def _fake_build_client():
        return _FakeClient()

    def _fake_delete_points(client, collection, ids):
        deleted.append(collection)

    monkeypatch.setattr("apps.knowledge.vectorindex.build_client", _fake_build_client)
    monkeypatch.setattr("apps.knowledge.vectorindex.delete_points", _fake_delete_points)

    operation = type("Op", (), {"pk": 1, "params": {}, "confirmations": 1})()

    # ---- 2) 已冻结：任何远端删点都不许发生 ----
    frozen_token = writegate.freeze(library.pk, reason="重建中（测试）")
    try:
        with pytest.raises(writegate.WriteFrozen):
            planner_services._archive_file(user, operation, record)
        assert deleted == [], "库已冻结时绝不允许发生远端删点"
        record.refresh_from_db()
        assert record.parse_status == ParseStatus.DONE, "归档被拒时状态不得改变"
        assert record.embedded_chunk_count == 1, "归档被拒时账目不得清零"
    finally:
        writegate.unfreeze(library.pk, fence_token=frozen_token)

    # ---- 1) 必须等待：真实 delete_points 回调里从第二个连接试 freeze ----
    def _delete_and_try_freeze(client, collection, ids):
        _fake_delete_points(client, collection, ids)
        outcome: dict = {}

        def _worker():
            # 线程内的 Django 连接是**独立连接**（连接注册表是线程局部的），
            # 所以这确实是"另一个会话"在抢同一把 gate 行锁。
            from django.db import connection as _thread_conn

            try:
                with _thread_conn.cursor() as cur:
                    cur.execute("SET statement_timeout = 800")
                writegate.freeze(library.pk, reason="并发重建（应被锁住）")
                outcome["frozen"] = True
            except Exception as exc:  # noqa: BLE001 - 期望就是"被锁住"
                outcome["error"] = exc
            finally:
                _thread_conn.close()

        thread = threading.Thread(target=_worker)
        thread.start()
        thread.join(timeout=15)
        assert not thread.is_alive(), "第二个连接上的 freeze 没有在 gate 行锁上被挡住"
        assert "frozen" not in outcome, (
            "归档持锁期间 freeze 竟然成功了：说明远端删点/状态提交不在同一把 gate 锁下"
        )
        err = outcome.get("error")
        assert isinstance(err, OperationalError), f"期望被行锁挡住，实际 {err!r}"
        assert isinstance(err.__cause__, psycopg.errors.QueryCanceled)

    monkeypatch.setattr(
        "apps.knowledge.vectorindex.delete_points", _delete_and_try_freeze
    )
    result = planner_services._archive_file(user, operation, record)
    assert result["archived"] is True
    assert deleted == ["kb-arch-g1"], "归档必须真的做了精确删点"
    record.refresh_from_db()
    assert record.parse_status == ParseStatus.ARCHIVED
    assert record.embedded_chunk_count == 0
    # 归档结束、锁释放后，冻结照常可用
    token2 = writegate.freeze(library.pk, reason="归档后重建")
    assert token2
    writegate.unfreeze(library.pk, fence_token=token2)


@requires_pg
def test_stale_writer_cannot_overwrite_online_state_after_freeze_cycle(
    library, monkeypatch
):
    """**陈旧任务的模型错误不得覆盖线上状态**（审查要求：许可覆盖失败路径）。

    场景：旧 worker 在模型调用期间，库被 freeze 又 unfreeze（换代窗口结束，
    **collection 没变**），随后模型报错。旧 worker 手里那把许可在解除时已经被
    旋转作废 —— 它想写 ``partial``/``unavailable`` 会被拒，线上状态/指针/计数
    一个字段都不许变（也不许被改写成 FAILED）。
    """
    from apps.knowledge import embedding as emb
    from apps.knowledge import pipeline
    from apps.knowledge.models import DocumentChunk, VectorCollection
    from apps.knowledge.resilience import ServiceUnavailable
    from tests.test_vector_profiles import FakeQdrant

    profile = emb.get_profile("bge1024")
    VectorCollection.objects.create(
        name="kb-stale-g1",
        dimension=profile.dimension,
        embedding_model=profile.model,
        model_version=profile.model_version,
        profile=profile.name,
        base_url=profile.base_url,
        library=library,
        storage_mode="server",
        storage_path="vector_data/qdrant",
        is_active=True,
        build_status="ready",
    )
    KnowledgeLibrary.objects.filter(pk=library.pk).update(
        active_generation=1, vector_collection="kb-stale-g1"
    )
    record = IngestedFile.objects.create(
        library=library,
        original_name="陈旧写入.md",
        content_sha256="c" * 64,
        size_bytes=10,
        parse_status=ParseStatus.DONE,
        vector_collection="kb-stale-g1",
        vector_collection_name="kb-stale-g1",
        vector_generation=1,
        chunk_count=1,
        embedded_chunk_count=1,
        vector_status="indexed",
        parse_error="",
    )
    DocumentChunk.objects.create(file=record, ordinal=1, text="内容", token_estimate=2)

    def _embed_then_freeze_and_fail(texts, **kwargs):
        # 模型调用期间发生 freeze + unfreeze：许可被旋转作废；随后模型报错。
        t = writegate.freeze(library.pk, reason="重建中（测试）")
        writegate.unfreeze(library.pk, fence_token=t)
        raise ServiceUnavailable("embedding", "模拟模型故障")

    monkeypatch.setattr(pipeline, "embed_for_indexing", _embed_then_freeze_and_fail)

    # 先按真实顺序领租约（claim 自己会把状态置 RUNNING），
    # 再对"领到租约之后"的线上状态做快照。
    token = pipeline.acquire_lease(record, force=True, consume_budget=False)
    record.refresh_from_db()
    before = {
        "parse_status": record.parse_status,
        "parse_error": record.parse_error,
        "vector_status": record.vector_status,
        "vector_collection_name": record.vector_collection_name,
        "vector_generation": record.vector_generation,
        "embedded_chunk_count": record.embedded_chunk_count,
        "chunk_count": record.chunk_count,
    }

    with pytest.raises(writegate.WriteFrozen):
        pipeline._process_file_locked(
            record,
            lease_token=token,
            client=FakeQdrant(),
            # reuse_chunks：这条用例只测"陈旧写入被拒"，不重新解析原文
            reuse_chunks=True,
        )

    record.refresh_from_db()
    assert {
        "parse_status": record.parse_status,
        "parse_error": record.parse_error,
        "vector_status": record.vector_status,
        "vector_collection_name": record.vector_collection_name,
        "vector_generation": record.vector_generation,
        "embedded_chunk_count": record.embedded_chunk_count,
        "chunk_count": record.chunk_count,
    } == before, "陈旧任务不得改写任何线上状态（含 partial/unavailable/FAILED）"

