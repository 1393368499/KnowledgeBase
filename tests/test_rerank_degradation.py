"""重排失败**一律**标记为"降级"并带上原因（真实缺陷回归）。

独立复现证据：``artifacts/acceptance/independent-rerank-degradation-20261016.json``
—— 合成一个"HTTP 200 但缺 ``results``"的畸形响应后，``rerank.applied=false``、
``rerank.error`` 有值，但 ``degraded=false``、``degraded_reason=""``，
于是搜索页（``apps/webui/static/webui/kb-search.js`` 读 ``report.degraded``）
**一条降级提示都不显示**：用户拿到的其实是 RRF 融合排序结果，却看起来
像"重排按相关性排过"的结果。

口径（为什么必须所有异常都算降级）：
    降级 = 本次排序退化为 RRF 融合排序，这与"重排生效"是两回事。
    所以任何重排异常 → ``degraded=True`` + ``degraded_reason`` 有原因；
    已回退的融合结果必须照常返回（不是抛异常、也不是空结果）。
    ``rerank.unavailable`` 的语义不变：仍只在服务不可用时为 True。

写法参考：``tests/test_model_clients.py``（用 ``httpx.MockTransport`` 构造
``httpx.Client(transport=..., trust_env=False)`` 再注进客户端）与
``tests/test_offline_integration.py``（``indexed_library`` 夹具 + 
``pipeline.search(..., client=qdrant, use_rerank=..., rerank_client=...)``）。
整个用例离线：向量是显式离线适配器的确定性假向量，重排走 MockTransport，
不碰真实 AS711、不写生产库。
"""

from __future__ import annotations

import httpx
import pytest
from django.test import override_settings

from apps.knowledge import pipeline, vectorindex
from apps.knowledge import testing_adapter as ta
from apps.knowledge.embedding import active_profile
from apps.knowledge.models import KnowledgeLibrary, VectorCollection
from apps.knowledge.rerank import RerankClient, RerankProfile
from apps.knowledge.resilience import BREAKERS
from apps.knowledge.vectorindex import VectorPoint, point_id_for, upsert_points

pytestmark = pytest.mark.django_db

DIM = 1024
QUERY = "质保期"


@pytest.fixture(autouse=True)
def _clean_breakers():
    """熔断器是**进程内全局**状态：不清会跨用例互相影响。"""
    BREAKERS.clear()
    yield
    BREAKERS.clear()


@pytest.fixture
def indexed_library(library, isolated_paths, qdrant_client):
    """与 ``tests/test_offline_integration.py`` 同款：generation=1 的测试专用 collection。

    夹具自带隔离路径（``isolated_paths`` / ``qdrant_client``），用完即回收，
    不需要用例自己清理。
    """
    name = ta.test_collection_name(library.pk, 1)
    vectorindex.ensure_collection(qdrant_client, name, DIM)
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
    # 让测试手里的实例也跟上：pipeline 读的是实例属性
    library.refresh_from_db()
    yield library, qdrant_client, name


def _seed_one_point(library, qdrant, collection, make_file):
    """写一条**可核验**的向量点，保证融合那一步一定有 1 条候选。

    两个细节都是有原因的：
    * ``file_id`` / ``content_sha256`` 必须与库里一致 —— 否则
      ``pipeline._drop_tombstoned`` 会 fail-closed 把它丢掉，结果为空；
    * 向量直接用**查询文本本身**的确定性假向量 → 稠密那一路必定命中，
      不依赖本地模式的关键字（MatchText）能力。
    """
    record = make_file(library, name="重排降级.md")
    upsert_points(
        qdrant,
        collection,
        [
            VectorPoint(
                point_id=point_id_for(record, 1),
                vector=ta.OfflineAdapter(dimension=DIM).embed([QUERY])[0],
                payload={
                    "text": "质保期 12 个月",
                    "permission_code": record.permission_code,
                    "library_id": library.pk,
                    "library_code": library.code,
                    "file_id": record.pk,
                    "content_sha256": record.content_sha256,
                    "ordinal": 1,
                },
            )
        ],
        dimension=DIM,
    )
    return record


def _rerank_profile() -> RerankProfile:
    return RerankProfile(base_url="http://as711.test:8002/v1", model="bge-reranker-large")


def _mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


def _search(library, qdrant, record, rerank_client, **overrides):
    with override_settings(**overrides):
        return pipeline.search(
            query=QUERY,
            libraries=[library],
            permission_codes=[record.permission_code],
            limit=5,
            use_rerank=True,
            rerank_client=rerank_client,
            client=qdrant,
        )


# ------------------------------------------------- 畸形响应（本次缺陷的复现） ----


def test_malformed_rerank_response_is_degraded_and_keeps_results(
    indexed_library, make_file
):
    """HTTP 200 但缺 ``results``：必须 degraded=True 且保留回退的融合结果。"""
    library, qdrant, collection = indexed_library
    record = _seed_one_point(library, qdrant, collection, make_file)

    def handler(request: httpx.Request) -> httpx.Response:
        # 200 + 合法 JSON，但结构不是重排协议要求的 {"results": [...]}
        return httpx.Response(200, json={"unexpected": "schema"})

    client = _mock_client(handler)
    try:
        outcome = _search(
            library, qdrant, record, RerankClient(_rerank_profile(), client=client)
        )
    finally:
        client.close()

    report = outcome["report"]
    assert report["degraded"] is True, "重排失败没被标成降级（本次缺陷）"
    assert "重排" in report["degraded_reason"]
    assert report["rerank"]["applied"] is False
    assert report["rerank"]["error"], "失败原因必须留在 rerank.error 里"
    # 结构畸形不是"服务不可用"：unavailable 的语义不能被放宽
    assert report["rerank"]["unavailable"] is False
    # **结果必须保留**：降级不等于"没有结果"
    assert outcome["results"], "重排失败后已回退的融合结果被丢掉了"


# ------------------------------------------------------------------ 服务不可用 ----


def test_rerank_transport_failure_is_degraded_and_unavailable(
    indexed_library, make_file
):
    """transport 抛 ``httpx.ConnectError``：degraded + unavailable 都要为真。"""
    library, qdrant, collection = indexed_library
    record = _seed_one_point(library, qdrant, collection, make_file)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("连接被拒绝", request=request)

    client = _mock_client(handler)
    try:
        outcome = _search(
            library,
            qdrant,
            record,
            RerankClient(_rerank_profile(), client=client),
            # 关掉退避重试：本用例验的是"不可用如何被记录"，不是重试策略，
            # 不让它白等一次 0.5s 退避（重试本身在 test_model_clients.py 有覆盖）。
            KB_RERANK_RETRIES=0,
        )
    finally:
        client.close()

    report = outcome["report"]
    assert report["degraded"] is True
    assert "不可用" in report["degraded_reason"]
    assert report["rerank"]["unavailable"] is True
    assert report["rerank"]["applied"] is False
    assert report["rerank"]["error"]
    assert outcome["results"]


# -------------------------------------------------------------------- 正常路径 ----


def test_healthy_rerank_is_not_degraded(indexed_library, make_file):
    """正常响应：不能因为"改成了全都标降级"而误报。"""
    library, qdrant, collection = indexed_library
    record = _seed_one_point(library, qdrant, collection, make_file)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"results": [{"index": 0, "relevance_score": 0.93}]}
        )

    client = _mock_client(handler)
    try:
        outcome = _search(
            library, qdrant, record, RerankClient(_rerank_profile(), client=client)
        )
    finally:
        client.close()

    report = outcome["report"]
    assert report["rerank"]["applied"] is True
    assert report["degraded"] is False
    assert report["degraded_reason"] == ""
    assert outcome["results"]
