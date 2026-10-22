"""离线模式（``KB_MODEL_CALLS_DISABLED=1``）的回归测试。

用途：AS711 未就绪时做界面验收——**绝不发起任何真实外呼**，
但界面仍然可用（语义检索如实标注不可用，关键字检索照常工作）。

与 ``KB_TEST_ADAPTER`` 的区别（这条要说清楚）：
- 测试适配器会**写假向量**，所以只能写测试专用 collection；
- 离线模式**什么都不写**，因此绝不可能污染正式索引。
"""

from __future__ import annotations

import httpx
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from apps.knowledge import pipeline
from apps.knowledge.embedding import active_profile
from apps.knowledge.models import DocumentChunk, IngestedFile, KnowledgeLibrary, ParseStatus
from apps.knowledge.resilience import ServiceUnavailable, model_calls_disabled

pytestmark = pytest.mark.django_db


def test_flag_reads_from_settings(db):
    assert model_calls_disabled() is False
    with override_settings(KB_MODEL_CALLS_DISABLED=True):
        assert model_calls_disabled() is True


def test_post_json_refuses_without_any_network_call(monkeypatch):
    """离线模式下 ``post_json`` **在发请求之前**就失败。"""
    from apps.knowledge import resilience

    called = {"n": 0}

    class _Client:
        def post(self, *args, **kwargs):  # pragma: no cover - 不该被调用
            called["n"] += 1
            raise AssertionError("离线模式下不应发起任何请求")

    with override_settings(KB_MODEL_CALLS_DISABLED=True):
        with pytest.raises(ServiceUnavailable) as excinfo:
            resilience.post_json(
                http=_Client(),
                url="http://as711.test/v1/embeddings",
                payload={"model": "x", "input": ["y"]},
                service="embed:test",
            )
    assert called["n"] == 0
    assert "离线模式" in str(excinfo.value)


@override_settings(KB_MODEL_CALLS_DISABLED=True)
def test_embed_texts_fails_fast_without_network():
    from apps.knowledge.embedding import embed_texts

    with pytest.raises(ServiceUnavailable) as excinfo:
        embed_texts(["质保期"], profile=active_profile())
    assert "离线模式" in str(excinfo.value)


@override_settings(KB_MODEL_CALLS_DISABLED=True)
def test_rerank_and_ocr_fail_fast():
    from apps.knowledge.ocr import OcrClient, OcrError, OcrProfile
    from apps.knowledge.rerank import RerankClient, RerankError, RerankProfile

    reranker = RerankClient(RerankProfile(base_url="http://as711.test:8002/v1", model="m"))
    with pytest.raises(RerankError):
        reranker.rerank(query="q", documents=["a"])

    ocr = OcrClient(OcrProfile(base_url="http://as711.test:8003/v1", model="m"))
    with pytest.raises(OcrError):
        ocr.recognize_image(b"fake")


@override_settings(KB_MODEL_CALLS_DISABLED=True)
def test_search_degrades_instantly_and_keeps_keyword(db, library, make_file, offline_paths):
    """离线模式下检索**立刻**降级（不等超时），关键字那一路照常工作。"""
    from apps.knowledge.parsers import ParsedBlock

    record = make_file(library, name="离线检索.txt")
    DocumentChunk.objects.create(
        file=record, ordinal=1, text="预付款比例 30%，验收后付至 95%", token_estimate=8
    )
    IngestedFile._base_manager.filter(pk=record.pk).update(
        parse_status=ParseStatus.DONE, chunk_count=1
    )
    KnowledgeLibrary.objects.filter(pk=library.pk).update(vector_collection="kb-offline-g1")
    record.refresh_from_db()
    library.refresh_from_db()

    import time

    started = time.monotonic()
    outcome = pipeline.search(
        query="预付款比例",
        libraries=[library],
        permission_codes=[record.permission_code],
        use_rerank=False,
        client=None,
    )
    elapsed = time.monotonic() - started

    report = outcome["report"]
    assert report["dense_skipped_by_breaker"] is True, {
        "collections": report.get("collections"),
        "degraded": report.get("degraded"),
        "reason": report.get("degraded_reason"),
        "profiles": report.get("collection_profiles"),
    }
    assert report["degraded"] is True
    assert "离线模式" in report["degraded_reason"]
    assert report["db_keyword_hits"] >= 1
    assert outcome["results"]


@override_settings(KB_MODEL_CALLS_DISABLED=True)
def test_indexing_fails_clearly_without_writing_fake_vectors(db, library, make_file, offline_paths):
    """离线模式下不能建索引：必须**明确失败**，而不是写假向量进正式库。"""
    from apps.knowledge import vectorindex
    from tests.test_vector_profiles import FakeQdrant

    record = make_file(library, name="离线入库.txt")
    DocumentChunk.objects.create(file=record, ordinal=1, text="内容", token_estimate=2)
    KnowledgeLibrary.objects.filter(pk=library.pk).update(vector_collection="kb-offline-g2")
    library.refresh_from_db()
    record.refresh_from_db()

    qdrant = FakeQdrant()
    with pytest.raises(pipeline.PipelineError):
        pipeline.embed_chunks(record, profile=active_profile(), client=qdrant)
    # 一个点都没写：离线模式绝不产生"看着像成功"的假向量
    assert qdrant.points.get("kb-offline-g2", {}) == {}


@override_settings(KB_SEARCH_DENSE_ENABLED=False, KB_MODEL_CALLS_DISABLED=False)
def test_dense_can_be_disabled_by_config(db, library, make_file, offline_paths):
    """``KB_SEARCH_DENSE_ENABLED=0`` 同样跳过稠密检索，且报告里说明原因。"""
    DocumentChunk.objects.create(
        file=make_file(library, name="关稠密.txt"), ordinal=1, text="质保期 12 个月", token_estimate=4
    )
    record = IngestedFile.objects.filter(library=library).first()
    KnowledgeLibrary.objects.filter(pk=library.pk).update(vector_collection="kb-x-g1")
    library.refresh_from_db()

    outcome = pipeline.search(
        query="质保期",
        libraries=[library],
        permission_codes=[record.permission_code],
        use_rerank=False,
    )
    report = outcome["report"]
    assert report["dense_skipped_by_breaker"] is True
    assert "KB_SEARCH_DENSE_ENABLED" in report["degraded_reason"]


@override_settings(KB_MODEL_CALLS_DISABLED=True)
def test_upload_still_persists_bytes_even_if_indexing_fails(
    api, admin_user, library, offline_paths
):
    """离线模式下上传：原始字节照常落盘入库，处理失败但**不丢原件**。"""
    api.force_authenticate(user=admin_user)
    response = api.post(
        "/api/v1/knowledge/files/upload/",
        {
            "library": library.code,
            "file": SimpleUploadedFile("offline.txt", "离线上传内容".encode()),
        },
        format="multipart",
    )
    assert response.status_code in {200, 201, 202}, response.content[:300]
    payload = response.json()
    record = IngestedFile.objects.get(pk=payload["file"]["id"])

    assert record.raw_path  # 原件落盘了
    assert record.parse_status in {ParseStatus.FAILED, ParseStatus.PENDING}
    assert record.vector_status != "indexed"
    # 失败原因必须是"可操作"的那句话，而不是裸异常或 500
    if record.parse_status == ParseStatus.FAILED:
        assert "不可用" in record.parse_error or "未启用" in record.parse_error
