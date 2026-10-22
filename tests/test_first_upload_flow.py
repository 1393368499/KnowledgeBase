"""全新库首传自动初始化：**不预先手工造已激活 collection**。

审查方明确要求：不能说"建库 → 首上传 → 处理 → 检索"可用，却要在测试里先
用 fixture 手工造一个已激活的 collection。这里所有用例都从**一个全新的库**开始，
唯一的准备动作是"建库"，其余全靠系统自己走通。
"""

from __future__ import annotations

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

import pytest

from apps.knowledge import vectorindex
from apps.knowledge import testing_adapter as ta
from apps.knowledge.models import (
    IngestedFile,
    KnowledgeLibrary,
    ParseStatus,
    VectorCollection,
)

pytestmark = pytest.mark.django_db

DIM = 1024


@pytest.fixture
def fresh_library(db):
    """一个**全新**的信息库：没有任何 collection、active_generation = 0。"""
    return KnowledgeLibrary.objects.create(code="brand-new", name="全新库")


def _upload(api, user, library, *, filename: str, content: bytes, **extra):
    api.force_authenticate(user=user)
    payload = {"library": library.code, "file": SimpleUploadedFile(filename, content)}
    payload.update(extra)
    return api.post("/api/v1/knowledge/files/upload/", payload, format="multipart")


@override_settings(KB_UPLOAD_PROCESS_MODE="inline", KB_TEST_ADAPTER="offline")
def test_first_upload_on_brand_new_library_initializes_index(
    api, admin_user, fresh_library, isolated_paths
):
    """建库 → 首上传：系统自动初建第一代索引并完成处理。"""
    library = fresh_library
    assert library.active_generation == 0
    assert VectorCollection.objects.filter(library=library).count() == 0

    response = _upload(
        api,
        admin_user,
        library,
        filename="首份资料.txt",
        content="质保期：设备验收合格之日起 12 个月。\n".encode(),
    )
    assert response.status_code in {200, 201}, response.content[:400]
    payload = response.json()

    # 1) 自动初建了索引，并且告诉调用方"这是初始化的"
    assert payload["index_state"]["initialized"] is True
    collection = payload["index_state"]["collection"]
    library.refresh_from_db()
    assert library.active_generation == 1
    assert library.vector_collection == collection

    # 2) 这份资料真的处理完了（解析 + 向量写入）
    record = IngestedFile.objects.get(pk=payload["file"]["id"])
    assert record.parse_status == ParseStatus.DONE, record.parse_error
    assert record.vector_status == "indexed"
    assert record.chunk_count > 0
    probe = vectorindex.build_client()
    try:
        assert vectorindex.count_points(probe, collection) == record.chunk_count
    finally:
        probe.close()


@override_settings(KB_UPLOAD_PROCESS_MODE="inline", KB_TEST_ADAPTER="offline")
def test_first_upload_then_search_hits(api, admin_user, fresh_library, isolated_paths):
    """建库 → 首上传 → 检索命中（全链路，无任何手工 collection 准备）。"""
    library = fresh_library
    response = _upload(
        api,
        admin_user,
        library,
        filename="会议纪要.txt",
        content="现场沟通记录：设备验收报告需补充安全交底记录。".encode(),
    )
    assert response.status_code in {200, 201}

    api.force_authenticate(user=admin_user)
    search = api.post(
        "/api/v1/knowledge/search/",
        {"q": "安全交底", "libraries": [library.code], "rerank": False},
        format="json",
    )
    assert search.status_code == 200
    body = search.json()
    assert body["results"], body
    assert body["results"][0]["library_code"] == library.code
    # 全新库第一代就参与了检索（说明指针确实切过去了）
    report = body["report"]
    assert report["collections"], report
    library.refresh_from_db()
    assert library.vector_collection in report["collections"]


@override_settings(KB_UPLOAD_PROCESS_MODE="inline", KB_TEST_ADAPTER="offline")
def test_second_upload_reuses_existing_index(api, admin_user, fresh_library, isolated_paths):
    """第二次上传：复用已有代次，不再重复初建、不产生多余 collection。"""
    library = fresh_library
    first = _upload(api, admin_user, library, filename="a.txt", content=b"alpha content")
    assert first.json()["index_state"]["initialized"] is True

    second = _upload(api, admin_user, library, filename="b.txt", content=b"beta content")
    assert second.status_code in {200, 201}
    state = second.json()["index_state"]
    assert state["initialized"] is False  # 复用，而不是又建一代
    assert state["collection"] == first.json()["index_state"]["collection"]
    # 只有一代（初建那次）
    assert VectorCollection.objects.filter(library=library).count() == 1


def test_rebuild_api_initializes_and_activates(api, admin_user, fresh_library, isolated_paths):
    """重建 API 也可以用来初建（管理路径），但空库必须被拒绝。"""
    library = fresh_library
    api.force_authenticate(user=admin_user)
    empty = api.post(
        "/api/v1/knowledge/rebuild/", {"library": library.code}, format="json"
    )
    # 空库不准激活：409 + 明确原因
    assert empty.status_code == 409
    assert empty.json()["activated"] is False
    assert "空索引" in empty.json()["reason"]


@override_settings(KB_UPLOAD_PROCESS_MODE="inline", KB_TEST_ADAPTER="offline")
def test_rebuild_api_after_upload_rebuilds_new_generation(
    api, admin_user, fresh_library, isolated_paths
):
    """有资料之后重建：新建一代、全量重建、coverage 通过后切换。"""
    library = fresh_library
    _upload(api, admin_user, library, filename="c.txt", content=b"gamma content")
    first_collection = KnowledgeLibrary.objects.get(pk=library.pk).vector_collection

    api.force_authenticate(user=admin_user)
    response = api.post(
        "/api/v1/knowledge/rebuild/", {"library": library.code}, format="json"
    )
    assert response.status_code == 201, response.content[:400]
    report = response.json()
    assert report["activated"] is True
    assert report["collection"] != first_collection  # 新的一代
    assert report["generation"] == 2

    library.refresh_from_db()
    assert library.vector_collection == report["collection"]
    # 旧代次登记行仍然保留（可回退）
    assert VectorCollection.objects.filter(library=library, name=first_collection).exists()


@override_settings(KB_UPLOAD_PROCESS_MODE="queue", KB_TEST_ADAPTER="offline")
def test_queue_mode_first_upload_still_initializes_index(
    api, admin_user, fresh_library, isolated_paths, monkeypatch
):
    """队列模式 + 无 worker：索引照样先初建，资料明确"待处理"。"""
    library = fresh_library
    import config.celery as celery_app

    monkeypatch.setattr(
        celery_app,
        "dispatch_processing",
        lambda file_id: {"dispatched": False, "mode": "queue", "task_id": ""},
    )
    response = _upload(api, admin_user, library, filename="queued.txt", content=b"queued body")
    assert response.status_code == 202
    payload = response.json()
    assert payload["index_state"]["initialized"] is True

    library.refresh_from_db()
    assert library.vector_collection  # 首传就已经有 collection，worker 接手时才能写
    record = IngestedFile.objects.get(pk=payload["file"]["id"])
    assert record.parse_status == ParseStatus.PENDING
    assert record.vector_status == "pending"
