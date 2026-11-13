"""并发保护与重建的专门回归用例（审查意见 21:15 第 2/3/4/5/6 条）。

这些点此前**没有**对应测试：全量套件通过并不能证明并发保护正确。
每个用例都直接对着一条被指出的真实缺陷：

1. 过期 RUNNING 的接管（原子 claim：状态 + 尝试号一起分配，不再"同状态 bump"）；
2. 已完成资料的重建**不消耗**解析失败重试预算；
3. 重建目标分支**不改当前指针**（整库激活前 online 指针不动）；
4. 远端建 collection 成功后抛异常 → 编号**作废不复用**（不能复用孤立 collection 名）；
5. 抢占失败 / 旧 owner 写入被拒。
"""

from __future__ import annotations

import datetime

import pytest
from django.utils import timezone

from apps.knowledge import embedding as emb
from apps.knowledge import pipeline, vectorindex
from apps.knowledge.models import (
    DocumentChunk,
    IngestedFile,
    KnowledgeLibrary,
    ParseStatus,
    VectorCollection,
)
from tests.test_vector_profiles import FakeQdrant

pytestmark = pytest.mark.django_db


def _fake_embed_http(dimension: int = 1024):
    """一个只回假向量的 httpx 客户端（离线守卫允许，因为不走真实外呼）。"""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        payload = _json.loads(request.content.decode())
        texts = payload.get("input") or []
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": i, "embedding": [0.01 * (i + 1)] * dimension}
                    for i in range(len(texts))
                ]
            },
        )

    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


def _chunked(library, make_file, *, name="接管用例.md", count=2) -> IngestedFile:
    # 显式用 PENDING：claim 现在会把 DONE 记录排除掉（这正是要验证的保护），
    # 所以测租约机制本身时必须给一个"可处理"的初始状态。
    record = make_file(library, name=name, status=ParseStatus.PENDING)
    for ordinal in range(1, count + 1):
        DocumentChunk.objects.create(
            file=record, ordinal=ordinal, text=f"第{ordinal}段内容", token_estimate=4
        )
    IngestedFile._base_manager.filter(pk=record.pk).update(chunk_count=count)
    record.refresh_from_db()
    return record


def _expire_lease(record: IngestedFile) -> None:
    IngestedFile._base_manager.filter(pk=record.pk).update(
        parse_lease_expires_at=timezone.now() - datetime.timedelta(seconds=1)
    )


# ------------------------------------------------ 1) 过期 RUNNING 接管 ----


def test_expired_running_takeover_succeeds_and_bumps_attempt(library, make_file):
    """一个 worker 崩在 RUNNING，租约过期后另一个 worker **能接管**。

    旧实现的缺陷：接管会再调一次 ``_guarded_status(RUNNING, bump_attempts=True)``，
    而模型禁止"同状态递增尝试次数" → 接管直接抛错，资料永久停在 RUNNING。
    现在 claim 把"接管"定义成一次原子写：置 RUNNING + 尝试号 +1。
    """
    record = _chunked(library, make_file)
    # 模拟"另一个 worker 抢到了租约但崩了"：RUNNING + 租约已过期
    IngestedFile._base_manager.filter(pk=record.pk).update(
        parse_status=ParseStatus.RUNNING,
        parse_attempts=1,
        parse_lease_token="dead-worker-token",
        parse_lease_started_at=timezone.now() - datetime.timedelta(hours=2),
        parse_lease_expires_at=timezone.now() - datetime.timedelta(seconds=1),
    )
    record.refresh_from_db()

    token = pipeline.acquire_lease(record)

    record.refresh_from_db()
    assert record.parse_lease_token == token
    assert record.parse_status == ParseStatus.RUNNING  # claim 就是把它置为 RUNNING
    assert record.parse_attempts == 2  # 接管 = 新的一次尝试
    assert record.parse_error == ""
    pipeline.release_lease(record, token)


def test_takeover_does_not_raise_same_status_bump_error(library, make_file):
    """回归到具体异常：接管路径里不能再出现"同状态递增尝试次数"的错误。"""
    record = _chunked(library, make_file)
    IngestedFile._base_manager.filter(pk=record.pk).update(
        parse_status=ParseStatus.RUNNING,
        parse_attempts=1,
        parse_lease_token="crashed",
        parse_lease_expires_at=timezone.now() - datetime.timedelta(seconds=5),
    )
    record.refresh_from_db()
    # 不应抛 ImmutableRecordError
    token = pipeline.acquire_lease(record)
    assert token
    pipeline.release_lease(record, token)


def test_active_lease_cannot_be_stolen(library, make_file):
    """租约还没过期：第二个 worker 抢不到（不能重复处理）。"""
    record = _chunked(library, make_file)
    token = pipeline.acquire_lease(record)
    try:
        with pytest.raises(pipeline.LeaseNotAcquired):
            pipeline.acquire_lease(record)
    finally:
        pipeline.release_lease(record, token)


def test_stale_owner_write_rejected_after_takeover(library, make_file):
    """旧 owner 的租约被接管后，它写状态必须被 CAS 拒绝。"""
    record = _chunked(library, make_file)
    stale = pipeline.acquire_lease(record)
    _expire_lease(record)
    record.refresh_from_db()
    fresh = pipeline.acquire_lease(record)
    try:
        with pytest.raises(pipeline.LeaseLost):
            pipeline._guarded_status(record, stale, ParseStatus.RUNNING)
    finally:
        pipeline.release_lease(record, fresh)


def test_process_file_skips_after_another_worker_finished(library, make_file):
    """DONE/上限判断在抢到租约之后：另一个 worker 已完成时，本次应"跳过"。"""
    record = _chunked(library, make_file)
    IngestedFile._base_manager.filter(pk=record.pk).update(
        parse_status=ParseStatus.DONE, vector_status="indexed"
    )
    record.refresh_from_db()
    step = pipeline.process_file(record, client=FakeQdrant())
    assert step.metrics.get("skipped") is True
    record.refresh_from_db()
    assert record.parse_attempts == 0  # 跳过的路径不消耗尝试号


# ------------------------------------- 2) 重建不吃解析失败预算 ----


def test_rebuild_of_done_file_does_not_consume_parse_budget(library, make_file):
    """已完成的资料重建时**不消耗** MAX_PARSE_ATTEMPTS 预算。

    换模型/重建本来就要反复重嵌入；如果每次都算一次"解析失败重试"，
    正常运维几次就把上限耗光，之后连正常上传都处理不了。
    """
    record = _chunked(library, make_file)
    IngestedFile._base_manager.filter(pk=record.pk).update(
        parse_status=ParseStatus.DONE, parse_attempts=IngestedFile.MAX_PARSE_ATTEMPTS
    )
    record.refresh_from_db()

    # **必须用 force=True**：DONE + 预算用满的记录，非 force 的 claim 会（正确地）
    # 拒绝；重建走的是 force 通道，且 force 不递增尝试号。
    token = pipeline.acquire_lease(record, force=True, consume_budget=False)
    try:
        record.refresh_from_db()
        assert record.parse_lease_token == token
        # 上限已经用满，重建仍然成功且**没有再多消耗一次预算**
        assert record.parse_attempts == IngestedFile.MAX_PARSE_ATTEMPTS
        # 重复写入同样的分段是幂等的，不报错也不重复插入
        chunks = list(record.chunks.order_by("ordinal"))
        step = pipeline.write_chunks(record, [])
        assert step.metrics.get("chunks") == len(chunks)
        assert DocumentChunk.objects.filter(file=record).count() == len(chunks)
    finally:
        pipeline.release_lease(record, token)


def test_rebuild_of_done_file_reuses_chunks_and_does_not_touch_budget(library, make_file):
    """重建全流程：复用分段 + 不耗预算 + 线上状态一个字段都不动。

    这条用例原来直接调 ``_process_file_locked(reuse_chunks=True)``（无目标代次），
    那是**旧语义**：``reuse_chunks`` 当时只在“重建分支”里被用到，测试靠它间接验证
    重建。真实重建入口是 :func:`rebuild_library`：它自己取冻结、自己 prepare 新代次、
    不走 ``process_file/hold_lease/acquire_lease``，所以**根本不会碰解析预算与
    线上状态**。这里改成对着真实入口断言（断言比原来更强，不是放宽）：
    预算不变、分段数不变、解析状态不变、线上指针不变、目标代次里有点。
    """
    record = _chunked(library, make_file)
    qdrant = FakeQdrant()
    online_profile = emb.get_profile("bge1024")
    # 让“线上”有一个**已登记**的旧代次：任何指针改动都能被发现。
    # 必须登记，否则 validate_activation 的"指向未登记 collection"检查会（正确地）
    # 拒绝切换——那条检查本身也是被保护的行为。
    VectorCollection.objects.create(
        name="kb-online-old",
        dimension=1024,
        embedding_model=online_profile.model,
        model_version=online_profile.model_version,
        profile=online_profile.name,
        base_url=online_profile.base_url,
        library=library,
        storage_mode="server",
        storage_path="data/vector_data/qdrant",
        is_active=True,
        build_status="ready",
    )
    KnowledgeLibrary.objects.filter(pk=library.pk).update(
        active_generation=99, vector_collection="kb-online-old"
    )
    IngestedFile._base_manager.filter(pk=record.pk).update(
        parse_status=ParseStatus.DONE,
        parse_attempts=IngestedFile.MAX_PARSE_ATTEMPTS,
        vector_collection="kb-online-old",
        vector_collection_name="kb-online-old",
        vector_generation=99,
        vector_status="indexed",
        chunk_count=2,
        embedded_chunk_count=2,
    )
    record.refresh_from_db()
    chunks_before = list(
        DocumentChunk.objects.filter(file=record).order_by("ordinal").values_list("pk", "text")
    )

    report = pipeline.rebuild_library(
        library,
        profile=online_profile,
        client=qdrant,
        http_client=_fake_embed_http(),
    )

    assert report["files_failed"] == []
    assert report["activated"] is True
    # 复用分段：没有重新解析，分段表逐行未变
    assert (
        list(
            DocumentChunk.objects.filter(file=record)
            .order_by("ordinal")
            .values_list("pk", "text")
        )
        == chunks_before
    )
    record.refresh_from_db()
    # 解析预算一格都没动（重建不吃解析失败预算）
    assert record.parse_attempts == IngestedFile.MAX_PARSE_ATTEMPTS
    assert record.parse_status == ParseStatus.DONE
    # 切换成功后线上状态才切到新代次（这是"切换"该做的事，不是重建中途做的）
    assert record.vector_collection_name == report["collection"]
    assert record.vector_generation == report["generation"]
    assert report["generation"] != 99
    # 冻结窗口内目标代次里确实写进了这份资料的全部点
    assert qdrant.count(collection_name=report["collection"]).count == 2


# ------------------------------- 3) 重建目标分支不改当前指针 ----


def test_rebuild_target_write_does_not_move_any_pointer(library, make_file):
    """重建写目标代次时，**库指针 + 资料的全部三个指针字段**都必须保持不变。

    三个字段是 ``vector_collection`` / ``vector_collection_name`` /
    ``vector_generation``。早期只断言了后两个，漏掉了 ``vector_collection``——
    而它正是"完成写入"那条路径上会被改掉的那个（审查指出）。
    """
    record = _chunked(library, make_file)
    qdrant = FakeQdrant()
    target_profile = emb.get_profile("bge1024")
    plan = vectorindex.prepare_generation(library, profile=target_profile, client=qdrant)
    KnowledgeLibrary.objects.filter(pk=library.pk).update(
        active_generation=99, vector_collection="kb-online-old"
    )
    # 让资料自己先指向"线上旧代次"，任何指针改动都能被发现
    IngestedFile._base_manager.filter(pk=record.pk).update(
        parse_status=ParseStatus.DONE,
        vector_collection="kb-online-old",
        vector_collection_name="kb-online-old",
        vector_generation=99,
        vector_status="indexed",
        chunk_count=2,
        embedded_chunk_count=2,
    )
    library.refresh_from_db()
    # 先按真实顺序领租约（claim 自己会把状态置 RUNNING，那是 acquire_lease 的职责），
    # 再对"领到租约之后"的线上状态做快照：重建**从这一刻起**不许改动任何字段。
    token = pipeline.acquire_lease(record, force=True, consume_budget=False)
    record.refresh_from_db()

    pointer_snapshot = {
        "parse_status": record.parse_status,
        "parse_error": record.parse_error,
        "parse_attempts": record.parse_attempts,
        "chunk_count": record.chunk_count,
        "embedded_chunk_count": record.embedded_chunk_count,
        "vector_status": record.vector_status,
        "vector_collection": record.vector_collection,
        "vector_collection_name": record.vector_collection_name,
        "vector_generation": record.vector_generation,
    }

    step = pipeline._process_file_locked(
        record,
        lease_token=token,
        client=qdrant,
        http_client=_fake_embed_http(),
        # **重建必须显式给出目标 profile**：不给就无法证明"这一代用哪套模型"，
        # embed_chunks 会显式拒绝（这正是要被保护的行为）。
        profile=target_profile,
        target_collection=plan.collection,
        target_generation=plan.generation,
        reuse_chunks=True,
    )
    assert step.ok is True
    assert step.metrics["index"]["rebuild"] is True

    library.refresh_from_db()
    assert library.active_generation == 99
    assert library.vector_collection == "kb-online-old"

    record.refresh_from_db()
    assert {
        "parse_status": record.parse_status,
        "parse_error": record.parse_error,
        "parse_attempts": record.parse_attempts,
        "chunk_count": record.chunk_count,
        "embedded_chunk_count": record.embedded_chunk_count,
        "vector_status": record.vector_status,
        "vector_collection": record.vector_collection,
        "vector_collection_name": record.vector_collection_name,
        "vector_generation": record.vector_generation,
    } == pointer_snapshot, "重建不得改动任何当前生效的线上状态（含解析状态与计数）"
    # 点确实写进了目标 collection（重建成果由报告承载）
    assert qdrant.points.get(plan.collection), "目标代次里应当已经有点"
    # 而目标代次登记行在激活之前不得被标成已上线
    assert VectorCollection.objects.filter(library=library, is_active=True).count() == 0


# --------------------------- 3b) 同维异模型 / 旧实例 / 失败重建 ----


def _register(library, name: str, *, profile=None, is_active=False):
    profile = profile or emb.get_profile("bge1024")
    return VectorCollection.objects.create(
        name=name,
        dimension=profile.dimension,
        embedding_model=profile.model,
        model_version=profile.model_version,
        profile=profile.name,
        base_url=profile.base_url,
        library=library,
        storage_mode="server",
        storage_path="data/vector_data/qdrant",
        is_active=is_active,
        build_status="ready",
    )


def test_embed_chunks_refuses_explicit_profile_with_same_dim_other_model(library, make_file):
    """**同维不同模型**：显式传入的 profile 与登记快照不一致时必须拒绝。

    1024 维是"当前模型"的维度，但不是它的身份：另一个同样返回 1024 维的模型
    （不同 model / model_version / 端点）如果被允许写进同一 collection，
    索引里就会混进两套语义空间的向量 —— 检索结果看起来正常，实际全是错的。
    """
    record = _chunked(library, make_file)
    _register(library, "kb-same-dim-g1", is_active=True)
    KnowledgeLibrary.objects.filter(pk=library.pk).update(
        active_generation=1, vector_collection="kb-same-dim-g1"
    )
    library.refresh_from_db()

    same_dim_other_model = emb.ProfileInfo(
        name="bge1024",
        model="another-1024-model",
        model_version="v9",
        dimension=1024,
        base_url="http://172.16.100.222:8000/v1",
    )
    qdrant = FakeQdrant()
    with pytest.raises(pipeline.PipelineError, match="登记快照不一致"):
        pipeline.embed_chunks(
            record, profile=same_dim_other_model, client=qdrant, http_client=_fake_embed_http()
        )
    assert qdrant.points == {}, "被拒的写入一个点都不许落库"


def test_embed_chunks_refuses_target_generation_of_another_library(library, make_file):
    """目标代次必须**属于本库**且已登记：跨库/未登记的目标一律拒绝。"""
    record = _chunked(library, make_file)
    other_library = KnowledgeLibrary.objects.create(code="other-lib", name="别的库")
    other_plan = vectorindex.prepare_generation(
        other_library, profile=emb.get_profile("bge1024"), client=FakeQdrant()
    )
    qdrant = FakeQdrant()
    with pytest.raises(pipeline.PipelineError, match="不是信息库"):
        pipeline.embed_chunks(
            record,
            profile=emb.get_profile("bge1024"),
            client=qdrant,
            http_client=_fake_embed_http(),
            target_collection=other_plan.collection,
            target_generation=other_plan.generation,
        )
    assert qdrant.points == {}, "跨库目标一个点都不许写"


def test_stale_file_instance_cannot_move_pointer_back(library, make_file):
    """**旧实例不能把指针改回旧代**：切换后用旧实例做增量嵌入也不许回退指针。

    这是真实竞态：worker 在切换前拿到 file 实例（``vector_collection_name`` 还是
    旧代），切换之后它才醒来写回。如果信这个缓存值，档案指针就被改回旧代，
    检索（读新代）立刻漏掉这份资料，而且是静默的。
    """
    record = _chunked(library, make_file)
    _register(library, "kb-live-g1", is_active=False)
    _register(library, "kb-live-g2", is_active=True)
    KnowledgeLibrary.objects.filter(pk=library.pk).update(
        active_generation=1, vector_collection="kb-live-g1"
    )
    IngestedFile._base_manager.filter(pk=record.pk).update(
        vector_collection="kb-live-g1",
        vector_collection_name="kb-live-g1",
        vector_generation=1,
        embedded_chunk_count=0,
        vector_status="pending",
    )
    # **旧实例**：它记着 g1
    stale = IngestedFile._base_manager.get(pk=record.pk)
    assert stale.vector_collection_name == "kb-live-g1"

    # 别人把线上切到了 g2（同一 profile，另一代 collection）
    KnowledgeLibrary.objects.filter(pk=library.pk).update(
        active_generation=2, vector_collection="kb-live-g2"
    )
    qdrant = FakeQdrant()
    pipeline.embed_chunks(stale, client=qdrant, http_client=_fake_embed_http())

    stale.refresh_from_db()
    assert stale.vector_collection_name == "kb-live-g2", "旧实例把指针改回了旧代"
    assert stale.vector_generation == 2
    assert qdrant.points.get("kb-live-g2"), "向量必须写进**当前生效**的那一代"
    assert not qdrant.points.get("kb-live-g1"), "不许再往旧代写点"


def test_failed_rebuild_keeps_online_state_and_releases_gate(library, make_file):
    """失败重建：**线上状态/计数/指针一个都不动**，且必须释放自己的冻结。

    失败原因用"资料没有可复用分段"（确定性失败）。失败之后：
    * 目标代次标 failed（编号不复用、不会被激活）；
    * 库里没有任何冻结残留（否则整库被永久冻死）；
    * 资料的状态、计数、三个指针字段全部保持原样。
    """
    from apps.knowledge.writegate import gate_state

    good = _chunked(library, make_file, name="好资料.md")
    # 没有分段的资料：重建时"没有可复用分段"是**确定性失败**（不需要删任何东西，
    # 分段表是 append-only 的，删不掉也不该删）
    broken = make_file(library, name="无分段.md", status=ParseStatus.DONE)
    _register(library, "kb-old-g1", is_active=True)
    KnowledgeLibrary.objects.filter(pk=library.pk).update(
        active_generation=1, vector_collection="kb-old-g1"
    )
    IngestedFile._base_manager.filter(pk__in=[good.pk, broken.pk]).update(
        vector_collection="kb-old-g1",
        vector_collection_name="kb-old-g1",
        vector_generation=1,
        parse_status=ParseStatus.DONE,
        chunk_count=2,
        embedded_chunk_count=2,
        vector_status="indexed",
    )
    IngestedFile._base_manager.filter(pk=broken.pk).update(
        chunk_count=0, embedded_chunk_count=0
    )
    before = {
        row.pk: (
            row.parse_status,
            row.parse_attempts,
            row.chunk_count,
            row.embedded_chunk_count,
            row.vector_status,
            row.vector_collection,
            row.vector_collection_name,
            row.vector_generation,
        )
        for row in IngestedFile._base_manager.filter(pk__in=[good.pk, broken.pk])
    }
    assert before[broken.pk][2] == 0

    report = pipeline.rebuild_library(
        library,
        profile=emb.get_profile("bge1024"),
        client=FakeQdrant(),
        http_client=_fake_embed_http(),
    )

    assert report["activated"] is False
    assert [item["file_id"] for item in report["files_failed"]] == [broken.pk]
    # 目标代次标 failed 且没被激活
    target = VectorCollection.objects.filter(library=library, name=report["collection"]).first()
    assert target is not None and target.build_status == "failed"
    assert target.build_error
    assert VectorCollection.objects.filter(library=library, is_active=True).count() == 1
    assert VectorCollection.objects.filter(library=library, is_active=True).first().name == "kb-old-g1"
    # **冻结必须被释放**（否则整库被永久冻死）
    assert gate_state(library.pk)["frozen"] is False
    # 线上状态一个字段都没动
    after = {
        row.pk: (
            row.parse_status,
            row.parse_attempts,
            row.chunk_count,
            row.embedded_chunk_count,
            row.vector_status,
            row.vector_collection,
            row.vector_collection_name,
            row.vector_generation,
        )
        for row in IngestedFile._base_manager.filter(pk__in=[good.pk, broken.pk])
    }
    assert after == before, "失败重建不得改动任何线上状态/计数/指针"
    library.refresh_from_db()
    assert library.vector_collection == "kb-old-g1"
    assert library.active_generation == 1


# --------------------------- 4) 远端建成后抛异常：编号不复用 ----


class _ExplodingQdrant(FakeQdrant):
    """前 N 次调用正常，之后在"建 text index"这一步抛异常。"""

    def __init__(self, *, fail_on_text_index=True):
        super().__init__()
        self.fail_on_text_index = fail_on_text_index
        self.created: list[str] = []

    def create_collection(self, collection_name, vectors_config, **kwargs):
        self.created.append(collection_name)
        return super().create_collection(collection_name, vectors_config, **kwargs)

    def create_payload_index(self, collection_name, field_name, field_schema, **kwargs):
        if self.fail_on_text_index:
            raise RuntimeError("模拟：collection 建好了，但建索引时超时")
        return super().create_payload_index(
            collection_name, field_name, field_schema, **kwargs
        )


def test_prepare_failure_after_remote_create_retires_the_number(library, monkeypatch):
    """远端 collection 建成功后抛异常：编号作废，下一次必须拿**新编号**。

    这里让 ``ensure_text_index`` 抛错来模拟"collection 已经建好、后续步骤超时"，
    因为真实路径里它紧跟在建 collection 之后（而且它自己吞异常，必须显式替换）。
    """
    qdrant = FakeQdrant()
    profile = emb.get_profile("bge1024")
    created: list[str] = []

    original_create = qdrant.create_collection

    def tracking_create(collection_name, vectors_config, **kwargs):
        created.append(collection_name)
        return original_create(collection_name, vectors_config, **kwargs)

    monkeypatch.setattr(qdrant, "create_collection", tracking_create)
    monkeypatch.setattr(
        vectorindex,
        "ensure_text_index",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("模拟：建索引阶段超时")),
    )

    with pytest.raises(RuntimeError):
        vectorindex.prepare_generation(library, profile=profile, client=qdrant)

    # 登记行存在但标成 failed：编号被占用，不会被复用
    failed = list(VectorCollection.objects.filter(library=library, build_status="failed"))
    assert len(failed) == 1, [
        (row.name, row.build_status) for row in VectorCollection.objects.filter(library=library)
    ]
    assert failed[0].build_error
    assert failed[0].name in created  # 远端确实建过这个名字

    # 下一次 prepare：必须拿新编号，且不能再落到那个失败的名字上
    monkeypatch.undo()
    qdrant2 = FakeQdrant()
    plan = vectorindex.prepare_generation(library, profile=profile, client=qdrant2)
    assert plan.collection != failed[0].name
    assert plan.generation > 0
    # 第二次成功 → ready
    assert (
        VectorCollection.objects.get(library=library, name=plan.collection).build_status
        == "ready"
    )


def test_activation_refuses_non_ready_registration(library, make_file):
    """building / failed 的代次不允许被切换上线（空索引不能变成线上索引）。"""
    record = _chunked(library, make_file)
    qdrant = FakeQdrant()
    plan = vectorindex.prepare_generation(
        library, profile=emb.get_profile("bge1024"), client=qdrant
    )
    VectorCollection.objects.filter(library=library, name=plan.collection).update(
        build_status="failed", build_error="模拟失败"
    )
    with pytest.raises(vectorindex.VectorIndexError, match="构建状态"):
        vectorindex.validate_activation(library, plan, client=qdrant)


def test_prepare_keeps_counter_after_failure(library, monkeypatch):
    """远端失败后计数器**不回退**：编号只增不减。"""
    qdrant = FakeQdrant()
    profile = emb.get_profile("bge1024")
    monkeypatch.setattr(
        vectorindex,
        "ensure_text_index",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("模拟超时")),
    )
    with pytest.raises(RuntimeError):
        vectorindex.prepare_generation(library, profile=profile, client=qdrant)
    library.refresh_from_db()
    assert library.next_generation == 1  # 已经推进，不回滚
