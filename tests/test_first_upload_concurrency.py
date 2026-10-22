"""真实并发首上传回归（**新库 + 真 PG + 多线程**）。

为什么必须单独写这一组（并发实测暴露的真实竞态）
------------------------------------------------
4 并发对**全新库**上传时，旧 ``ensure_library_index`` 是无锁的 check-then-act：

- 4 个请求都看到"库还没有 collection" → 各自 ``prepare_generation`` 造出
  g1/g2/g3/g4；
- 各自写库指针（后写者覆盖），再 ``update(is_active=True)``；
- 偏唯一索引 ``uniq_active_collection_per_library`` 只允许一行 active
  → 后 3 个抛 ``IntegrityError`` → **HTTP 500**；
- 而且留下**指针与登记表不一致**（指针指 g4，登记表 g1 才 active）。

本组用例的要求（审查方指定）：
- **真 PG + 多线程**（不是 eager；SQLite 的锁语义测不出这个竞态）；
- 模型禁用（不发任何模型请求）；
- **随机 collection 命名空间**（不碰正式 collection）；
- 断言：**全部请求被受理（2xx）**、**恰好一代被采用**、
  指针与 ``is_active`` **一致**、未采用的代次留痕为 ``failed``。

多线程 + Django 事务各自独立连接，所以线程内要 ``close_old_connections()``。
"""

from __future__ import annotations

import os
import threading
import uuid

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections, connections
from django.test import override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.knowledge.models import (
    IngestedFile,
    KnowledgeLibrary,
    LibraryMember,
    MemberRole,
    VectorCollection,
)

pytestmark = pytest.mark.django_db(transaction=True)


def _require_postgres() -> None:
    if connections["default"].vendor != "postgresql":
        pytest.skip("并发初始化竞态只在真实 PG 上有意义（SQLite 锁语义不同）")


def _fresh_library(code: str) -> KnowledgeLibrary:
    """**全新库**：指针为空、没有任何登记代次（这才叫首上传）。"""
    return KnowledgeLibrary.objects.create(
        code=code,
        name=f"压测库 {code}",
        vector_collection="",   # 关键：空指针
        active_generation=0,
        next_generation=0,
    )


def _upload_concurrently(library, user, *, threads: int, prefix: str = "并发首上传"):
    """多线程同时走**视图路径**（含 ensure_library_index + ingest）。"""
    from apps.knowledge.views import IngestedFileViewSet

    results: list[dict] = []
    lock = threading.Lock()
    barrier = threading.Barrier(threads)

    def worker(index: int) -> None:
        close_old_connections()
        try:
            upload = SimpleUploadedFile(
                f"{prefix}-{index}.txt",
                f"并发内容 {index} {uuid.uuid4().hex}\n质保期 12 个月。".encode("utf-8"),
                content_type="text/plain",
            )
            request = APIRequestFactory().post(
                "/api/v1/knowledge/files/upload/",
                {"file": upload, "library": library.code},
                format="multipart",
            )
            force_authenticate(request, user=user)
            barrier.wait(timeout=30)  # 尽量同时打进去
            response = IngestedFileViewSet.as_view({"post": "upload"})(request)
            with lock:
                results.append(
                    {"index": index, "status": response.status_code,
                     "body": getattr(response, "data", None)}
                )
        except Exception as exc:  # noqa: BLE001 - 记录后在断言里体现
            with lock:
                results.append({"index": index, "status": None, "error": repr(exc)})
        finally:
            close_old_connections()

    workers = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    for item in workers:
        item.start()
    for item in workers:
        item.join(timeout=180)
    return results


def _grant_admin(library, user) -> None:
    LibraryMember.objects.create(library=library, user=user, role=MemberRole.ADMIN)


def test_concurrent_first_upload_single_winner(isolated_namespace, admin_user):
    """4 并发对**全新库**上传：全部 2xx、恰好一代 active、指针一致。"""
    _require_postgres()
    library = _fresh_library(f"load-test-{uuid.uuid4().hex[:6]}")
    _grant_admin(library, admin_user)

    results = _upload_concurrently(library, admin_user, threads=4)

    # 1) **全部被受理**：不允许 500（这正是旧实现的失败模式）
    bad = [item for item in results if not (item["status"] and 200 <= item["status"] < 300)]
    assert not bad, bad

    # 2) 指针与登记表一致
    library.refresh_from_db()
    assert library.vector_collection, "首上传后必须已发布一代"
    active = list(
        VectorCollection.objects.filter(library=library, is_active=True).values_list(
            "name", flat=True
        )
    )
    assert active == [library.vector_collection], (
        f"指针={library.vector_collection} 与 active 登记={active} 不一致"
    )

    # 3) 未采用的代次必须留痕 failed（不是悄悄留 ready），且都不是 active
    for row in VectorCollection.objects.filter(library=library).exclude(
        name=library.vector_collection
    ):
        assert row.is_active is False
        assert row.build_status in {"failed", "building"}, row.build_status

    # 4) 资料记录都在（不允许"500 导致记录消失"）
    stored = IngestedFile._base_manager.filter(library=library).count()
    assert stored == len(results), f"入库记录 {stored} != 请求数 {len(results)}"


def test_concurrent_first_upload_reuses_published_generation(isolated_namespace, admin_user):
    """第二轮并发：库已有 collection → 全部复用同一代，不再 mint 新代次。"""
    _require_postgres()
    library = _fresh_library(f"load-test-{uuid.uuid4().hex[:6]}")
    _grant_admin(library, admin_user)

    first = _upload_concurrently(library, admin_user, threads=2, prefix="第一轮")
    assert all(200 <= (item["status"] or 0) < 300 for item in first), first
    library.refresh_from_db()
    published = library.vector_collection
    generation = library.active_generation

    second = _upload_concurrently(library, admin_user, threads=4, prefix="第二轮")
    assert all(200 <= (item["status"] or 0) < 300 for item in second), second

    library.refresh_from_db()
    assert library.vector_collection == published, "已发布过就不该换代次"
    assert library.active_generation == generation
    assert VectorCollection.objects.filter(library=library, is_active=True).count() == 1


def test_index_failure_still_keeps_ingest_record(isolated_namespace, admin_user, monkeypatch):
    """索引初始化失败（Qdrant 挂）时：**资料记录仍必须存在**且返回 2xx 待处理。

    旧实现把 ``ensure_library_index`` 放在 ``ingest_file`` 之前且只捕
    ``PipelineError`` → 外部失败直接 500 且**记录根本没落库**，
    原件在磁盘上却查不到，也没有可重试对象。
    """
    _require_postgres()
    from apps.knowledge import pipeline, views

    library = _fresh_library(f"load-test-{uuid.uuid4().hex[:6]}")
    _grant_admin(library, admin_user)

    def boom(*args, **kwargs):
        raise RuntimeError("QdrantUnavailable: 模拟向量库不可用")

    monkeypatch.setattr(pipeline, "ensure_library_index", boom)
    monkeypatch.setattr(views.pipeline, "ensure_library_index", boom, raising=False)

    upload = SimpleUploadedFile("索引失败.txt", b"retry me\n", content_type="text/plain")
    request = APIRequestFactory().post(
        "/api/v1/knowledge/files/upload/",
        {"file": upload, "library": library.code},
        format="multipart",
    )
    force_authenticate(request, user=admin_user)
    response = views.IngestedFileViewSet.as_view({"post": "upload"})(request)

    # 受理（202 待处理），不是 500
    assert response.status_code in {200, 201, 202}, response.data
    # **记录一定在**，并且是"待处理"而不是"已完成"
    record = IngestedFile._base_manager.filter(library=library).first()
    assert record is not None, "索引失败导致入库记录丢失（违反保留原件可重试）"
    assert record.parse_status != "done"


def test_unused_generation_is_marked_failed_and_number_not_reused(isolated_namespace):
    """未采用的代次留痕 ``failed`` + 原因；编号**不复用**（下次分配更大编号）。"""
    _require_postgres()
    from apps.knowledge import vectorindex
    from apps.knowledge.embedding import active_profile
    from apps.knowledge.pipeline import _discard_generation

    library = _fresh_library(f"load-test-{uuid.uuid4().hex[:6]}")
    plan = vectorindex.prepare_generation(library, profile=active_profile())
    _discard_generation(plan, reason="test_unused")

    row = VectorCollection.objects.get(library=library, name=plan.collection)
    assert row.build_status == "failed"
    assert "test_unused" in row.build_error
    assert row.is_active is False

    next_plan = vectorindex.prepare_generation(library, profile=active_profile())
    assert next_plan.generation > plan.generation, "编号被复用了"


def test_ensure_index_keeps_allocation_when_remote_post_create_fails(
    isolated_namespace, monkeypatch
):
    """**占号回退回归**（审查方点名）：远端 create 成功之后抛错，
    编号与 failed 登记必须**仍然存在**，下一次分配的编号必须更大。

    这一条针对我上一版引入的真实缺陷：``ensure_library_index`` 把
    ``prepare_generation`` 包在**外层 atomic** 里，于是它内部的"占号事务"
    退化成 savepoint——远端集合已经建好，外层一抛错就把
    ``next_generation`` 与 failed 登记行**一起回滚**，下次再分配会拿到
    **同一个编号**，新数据被写进那个没人登记的孤立集合。

    关键在于**必须走 ensure_library_index**（而不是直接调 prepare_generation），
    因为缺陷只在"外层有事务"时才出现。
    """
    _require_postgres()
    from apps.knowledge import pipeline, vectorindex
    from apps.knowledge.embedding import active_profile

    library = _fresh_library(f"load-test-{uuid.uuid4().hex[:6]}")

    # 远端 collection **建成功**，随后在"建文本索引"这一步抛错
    remote_created: list[str] = []
    real_ensure_collection = vectorindex.ensure_collection

    def tracking_ensure_collection(client, name, dimension):
        real_ensure_collection(client, name, dimension)
        remote_created.append(name)   # 远端确实已建成

    monkeypatch.setattr(vectorindex, "ensure_collection", tracking_ensure_collection)

    def boom(*args, **kwargs):
        raise RuntimeError("index_build_failed: 远端集合已建成后失败")

    monkeypatch.setattr(vectorindex, "ensure_text_index", boom)

    with pytest.raises(Exception):
        pipeline.ensure_library_index(library)

    # 远端确实建过一个集合（证明"建成后失败"这个前提成立）
    assert remote_created, "前置条件不成立：远端集合没建成"
    failed_name = remote_created[0]

    # 1) **占号没有被回滚**：failed 登记行仍在
    row = VectorCollection.objects.filter(library=library, name=failed_name).first()
    assert row is not None, "占号/失败登记被回滚了——孤立集合会被下次复用"
    assert row.build_status == "failed", row.build_status

    # 2) next_generation 已被推进（编号作废、不复用）
    library.refresh_from_db()
    consumed_generation = int(library.next_generation)
    assert consumed_generation >= 1

    # 3) 修好远端之后重试：必须拿到**更大的编号**，绝不能复用 failed_name
    monkeypatch.setattr(vectorindex, "ensure_text_index", lambda *a, **k: None)
    state = pipeline.ensure_library_index(library)
    assert state["collection"] != failed_name, "复用了已作废的编号/孤立集合名"
    from apps.knowledge.pipeline import _generation_from_name

    assert _generation_from_name(state["collection"]) > _generation_from_name(failed_name)


def test_ensure_index_honours_outer_transaction_rollback_safely(
    isolated_namespace, monkeypatch
):
    """**调用方**自己包外层事务并回滚时，编号仍**绝不被复用**。

    这是上一条的强化版，也暴露了一个本质限制：如果**调用方**把
    ``ensure_library_index`` 包进它自己的 ``atomic()``，那么
    ``prepare_generation`` 内部的"占号事务"依然会退化成 savepoint，
    调用方的回滚会把占号行与 ``next_generation`` 一起带走——这**不是**
    ``ensure_library_index`` 自己能控制的（它已经不在事务里了）。

    所以安全保证不能只靠"占号一定提交"，还要有**兜底**：
    远端集合已存在但没有登记行 → 认定为孤立集合，登记 ``failed`` 留痕
    并**跳到下一个编号**。于是"编号绝不复用"在任何回滚情形下都成立。

    本用例断言的正是这个安全性质（而不是"行一定还在"——那种断言在
    调用方回滚的前提下不成立）。
    """
    _require_postgres()
    from django.db import transaction as _tx

    from apps.knowledge import pipeline, vectorindex
    from apps.knowledge.pipeline import _generation_from_name

    library = _fresh_library(f"load-test-{uuid.uuid4().hex[:6]}")
    remote_created: list[str] = []
    real_ensure_collection = vectorindex.ensure_collection

    def tracking(client, name, dimension):
        real_ensure_collection(client, name, dimension)
        remote_created.append(name)

    monkeypatch.setattr(vectorindex, "ensure_collection", tracking)

    def boom(*args, **kwargs):
        raise RuntimeError("post_create_failure")

    monkeypatch.setattr(vectorindex, "ensure_text_index", boom)

    with pytest.raises(Exception):
        with _tx.atomic():
            pipeline.ensure_library_index(library)
            # 调用方在 ensure 之后还做了别的事并失败 → 外层整体回滚

    assert remote_created, "前置条件不成立：远端集合没建成"
    orphan = remote_created[0]

    # 外层回滚后登记行确实没了（savepoint 被回滚，这是调用方的责任范围）
    # —— 但**安全性质**必须成立：下一次分配绝不能再拿到这个名字。
    monkeypatch.setattr(vectorindex, "ensure_text_index", lambda *a, **k: None)
    state = pipeline.ensure_library_index(library)

    assert state["collection"] != orphan, "复用了孤立集合名（占号回退缺陷）"
    assert _generation_from_name(state["collection"]) > _generation_from_name(orphan), (
        f"新编号 {state['collection']} 没有大于孤立集合 {orphan}"
    )
    # 孤立集合必须留痕为 failed（在下次分配时被登记）
    assert VectorCollection.objects.filter(
        library=library, name=orphan, build_status="failed"
    ).exists(), "孤立集合没有被登记留痕"

