"""通过**真实 multipart API** 覆盖上传全流程的回归测试。

为什么单独一批（审查方要求）：
手工构造模型（直接 ``IngestedFile.objects.create``）可以让很多断言"看起来通过"，
但它绕过了真实的 HTTP 入口、真实的 multipart 解析、真实的落盘与状态机。
这里所有用例都走 ``POST /api/v1/knowledge/files/upload/``（DRF APIClient 的
multipart 上传），断言首次上传、重复上传、失败重试、队列模式、关键词可搜。
"""

from __future__ import annotations

import io
import os
import stat

import pytest
from django.test import override_settings

from apps.knowledge import vectorindex
from apps.knowledge import testing_adapter as ta
from apps.knowledge.embedding import active_profile
from apps.knowledge.models import (
    DocumentChunk,
    IngestedFile,
    KnowledgeLibrary,
    ParseStatus,
    VectorCollection,
)

pytestmark = pytest.mark.django_db

DIM = 1024


@pytest.fixture
def library_setup(library, isolated_paths):
    """准备一个测试专用 collection 并把库指针指过去；**按需开客户端**。

    为什么不在夹具里提前开：本地嵌入模式的 Qdrant 由文件锁串行化，同一目录
    开第二个客户端会 RuntimeError。所以这里只登记元数据，客户端由具体用例
    通过 :func:`_open_vector_store` 懒加载，并把同一个实例注入流水线。
    """
    return library


def _force_pending_source(monkeypatch):
    """让上传时**新建的资料**处于 PENDING。

    conftest 的 ``make_file`` 默认造 DONE 的"既有资料"（API 列表/统计用例需要），
    但上传流水线必须从 PENDING 开始，否则 ``process_file`` 会走"已处理，跳过"
    分支，测试就变成假通过。
    """

    from apps.knowledge.models import IngestedFile as Model

    original = Model._meta.get_field("parse_status").default
    monkeypatch.setattr(
        Model._meta.get_field("parse_status"), "default", "pending", raising=False
    )
    return original


def _open_vector_store(library, isolated_paths):
    """开一个客户端 + 建测试 collection + 注入流水线。返回 (client, name, restore)。"""
    from apps.knowledge import pipeline as pipeline_module

    client = vectorindex.build_client()
    name = ta.test_collection_name(library.pk, 1)
    vectorindex.ensure_collection(client, name, DIM)
    VectorCollection.objects.create(
        name=name,
        dimension=DIM,
        embedding_model=active_profile().model,
        model_version=active_profile().model_version,
        profile=active_profile().name,
        library=library,
        storage_mode="embedded",
        storage_path=str(isolated_paths["vector"]),
        is_active=True,
    )
    KnowledgeLibrary.objects.filter(pk=library.pk).update(
        active_generation=1, vector_collection=name
    )
    library.refresh_from_db()

    original = pipeline_module.QDRANT_CLIENT_FACTORY
    pipeline_module.QDRANT_CLIENT_FACTORY = lambda: client

    def restore() -> None:
        pipeline_module.QDRANT_CLIENT_FACTORY = original
        client.close()

    return client, name, restore


def _upload(api, user, library, *, filename: str, content: bytes, **extra):
    """通过**真实 multipart** 上传。

    必须用 ``SimpleUploadedFile`` 带上文件名：裸 ``BytesIO`` 会让 DRF 收到空
    filename，白名单校验直接把请求判成"不支持的文件类型"（这个坑踩过）。
    """
    from django.core.files.uploadedfile import SimpleUploadedFile

    api.force_authenticate(user=user)
    upload = SimpleUploadedFile(filename, content)
    payload = {"library": library.code, "file": upload}
    payload.update(extra)
    return api.post("/api/v1/knowledge/files/upload/", payload, format="multipart")


# --------------------------------------------------------------- 首次上传 ----


def test_upload_persists_bytes_and_records_raw_path(api, admin_user, library_setup, isolated_paths):
    """**首次**通过真实 multipart 上传：记录里必须有 raw_path/raw_size_bytes。

    这是审查方指出的具体缺陷：services.ingest_file 早先建记录时漏写这两个字段，
    结果上传成功后 process 接口会直接拒绝（"没有原始字节"）。
    """
    library = library_setup
    body = "华东石化检维修 技术协议 质保期 12 个月\n".encode("utf-8")

    response = _upload(api, admin_user, library, filename="技术协议.txt", content=body)
    assert response.status_code in {200, 201, 202}, response.content[:400]
    payload = response.json()

    record = IngestedFile.objects.get(pk=payload["file"]["id"])
    assert record.raw_path, "首次上传必须在创建时就写入 raw_path"
    assert record.raw_size_bytes == len(body)
    assert record.size_bytes == len(body)
    assert payload["stored"]["reused"] is False
    assert payload["stored"]["read_only"] is True
    # 落盘内容可回读且哈希自洽
    from apps.knowledge import storage

    assert storage.read_blob(record.content_sha256) == body


def test_upload_repeat_returns_duplicate_and_reuses_bytes(api, admin_user, library_setup, isolated_paths):
    """重复上传同一份内容：命中去重、复用已落盘副本，不产生第二条记录。"""
    library = library_setup
    body = b"same-content-for-dedupe"

    first = _upload(api, admin_user, library, filename="重复.txt", content=body)
    assert first.status_code in {200, 201, 202}
    second = _upload(api, admin_user, library, filename="重复.txt", content=body)
    payload = second.json()

    assert payload["created"] is False
    assert payload["reason"] == "duplicate_content"
    assert payload["stored"]["reused"] is True
    assert IngestedFile.objects.filter(library=library).count() == 1
    # 没写新字节 → 不是"补齐原件"，并且处理队列不该被无谓地重排
    assert payload["processing"]["blob_restored"] is False
    assert payload["processing"].get("queued") is not True


def test_reshipping_a_failed_file_restores_a_lost_blob_and_requeues(
    api, admin_user, library_setup, isolated_paths, monkeypatch
):
    """原件丢失 / 处理失败的资料：**重传同一份文件就是修复入口**。

    独立审计 F2 指出：原件丢失此前只能看见不能修（IngestedFile 的 update/destroy
    是有意的 PermissionDenied，add_to_library 只新增记录）。这里锁住真实修复路径：
      * 内容寻址落盘把丢失的原始字节**写回同一个只读路径**（blob_restored=True）；
      * 记录仍是同一条（去重命中，不产生第二条）；
      * 该资料此前是 FAILED，所以这次重传会**重新排队处理**。
    """
    from apps.knowledge import storage
    from apps.knowledge.models import ParseStatus

    library = library_setup
    body = b"lost-original-bytes"

    first = _upload(api, admin_user, library, filename="丢了.txt", content=body)
    record = IngestedFile.objects.get(pk=first.json()["file"]["id"])
    # 把它做成"原件丢失 + 处理失败"的真实状态。
    # 注意：只读副本（只读位）会让 unlink 直接报 PermissionError，所以先解除只读位
    # —— 这也正是"丢了"在真实环境里的样子：文件不在了。
    target = storage.blob_path(record.content_sha256)
    os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
    target.unlink()
    IngestedFile._base_manager.filter(pk=record.pk).update(
        parse_status=ParseStatus.FAILED, parse_error="模型服务不可用"
    )
    assert storage.verify_blob(record.content_sha256) is False

    queued: list[tuple] = []
    monkeypatch.setattr(
        "config.celery.dispatch_processing",
        lambda file_id, **kwargs: queued.append((file_id, kwargs))
        or {"dispatched": True, "mode": "queue", "task_id": "t-1"},
    )

    second = _upload(api, admin_user, library, filename="丢了.txt", content=body)
    payload = second.json()

    assert second.status_code == 202
    assert payload["created"] is False
    assert payload["reason"] == "duplicate_content"
    assert payload["stored"]["reused"] is False, "丢失的原件必须真的被重新写入"
    assert payload["processing"]["blob_restored"] is True
    assert payload["processing"]["queued"] is True
    assert queued and queued[0][0] == record.pk
    assert storage.verify_blob(record.content_sha256) is True, "原件应已补齐"
    assert IngestedFile.objects.filter(library=library).count() == 1


def test_upload_rejects_disallowed_extension(api, admin_user, library_setup, isolated_paths):
    library = library_setup
    response = _upload(
        api, admin_user, library, filename="恶意.exe", content=b"MZ\x90\x00"
    )
    assert response.status_code == 400
    assert "不支持" in response.content.decode("utf-8")


def test_upload_requires_contribute_permission(api, user, library_setup, isolated_paths):
    """没有该库写入权限的用户不能上传（403）。"""
    library = library_setup
    response = _upload(api, user, library, filename="越权.txt", content=b"nope")
    assert response.status_code in {403, 404}


# ------------------------------------------------------- 处理模式与状态机 ----


@override_settings(KB_UPLOAD_PROCESS_MODE="inline", KB_TEST_ADAPTER="offline")
def test_upload_inline_completes_and_indexes(api, admin_user, library_setup, isolated_paths):
    """inline 模式：上传即完成解析 + 向量写入（真 Qdrant，测试适配器向量）。"""
    library = library_setup
    qdrant, name, restore = _open_vector_store(library, isolated_paths)
    body = "质保期：设备验收合格之日起 12 个月。\n".encode()

    response = _upload(api, admin_user, library, filename="inline.txt", content=body)
    assert response.status_code in {200, 201}
    payload = response.json()
    assert payload["processing"]["mode"] == "inline"
    assert payload["processing"]["ok"] is True, f"{payload['processing']}"
    record = IngestedFile.objects.get(pk=payload["file"]["id"])
    assert record.parse_status == ParseStatus.DONE
    assert record.vector_status == "indexed"
    assert DocumentChunk.objects.filter(file=record).count() > 0
    # 用临时新开客户端数点：注入流水线的实例会在 restore 时关闭，断言不该依赖它
    probe = vectorindex.build_client()
    try:
        assert vectorindex.count_points(probe, name) == record.chunk_count
    finally:
        probe.close()


@override_settings(KB_UPLOAD_PROCESS_MODE="queue", KB_TEST_ADAPTER="offline")
def test_upload_queue_mode_reports_pending_not_fake_done(
    api, admin_user, library_setup, isolated_paths, monkeypatch
):
    """queue 模式 + 没有 worker：必须明确"待处理"，绝不假装完成。"""
    library = library_setup

    # 模拟"broker 不可用"（真实环境里没有 worker/broker 就是这个结果）
    import config.celery as celery_app

    monkeypatch.setattr(
        celery_app,
        "dispatch_processing",
        lambda file_id: {"dispatched": False, "mode": "queue", "task_id": "", "error": "broker 不可用"},
    )

    response = _upload(
        api, admin_user, library, filename="待处理.txt", content=b"pending-content"
    )
    assert response.status_code == 202
    payload = response.json()
    assert payload["processing"]["queued"] is True
    assert payload["processing"]["ok"] is False
    assert "待处理" in payload["processing"]["detail"]

    record = IngestedFile.objects.get(pk=payload["file"]["id"])
    # 原件必须已经安全落盘，状态是"待处理"（不是 done）
    assert record.raw_path
    assert record.parse_status == ParseStatus.PENDING
    assert record.vector_status == "pending"


@override_settings(KB_UPLOAD_PROCESS_MODE="queue", KB_TEST_ADAPTER="offline", CELERY_TASK_ALWAYS_EAGER=True)
def test_upload_queue_then_kbprocess_completes(api, admin_user, library_setup, isolated_paths):
    """queue 之后的资料，用 manage.py kbprocess 能在进程内推进到完成。"""
    from django.core.management import call_command

    library = library_setup
    qdrant, name, restore = _open_vector_store(library, isolated_paths)
    response = _upload(
        api, admin_user, library, filename="待推进.txt", content=b"quality-period-12-months"
    )
    assert response.status_code in {200, 201, 202}
    record = IngestedFile.objects.get(pk=response.json()["file"]["id"])

    out = io.StringIO()
    call_command("kbprocess", "--file", str(record.pk), stdout=out)

    record.refresh_from_db()
    assert record.parse_status == ParseStatus.DONE
    assert record.vector_status == "indexed"
    # 临时新开客户端数点（注入的实例会在 restore 时关闭）
    probe = vectorindex.build_client()
    try:
        assert vectorindex.count_points(probe, name) == record.chunk_count
    finally:
        probe.close()


@override_settings(KB_UPLOAD_PROCESS_MODE="inline", KB_TEST_ADAPTER="offline")
def test_failed_parse_marks_failed_then_retry_succeeds_after_fix(
    api, admin_user, library_setup, isolated_paths
):
    """失败 → 状态为 failed（不是 done、不卡 running）→ 修好后重试成功。"""
    library = library_setup
    qdrant, name, restore = _open_vector_store(library, isolated_paths)
    response = _upload(api, admin_user, library, filename="坏文件.xls", content=b"BIFF junk")
    payload = response.json()
    record = IngestedFile.objects.get(pk=payload["file"]["id"])

    assert record.parse_status == ParseStatus.FAILED
    assert record.parse_error
    assert record.chunk_count == 0
    assert payload["processing"]["ok"] is False

    # 重试接口：仍然是失败（这份内容不是有效工作簿），但不产生半成品
    api.force_authenticate(user=admin_user)
    retry = api.post(f"/api/v1/knowledge/files/{record.pk}/process/", {"force": "1"})
    assert retry.status_code == 409
    record.refresh_from_db()
    assert record.parse_status == ParseStatus.FAILED
    assert DocumentChunk.objects.filter(file=record).count() == 0

    # 换成可解析内容（同名不同内容 → 新记录），确认流水线本身是好的
    ok = _upload(api, admin_user, library, filename="好文件.txt", content="质保期 12 个月".encode())
    assert ok.status_code in {200, 201}
    assert IngestedFile.objects.get(pk=ok.json()["file"]["id"]).parse_status == ParseStatus.DONE


@override_settings(KB_UPLOAD_PROCESS_MODE="inline")
def test_upload_failure_never_leaves_running(api, admin_user, library_setup, isolated_paths, monkeypatch):
    """任何异常（含未预料的）都必须落到 FAILED，不能把资料永久留在 RUNNING。"""
    library = library_setup
    from apps.knowledge import pipeline

    def boom(*args, **kwargs):
        raise RuntimeError("模拟第三方库炸了")

    monkeypatch.setattr(pipeline, "write_chunks", boom)
    response = _upload(api, admin_user, library, filename="炸.txt", content=b"whatever")
    assert response.status_code in {200, 201}
    record = IngestedFile.objects.get(pk=response.json()["file"]["id"])
    assert record.parse_status == ParseStatus.FAILED
    assert "RuntimeError" in record.parse_error


# ------------------------------------------- 关键字可搜：不依赖模型服务 ----


@override_settings(KB_UPLOAD_PROCESS_MODE="inline", KB_TEST_ADAPTER="offline")
def test_keyword_searchable_after_upload(api, admin_user, library_setup, isolated_paths):
    """上传解析后，用关键词就能搜到（不依赖 AS711，也不依赖向量那一路）。"""
    library = library_setup
    body = "现场沟通记录：设备验收报告需补充安全交底记录。".encode()
    response = _upload(api, admin_user, library, filename="沟通记录.txt", content=body)
    assert response.status_code in {200, 201}

    api.force_authenticate(user=admin_user)
    search = api.post(
        "/api/v1/knowledge/search/",
        {"q": "安全交底", "libraries": [library.code], "rerank": False},
        format="json",
    )
    assert search.status_code == 200
    payload = search.json()
    assert payload["results"], payload
    assert payload["results"][0]["library_code"] == library.code
    # 至少有一路是"数据库关键词兜底"或向量侧关键字命中的
    report = payload["report"]
    assert report["db_keyword_hits"] > 0 or report["sparse_hits"] > 0


@override_settings(KB_UPLOAD_PROCESS_MODE="inline", KB_TEST_ADAPTER="offline")
def test_search_filters_by_library_dropdown(api, admin_user, library_setup, isolated_paths, make_library):
    """信息库下拉筛选：选中 A 库时不能返回 B 库的命中。"""
    library = library_setup
    qdrant, name, restore = _open_vector_store(library, isolated_paths)
    other = make_library(code="other-lib2", name="另一个库")
    other_name = ta.test_collection_name(other.pk, 1)
    # 复用同一个客户端（本地嵌入模式不能开第二个）
    vectorindex.ensure_collection(qdrant, other_name, DIM)
    VectorCollection.objects.create(
        name=other_name, dimension=DIM, embedding_model="m", library=other, is_active=True
    )
    KnowledgeLibrary.objects.filter(pk=other.pk).update(
        active_generation=1, vector_collection=other_name
    )

    _upload(api, admin_user, library, filename="甲库文档.txt", content="预付款比例 30%".encode())
    _upload(api, admin_user, other, filename="乙库文档.txt", content="预付款比例 30%".encode())

    api.force_authenticate(user=admin_user)
    response = api.post(
        "/api/v1/knowledge/search/",
        {"q": "预付款比例", "libraries": [other.code], "rerank": False},
        format="json",
    )
    assert response.status_code == 200
    codes = {hit["library_code"] for hit in response.json()["results"]}
    assert codes <= {other.code}, codes
