"""并发 claim 的专门回归（审查意见：状态/预算必须放进原子 claim 判定）。

覆盖两条被指出的真实缺陷：

1. **旧实例读到 PENDING，另一个实例已 DONE** —— 旧实例再调用时，
   claim 必须命中 0 行（因为 ``parse_status = DONE`` 被排除），
   于是"跳过"，**不能**把状态改回 RUNNING、也不能把尝试号 +1。
   旧实现在 claim 里无条件 UPDATE，导致 DONE 检查永远看不到 DONE。
2. **预算用尽** —— 超过 ``MAX_PARSE_ATTEMPTS`` 时 claim 不生效，
   资料**不会卡在 RUNNING**（旧实现是先 claim 再在 try 外抛异常，必然留 RUNNING）。
"""

from __future__ import annotations

import pytest

from apps.knowledge import embedding as emb
from apps.knowledge import pipeline, vectorindex
from apps.knowledge.models import DocumentChunk, IngestedFile, ParseStatus
from tests.test_vector_profiles import FakeQdrant

pytestmark = pytest.mark.django_db


def _record(library, make_file, *, name="claim回归.md", count=1) -> IngestedFile:
    record = make_file(library, name=name)
    for ordinal in range(1, count + 1):
        DocumentChunk.objects.create(
            file=record, ordinal=ordinal, text=f"第{ordinal}段", token_estimate=2
        )
    IngestedFile._base_manager.filter(pk=record.pk).update(chunk_count=count)
    record.refresh_from_db()
    return record


# ------------------------------- 1) 旧实例 vs 已 DONE ----------------------


def test_stale_pending_instance_cannot_reprocess_done_record(library, make_file):
    """旧实例手里是 PENDING；库里已经是 DONE → 再调用必须"跳过"且不改任何字段。"""
    record = _record(library, make_file)
    IngestedFile._base_manager.filter(pk=record.pk).update(
        parse_status=ParseStatus.PENDING, parse_attempts=0
    )
    record.refresh_from_db()
    stale = record  # 旧实例：内存里是 PENDING

    # 另一个 worker 把它跑完了
    IngestedFile._base_manager.filter(pk=record.pk).update(
        parse_status=ParseStatus.DONE,
        parse_attempts=1,
        chunk_count=1,
        vector_status="indexed",
    )
    before = IngestedFile._base_manager.filter(pk=record.pk).values(
        "parse_status", "parse_attempts", "chunk_count", "vector_status"
    ).first()

    step = pipeline.process_file(stale, client=FakeQdrant())

    assert step.ok is True
    assert step.metrics.get("skipped") is True
    assert step.metrics.get("stale") is True
    after = IngestedFile._base_manager.filter(pk=record.pk).values(
        "parse_status", "parse_attempts", "chunk_count", "vector_status"
    ).first()
    assert after == before, f"跳过的路径不能改动任何字段：{before} → {after}"


def test_claim_on_done_record_raises_already_processed(library, make_file):
    """直接调 claim：DONE 记录抛 AlreadyProcessed，且状态/计数原封不动。"""
    record = _record(library, make_file)
    IngestedFile._base_manager.filter(pk=record.pk).update(
        parse_status=ParseStatus.DONE, parse_attempts=1
    )
    record.refresh_from_db()

    with pytest.raises(pipeline.AlreadyProcessed):
        pipeline.acquire_lease(record)

    record.refresh_from_db()
    assert record.parse_status == ParseStatus.DONE
    assert record.parse_attempts == 1  # 没有 +1
    assert record.parse_lease_token == ""  # 没有占用租约


# ------------------------------- 2) 预算用尽不留 RUNNING -------------------


def test_budget_exhausted_does_not_leave_running(library, make_file):
    """尝试次数用尽：claim 不生效，状态**不会**变成 RUNNING。"""
    record = _record(library, make_file)
    IngestedFile._base_manager.filter(pk=record.pk).update(
        parse_status=ParseStatus.FAILED,
        parse_attempts=IngestedFile.MAX_PARSE_ATTEMPTS,
    )
    record.refresh_from_db()

    with pytest.raises(pipeline.BudgetExhausted):
        pipeline.acquire_lease(record)

    record.refresh_from_db()
    assert record.parse_status == ParseStatus.FAILED  # 不是 RUNNING
    assert record.parse_attempts == IngestedFile.MAX_PARSE_ATTEMPTS
    assert record.parse_lease_token == ""


def test_process_file_budget_exhausted_raises_and_keeps_state(library, make_file):
    """process_file 走到预算用尽：抛 PipelineError，且不留下 RUNNING。"""
    record = _record(library, make_file)
    IngestedFile._base_manager.filter(pk=record.pk).update(
        parse_status=ParseStatus.FAILED,
        parse_attempts=IngestedFile.MAX_PARSE_ATTEMPTS,
    )
    record.refresh_from_db()

    with pytest.raises(pipeline.BudgetExhausted):
        pipeline.process_file(record, client=FakeQdrant())

    record.refresh_from_db()
    assert record.parse_status == ParseStatus.FAILED
    assert record.parse_lease_token == ""


def test_last_allowed_attempt_still_runs(library, make_file):
    """恰好还剩一次机会时应当允许跑（``< MAX`` 而不是 ``<=``）。"""
    record = _record(library, make_file)
    IngestedFile._base_manager.filter(pk=record.pk).update(
        parse_status=ParseStatus.FAILED,
        parse_attempts=IngestedFile.MAX_PARSE_ATTEMPTS - 1,
    )
    record.refresh_from_db()
    token = pipeline.acquire_lease(record)
    record.refresh_from_db()
    assert record.parse_status == ParseStatus.RUNNING
    assert record.parse_attempts == IngestedFile.MAX_PARSE_ATTEMPTS
    pipeline.release_lease(record, token)


# ------------------------------- 3) 重建不耗预算、复用分段 ------------------


def test_force_rebuild_does_not_consume_budget(library, make_file):
    """force（重建）不消耗普通重试预算：预算用满也能抢到。"""
    record = _record(library, make_file)
    IngestedFile._base_manager.filter(pk=record.pk).update(
        parse_status=ParseStatus.DONE,
        parse_attempts=IngestedFile.MAX_PARSE_ATTEMPTS,
    )
    record.refresh_from_db()

    token = pipeline.acquire_lease(record, force=True, consume_budget=False)
    record.refresh_from_db()
    assert record.parse_lease_token == token
    # **尝试号没有增加**：重建不是"一次失败重试"
    assert record.parse_attempts == IngestedFile.MAX_PARSE_ATTEMPTS
    pipeline.release_lease(record, token)


def _fake_embed_http(dimension: int = 1024):
    """本地假嵌入客户端（MockTransport，不走真实网络）。"""
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


def test_rebuild_reuses_chunks_without_reparsing(library, make_file):
    """重建走 reuse_chunks：复用已入库分段，不重新解析原文。

    真实重建必须**指明目标代次与目标 profile**（否则无法证明"这一代用哪套模型"，
    ``embed_chunks`` 会显式拒绝）。所以这里按真实形状 prepare 一个新代次，
    目标代次之外的线上状态一律不动。
    """
    record = _record(library, make_file, count=3)
    qdrant = FakeQdrant()
    target_profile = emb.get_profile("bge1024")
    plan = vectorindex.prepare_generation(library, profile=target_profile, client=qdrant)
    token = pipeline.acquire_lease(record, force=True, consume_budget=False)
    try:
        step = pipeline._process_file_locked(
            record,
            lease_token=token,
            client=qdrant,
            http_client=_fake_embed_http(),
            profile=target_profile,
            target_collection=plan.collection,
            target_generation=plan.generation,
            reuse_chunks=True,
        )
        assert step.metrics["parse"].get("reused") is True
        assert step.metrics["chunks"].get("chunks") == 3
        # 只写目标代次的点：库与资料的当前指针都不动
        library.refresh_from_db()
        assert library.vector_collection != plan.collection
        record.refresh_from_db()
        assert record.vector_collection_name != plan.collection
    finally:
        pipeline.release_lease(record, token)


def test_rebuild_without_chunks_refuses_clearly(library, make_file):
    """没有可复用分段时明确报错，而不是"假装重建成功"。"""
    record = make_file(library, name="无分段.md")
    token = pipeline.acquire_lease(record, force=True, consume_budget=False)
    try:
        with pytest.raises(pipeline.PipelineError, match="没有可复用的分段"):
            pipeline._process_file_locked(
                record, lease_token=token, client=FakeQdrant(), reuse_chunks=True
            )
    finally:
        pipeline.release_lease(record, token)
