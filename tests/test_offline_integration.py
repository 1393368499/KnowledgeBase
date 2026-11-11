"""离线集成测试：真实数据库 + 真实 Qdrant（本地嵌入模式）+ 显式测试适配器。

口径说明（必须写清楚，否则就成了"拿 mock 当真实"）
--------------------------------------------------
本文件验证的是**整条管道真的通**：

上传原始字节 → 落盘 + 哈希 → 真实解析 → 真实写 Qdrant → 真实检索 → 权限/库过滤

- 数据库：当前测试库（默认 SQLite；``--kb-db=postgres`` 时是真 PostgreSQL）
- 向量库：**真实 qdrant-client**，本地嵌入模式（落在临时目录），不是内存假实现；
  docker 里 server 模式的验证标了 ``real_qdrant``，需要容器在跑。
- 向量：**显式离线适配器**的确定性假向量（``KB_TEST_ADAPTER=offline``）。
  所以它只能证明"管道与过滤正确"，**不能**证明语义检索质量。
  真实 AS711 的验证标了 ``real_as711``，服务恢复后再跑。

一个重要的测试写法约定
----------------------
``kb_ingested_file`` 的 ``content_sha256`` / ``size_bytes`` 是**受数据库触发器保护**的
字段（P1 硬约束），建行之后再改会被直接拒绝。所以所有"带真实字节的资料"都必须
**一步创建**：先算内容地址 → 再用正确的 sha/大小建行。见 :func:`_make_with_bytes`。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from django.test import override_settings

from apps.knowledge import pipeline, storage, vectorindex
from apps.knowledge.parsers import ParseError
from apps.knowledge import testing_adapter as ta
from apps.knowledge.embedding import active_profile
from apps.knowledge.models import (
    DocumentChunk,
    IngestedFile,
    KnowledgeLibrary,
    ParseStatus,
    VectorCollection,
)
from apps.knowledge.permissions import readable_library_ids
from apps.knowledge.vectorindex import VectorPoint, point_id_for, upsert_points

pytestmark = pytest.mark.django_db

DIM = 1024

#: 本地嵌入模式的 Qdrant 不支持 payload 索引（它会明确警告），
#: 因此只有真实 server 模式可以验收全文索引的分词行为。
#: 嵌入模式的 MatchText 仍可做基本文本匹配。
LOCAL_MODE = True


def _sparse_works() -> bool:
    return not LOCAL_MODE


# ------------------------------------------------------------------ 夹具 ----


def _make_with_bytes(
    library,
    make_file,
    body: bytes,
    *,
    name: str = "资料.md",
    collection: str = "",
    **extra,
):
    """一步创建"已经有真实原始字节"的资料（sha/大小/路径一开始就正确）。

    ``collection`` 用来把资料直接指向测试专用 collection（``__offline_test`` 后缀），
    否则流水线会回落到正式 collection 名，被适配器的安全校验拒绝——那正是它该做的事。
    """
    stored = storage.store_bytes(body)
    record = make_file(
        library,
        name=name,
        sha=stored.sha256,
        size_bytes=stored.size_bytes,
        status=extra.pop("status", ParseStatus.PENDING),
        raw_path=stored.rel_path,
        raw_size_bytes=stored.size_bytes,
        # 注意不要再传 vector_collection（make_file 已经给了），
        # 这里只补流水线真正读的那个字段。
        vector_collection_name=collection,
        **extra,
    )
    return record


@pytest.fixture
def indexed_library(library, isolated_paths, qdrant_client):
    """给一个库准备好 generation=1 的**测试专用** collection（带 __offline_test 后缀）。"""
    name = ta.test_collection_name(library.pk, 1)
    vectorindex.ensure_collection(qdrant_client, name, DIM)
    vectorindex.ensure_text_index(qdrant_client, name)
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
    # 让测试手里的实例也跟上：pipeline 读的是实例属性，过期实例会回落到旧 generation
    library.refresh_from_db()
    yield library, qdrant_client, name


# ------------------------------------------------------------------ 原始字节 ----


def test_raw_bytes_persisted_hashed_and_read_only(isolated_paths):
    """原始字节：真落盘、真哈希、只读、重放同一份内容复用同一路径。"""
    payload = "华东石化检维修项目 技术协议".encode("utf-8")
    result = storage.store_bytes(payload)

    assert result.size_bytes == len(payload)
    assert result.reused is False
    assert result.path.is_file()
    assert storage.sha256_of_file(result.path) == result.sha256
    assert result.read_only is True
    assert result.rel_path.startswith(result.sha256[:2] + "/")

    again = storage.store_bytes(payload)
    assert again.reused is True
    assert again.path == result.path
    assert len(list(result.path.parent.iterdir())) == 1  # 内容寻址：目录里只有一个文件


def test_raw_bytes_refuses_overwrite_with_different_content(isolated_paths):
    """内容地址相同但磁盘内容被改：拒绝写入，不静默覆盖。"""
    payload = b"original-bytes"
    stored = storage.store_bytes(payload)

    import os

    os.chmod(stored.path, 0o644)  # 模拟被改动（先去掉只读位）
    stored.path.write_bytes(b"tampered-bytes")

    with pytest.raises(storage.BlobCorrupt):
        storage.store_bytes(payload)


def test_read_blob_verifies_hash_on_open(isolated_paths):
    stored = storage.store_bytes(b"trusted-bytes")
    assert storage.read_blob(stored.sha256) == b"trusted-bytes"
    import os

    os.chmod(stored.path, 0o644)
    stored.path.write_bytes(b"other-bytes")
    with pytest.raises(storage.BlobCorrupt):
        storage.read_blob(stored.sha256)


def test_missing_blob_reports_not_found(isolated_paths):
    with pytest.raises(storage.BlobNotFound):
        storage.read_blob("0" * 64)


# ----------------------------------------------------------- 解析 → 分段 ----


def test_parse_and_chunk_real_files(indexed_library, make_file):
    """真实解析 txt 并写分段（离线，不依赖模型服务）。"""
    library, _qdrant, _collection = indexed_library
    record = _make_with_bytes(
        library,
        make_file,
        "2026-02-18 现场会议纪要\n结论：设备验收报告需补充安全交底记录。\n".encode(),
        name="现场记录.txt",
    )

    blocks, step = pipeline.parse_stored_file(record)
    assert step.metrics["parser"] == "text"
    assert blocks and "现场会议纪要" in blocks[0].text

    chunk_step = pipeline.write_chunks(record, blocks)
    assert chunk_step.metrics["chunks"] == len(blocks)
    assert DocumentChunk.objects.filter(file=record).count() == len(blocks)


def test_parse_real_markdown_strips_syntax(indexed_library, make_file):
    library, _qdrant, _collection = indexed_library
    record = _make_with_bytes(
        library,
        make_file,
        "# 标题\n\n- 质保期 12 个月\n\n[链接](http://x)\n".encode(),
        name="需求.md",
    )
    blocks, step = pipeline.parse_stored_file(record)
    text = " ".join(block.text for block in blocks)
    assert step.metrics["parser"] == "text"
    assert "#" not in text
    assert "质保期 12 个月" in text
    assert "链接" in text


def test_parse_real_csv_rows(indexed_library, make_file):
    library, _qdrant, _collection = indexed_library
    record = _make_with_bytes(
        library,
        make_file,
        "序号,供应商,状态\n1,万华化学,已准入\n2,浙能电力,待评估\n".encode(),
        name="清单.csv",
    )
    blocks, step = pipeline.parse_stored_file(record)
    text = " ".join(block.text for block in blocks)
    assert step.metrics["parser"] == "csv"
    assert "供应商=万华化学" in text
    assert "状态=待评估" in text


def test_parse_unsupported_format_is_explicit(indexed_library, make_file):
    """坏掉的旧格式文件明确报错，不假装解析成功、也不返回空正文。

    `.xls` 依赖**隔离的 Office 转换服务**（Docker 部署，``KB_OFFICE_SOCKET``）
    才能转换，宿主上不跑任何本地转换进程。因此这里断言的是"明确失败并带原因"：
    未配置服务时说明能力缺失，配置了服务时由服务判定文件损坏。

    2026-10-16 更新（独立审查发现"伪成功"）：`.xls` 声明的是 OLE2 复合文档，
    转换前的容器结构校验会先拦住"不是 OLE2 的垃圾字节"，不把它送进转换器。
    """
    library, _qdrant, _collection = indexed_library
    record = _make_with_bytes(library, make_file, b"PK\x03\x04 junk", name="坏工作簿.xls")
    with pytest.raises(ParseError) as excinfo:
        pipeline.parse_stored_file(record)
    message = str(excinfo.value)
    assert message, "失败必须带原因"
    # 可能的原因：未配置转换服务 / 连不上服务 / 容器结构不符或被服务判损坏
    assert (
        "KB_OFFICE_SOCKET" in message
        or "Office 转换服务" in message
        or "无法连接" in message
        or "损坏文件" in message
        or "OLE2" in message
        or "转换" in message
    ), message


def test_source_changed_is_detected(indexed_library, make_file):
    """磁盘字节大小与库里记录不一致 → 明确报错（事实来源被改动）。"""
    library, _qdrant, _collection = indexed_library
    record = _make_with_bytes(library, make_file, b"abc", name="来源校验.txt")
    # raw_size_bytes 是受控字段（可写），故意写错来触发校验
    IngestedFile._base_manager.filter(pk=record.pk).update(raw_size_bytes=999)
    record.refresh_from_db()
    with pytest.raises(pipeline.SourceChanged):
        pipeline.parse_stored_file(record)


def test_missing_raw_bytes_reports_clearly(indexed_library, make_file):
    """原始字节在磁盘上不存在时报 BlobNotFound（不返回空内容糊过去）。"""
    library, _qdrant, _collection = indexed_library
    record = _make_with_bytes(library, make_file, b"abc", name="没有字节.txt")
    # 从磁盘上把这份内容删掉，模拟"库里有记录但事实来源丢了"
    path = storage.blob_path(record.content_sha256)
    assert path.is_file()
    path.chmod(0o644)
    path.unlink()
    with pytest.raises(storage.BlobNotFound):
        pipeline.parse_stored_file(record)


# ------------------------------------------------- 完整链路：解析 → 向量 → 检索 ----


def test_full_offline_pipeline_writes_vectors_and_searches(indexed_library, make_file, monkeypatch, settings):
    """完整链路：真实字节 → 解析 → 假向量写入真 Qdrant → 检索命中。"""
    library, qdrant, collection = indexed_library
    record = _make_with_bytes(
        library,
        make_file,
        (
            "# 产品需求文档 v2\n\n"
            "质保期：设备验收合格之日起 12 个月。\n"
            "预付款比例：30%，验收后付至 95%。\n"
        ).encode(),
        name="产品需求.md",
        collection=collection,
    )
    # 让库与资料都指向这一代（模拟 prepare + 校验通过后的切换）。
    # 直接更新指针是为了聚焦流水线本身；generation 的分配/校验逻辑有专门用例覆盖。
    KnowledgeLibrary.objects.filter(pk=library.pk).update(
        active_generation=1, vector_collection=collection
    )
    IngestedFile._base_manager.filter(pk=record.pk).update(
        vector_collection_name=collection, vector_generation=1
    )
    library.refresh_from_db()
    record.refresh_from_db()

    settings.KB_EMBED_BATCH_SIZE = 1
    from apps.knowledge import progress
    reported = []
    original_report = progress.report
    def capture(stage, label, current=None, total=None, unit='', detail=''):
        if stage == 'embedding':
            # 已报告写入的数量必须与实际向量点数一致。
            assert current == vectorindex.count_points(qdrant, collection)
            reported.append((current, total))
        original_report(stage, label, current, total, unit, detail)
    monkeypatch.setattr(progress, 'report', capture)
    step = pipeline.process_file(record, client=qdrant)
    record.refresh_from_db()

    assert step.ok is True
    assert record.parse_status == ParseStatus.DONE
    assert record.vector_status == "indexed"
    assert record.embedded_chunk_count == record.chunk_count > 0
    assert reported == [(n, record.chunk_count) for n in range(record.chunk_count + 1)]
    assert record.vector_collection_name == collection
    assert vectorindex.count_points(qdrant, collection) == record.chunk_count

    points, _ = qdrant.scroll(collection_name=collection, limit=10, with_payload=True)
    payload = points[0].payload
    assert payload["permission_code"] == record.permission_code
    assert payload["is_test_adapter"] is True  # 一眼看出是假向量算出来的
    assert payload["library_code"] == library.code

    outcome = pipeline.search(
        query="质保期",
        libraries=[library],
        permission_codes=[record.permission_code],
        limit=5,
        use_rerank=False,
        client=qdrant,
    )
    report = outcome["report"]
    assert report["test_adapter"] is True
    assert report["dense_hits"] > 0
    # 向量侧关键字（MatchText）是 **whole-token** 匹配：这里的分段文本里
    # 没有独立成词的 "质保期"（实际 token 是"质保期：设备验收合格之日起"），
    # 所以它命中不了——这是分词边界，不是"模式不支持"。
    # 真正的召回由**数据库关键词兜底**保证，下面就是对它的断言。
    assert report["db_keyword_fallback"] is True
    assert report["db_keyword_hits"] > 0
    assert outcome["results"]
    assert outcome["results"][0]["library_code"] == library.code
    assert outcome["results"][0]["original_name"] == "产品需求.md"


def test_search_filters_by_permission_code(indexed_library, make_file, make_library):
    """权限过滤真的落在向量过滤上。"""
    library, qdrant, collection = indexed_library
    other = make_library(code="other-lib", name="另一个库")
    record = _make_with_bytes(library, make_file, "质保期 12 个月".encode(), name="甲.md")

    vector = ta.OfflineAdapter().embed(["质保期 12 个月"])[0]
    upsert_points(
        qdrant,
        collection,
        [
            VectorPoint(
                point_id=point_id_for(record, 1),
                vector=vector,
                payload={
                    "text": "质保期 12 个月",
                    "permission_code": record.permission_code,
                    "library_id": library.pk,
                    "library_code": library.code,
                    "ordinal": 1,
                },
            )
        ],
        dimension=DIM,
    )

    assert (
        vectorindex.search_dense(
            qdrant,
            collections=[collection],
            vector=vector,
            limit=5,
            permission_codes=[f"library:{other.pk}"],
        )
        == []
    )

    hit = vectorindex.search_dense(
        qdrant,
        collections=[collection],
        vector=vector,
        limit=5,
        permission_codes=[record.permission_code],
    )
    assert len(hit) == 1
    assert hit[0].payload["library_code"] == library.code

    assert (
        len(
            vectorindex.search_dense(
                qdrant,
                collections=[collection],
                vector=vector,
                limit=5,
                permission_codes=None,
            )
        )
        == 1
    )
    assert (
        vectorindex.search_dense(
            qdrant, collections=[collection], vector=vector, limit=5, permission_codes=[]
        )
        == []
    )


def test_sparse_search_hits_keyword(indexed_library, make_file):
    library, qdrant, collection = indexed_library
    record = _make_with_bytes(library, make_file, "预付款比例 30%".encode(), name="乙.md")
    vector = ta.OfflineAdapter().embed(["预付款比例 30%"])[0]
    upsert_points(
        qdrant,
        collection,
        [
            VectorPoint(
                point_id=point_id_for(record, 1),
                vector=vector,
                payload={
                    "text": "预付款比例 30%，验收后付至 95%",
                    "permission_code": record.permission_code,
                    "library_id": library.pk,
                    "ordinal": 1,
                },
            )
        ],
        dimension=DIM,
    )
    hits = vectorindex.search_sparse(
        qdrant, collections=[collection], text="预付款", limit=5, permission_codes=None
    )
    assert len(hits) == 1
    assert "预付款比例" in hits[0].payload["text"]
    assert vectorindex.search_sparse(
        qdrant, collections=[collection], text="预付款", limit=5,
        permission_codes=["library:unauthorized"],
    ) == []


def test_retry_is_idempotent_no_duplicate_points(indexed_library, make_file):
    """重跑处理：点 id 稳定，点数不翻倍，分段不重复，原始字节不变。"""
    library, qdrant, collection = indexed_library
    record = _make_with_bytes(
        library,
        make_file,
        "第一段内容。\n第二段内容。\n".encode(),
        name="幂等.md",
        collection=collection,
    )

    pipeline.process_file(record, client=qdrant)
    record.refresh_from_db()
    first = {
        "points": vectorindex.count_points(qdrant, collection),
        "chunks": record.chunk_count,
        "sha": record.content_sha256,
        "raw": record.raw_path,
        "attempts": record.parse_attempts,
    }

    pipeline.retry_file(record, client=qdrant)
    record.refresh_from_db()

    assert vectorindex.count_points(qdrant, collection) == first["points"]
    assert record.chunk_count == first["chunks"]
    assert record.content_sha256 == first["sha"]
    assert record.raw_path == first["raw"]
    assert record.parse_attempts == first["attempts"] + 1  # 重试是记录了次数的


def test_failed_parse_does_not_mark_done(indexed_library, make_file):
    """解析失败：状态是 failed，绝不写成 done（不留半成品当成品）。"""
    library, _qdrant, _collection = indexed_library
    record = _make_with_bytes(library, make_file, b"PK\x03\x04 junk", name="坏文件.xls")
    with pytest.raises(pipeline.PipelineError):
        pipeline.process_file(record, client=_qdrant)
    record.refresh_from_db()
    assert record.parse_status == ParseStatus.FAILED
    assert record.parse_error
    assert record.chunk_count == 0
    assert record.embedded_chunk_count == 0


def test_parse_attempts_have_an_upper_bound(indexed_library, make_file):
    """坏文件不会无限重试：尝试次数超上限后直接拒绝。"""
    library, _qdrant, _collection = indexed_library
    record = _make_with_bytes(library, make_file, b"PK\x03\x04 junk", name="再坏.xls")
    IngestedFile._base_manager.filter(pk=record.pk).update(parse_attempts=5)
    record.refresh_from_db()
    with pytest.raises(pipeline.PipelineError, match="上限"):
        pipeline.process_file(record, client=_qdrant)


# ------------------------------------------------------------ 检索降级行为 ----


def test_search_degrades_when_embedding_unavailable(indexed_library, make_file):
    """模型服务不可用时：关键字检索照旧，报告如实写"降级"。"""
    library, qdrant, collection = indexed_library
    record = _make_with_bytes(library, make_file, "质保期 12 个月".encode(), name="降级.md")
    upsert_points(
        qdrant,
        collection,
        [
            VectorPoint(
                point_id=point_id_for(record, 1),
                vector=ta.OfflineAdapter().embed(["质保期"])[0],
                payload={
                    "text": "质保期 12 个月",
                    "permission_code": record.permission_code,
                    "library_id": library.pk,
                    "ordinal": 1,
                },
            )
        ],
        dimension=DIM,
    )

    import httpx

    from apps.knowledge import embedding as emb

    def boom(request):
        raise httpx.ConnectError("连接被拒绝", request=request)

    broken = httpx.Client(transport=httpx.MockTransport(boom))

    with override_settings(KB_TEST_ADAPTER="", KB_EMBED_RETRIES=0):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                pipeline,
                "embed_texts",
                lambda texts, **kwargs: emb.embed_texts(
                    texts, client=broken, max_attempts=1, **kwargs
                ),
            )
            outcome = pipeline.search(
                query="质保期",
                libraries=[library],
                permission_codes=[record.permission_code],
                limit=5,
                use_rerank=False,
                client=qdrant,
            )

    report = outcome["report"]
    assert report["degraded"] is True
    assert report["degraded_reason"]
    assert report["service_unavailable"]["service"].startswith("embed:")
    if _sparse_works():
        assert report["sparse_hits"] > 0  # 降级不等于"没有结果"
        assert outcome["results"]
    else:
        # 本地模式：两路都拿不到结果，但报告必须**说清是两件事**：
        # 语义那路是"服务不可用"，关键字那路是"本地模式不支持"
        assert report["local_mode_sparse_unsupported"] is True
        assert report["dense_hits"] == 0


# ------------------------------------------------- 可选：真实 Qdrant server ----


@pytest.mark.real_qdrant
def test_real_qdrant_server_roundtrip():
    """真实 Qdrant server（docker）上的 collection 建/写/查/删全流程。"""
    name = "kb-pytest-server-roundtrip"
    qdrant = vectorindex.build_client()
    try:
        vectorindex.ensure_collection(qdrant, name, 8)
        assert vectorindex.collection_exists(qdrant, name)
        upsert_points(
            qdrant,
            name,
            [
                VectorPoint(
                    point_id="00000000-0000-0000-0000-000000000001",
                    vector=[0.1] * 8,
                    payload={"text": "真实 server 往返", "permission_code": "library:1"},
                )
            ],
            dimension=8,
        )
        assert vectorindex.count_points(qdrant, name) == 1
        hits = vectorindex.search_dense(qdrant, collections=[name], vector=[0.1] * 8, limit=1)
        assert hits and hits[0].payload["text"] == "真实 server 往返"
    finally:
        try:
            qdrant.delete_collection(name)
        except Exception:  # noqa: BLE001
            pass
        qdrant.close()


# ---------------------------------------------- 可选：真实 AS711 端到端 ----


@pytest.mark.real_as711
def test_real_as711_embedding_dimension_matches_profile():
    """真实 AS711 嵌入：实测维度必须等于 profile 声明维度（服务恢复后跑）。"""
    from apps.knowledge.embedding import probe_dimension

    report = probe_dimension(profile=active_profile())
    assert report["ready"] is True, report["error"]
    assert report["actual_dimension"] == active_profile().dimension


# ------------------------------------------------------------- 其它基础 ----


def test_readable_library_ids_for_staff_is_unrestricted(admin_user):
    assert readable_library_ids(admin_user) is None


def test_isolated_paths_are_inside_tmp(isolated_paths, tmp_path):
    """测试自己也要自证：临时目录确实被隔离了，不碰项目里的真实 vector_data。"""
    assert isolated_paths["vector"].is_relative_to(tmp_path)
    assert isolated_paths["raw"].is_relative_to(tmp_path)
    stored = storage.store_bytes(b"isolation-check")
    assert not Path(stored.rel_path).is_absolute()
    assert (isolated_paths["raw"] / stored.rel_path).is_file()
