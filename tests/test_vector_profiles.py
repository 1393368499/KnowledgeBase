"""嵌入 profile / 维度约束 / generation 切换 的测试。

这些用例锁死用户的硬约束：
- 现在 active profile 必须是 1024（``bge1024``），2048 只预配置、不激活；
- 不许"补零/截断/谎称 2048"——维度不匹配必须显式拒绝；
- 换模型必须新建 collection（generation +1）、全量重嵌入、原子切换，旧索引可回退。

Qdrant 用 ``FakeQdrant`` 替身（本文件里唯一的替身），它只保证"真的有 points API"
这一件事；被测的是 *我们的* generation / 维度 / 原子切换逻辑，不是 Qdrant 自己。
"""

from __future__ import annotations

import httpx
import pytest
from django.db import transaction

from apps.knowledge import embedding as emb
from apps.knowledge import pipeline, vectorindex
from apps.knowledge.embedding import DimensionMismatch, EmbeddingError
from apps.knowledge.resilience import CircuitBreaker, ServiceUnavailable
# 注意：vectorindex 有自己的一套异常体系，它的 DimensionMismatch 与 embedding 的
# **不是同一个类**。写索引层的断言必须用 vectorindex 这个。
from apps.knowledge.vectorindex import DimensionMismatch as VectorDimensionMismatch
from apps.knowledge.models import (
    DocumentChunk,
    IngestedFile,
    KnowledgeLibrary,
    VectorCollection,
)
from apps.knowledge.views import VectorCollectionViewSet
from governance.immutable import immutable_endpoint

pytestmark = pytest.mark.django_db


# ----------------------------------------------------------------- 替身 ----


def _fake_http(vec_len: int, *, captured: dict | None = None, status_code: int = 200):
    """构造一个返回固定维度向量的 httpx.Client 替身（不联网）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        payload = _json.loads(request.content.decode())
        if captured is not None:
            captured["payload"] = payload
            captured["url"] = str(request.url)
        count = len(payload["input"])
        return httpx.Response(
            status_code,
            json={"data": [{"embedding": [0.1] * vec_len} for _ in range(count)]},
            request=request,
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


class _FakeCollectionInfo:
    """模仿 qdrant_client 的 ``info.config.params.vectors.size`` 形状。

    ``_vector_size`` 读的就是这条路径；这里的形状必须跟真实客户端一致，
    否则它会返回 ``None``，把"维度检查"悄悄降级成"永远通过"。
    """

    def __init__(self, size: int) -> None:
        self.config = type("C", (), {"params": type("P", (), {"vectors": _FakeVectors(size)})()})()


class _FakeVectors:
    def __init__(self, size: int) -> None:
        self.size = size


class FakeQdrant:
    """内存版 Qdrant：只实现我们真正调用的那几个方法。"""

    def __init__(self, dim: int = 1024) -> None:
        self.dim = dim
        # **每个实例独立存储**：测试之间必须互相隔离。共享类级存储会让
        # "上一个用例的 collection"泄漏到下一个用例，把假失败和假通过同时引进来。
        # 同一个用例内如果需要"prepare 用 A、activate 用 B"，就把同一个实例传下去。
        self.collections: dict[str, int] = {}
        self.points: dict[str, dict[str, dict]] = {}
        self.text_indexed: set[str] = set()
        self.closed = False
        self.deleted: list[str] = []

    # -- collection 管理 --
    def collection_exists(self, name: str) -> bool:
        return name in self.collections

    def get_collection(self, name: str):
        return _FakeCollectionInfo(self.collections[name])

    def create_collection(self, collection_name: str, vectors_config) -> None:
        self.collections[collection_name] = int(vectors_config.size)
        self.points.setdefault(collection_name, {})

    def delete_collection(self, name: str) -> None:
        self.collections.pop(name, None)
        self.points.pop(name, None)
        self.deleted.append(name)

    def create_payload_index(self, *, collection_name: str, field_name: str, field_schema) -> None:
        self.text_indexed.add(f"{collection_name}.{field_name}")

    # -- 点操作 --
    def upsert(self, *, collection_name: str, points, wait: bool = True) -> None:
        bucket = self.points.setdefault(collection_name, {})
        for point in points:
            # 真的把 payload 和向量都存下来：激活校验要按点核对覆盖率，
            # 只存 id 的假实现会让"缺段"这种事测不出来。
            # 两种入参都要认：vectorindex.VectorPoint（字段 point_id）
            # 和 qdrant_client 的 PointStruct（字段 id）——生产路径走的是后者。
            point_id = getattr(point, "point_id", None) or getattr(point, "id", None)
            bucket[str(point_id)] = {
                "vector": list(point.vector),
                "payload": dict(getattr(point, "payload", None) or {}),
            }

    def retrieve(self, *, collection_name: str, ids, with_payload=True, with_vectors=False):
        """模仿 qdrant-client 的 ``retrieve``：按 id 取点。"""
        bucket = self.points.get(collection_name, {})
        out = []
        for point_id in ids:
            item = bucket.get(str(point_id))
            if item is None:
                continue
            out.append(
                _FakePoint(
                    id=str(point_id),
                    payload=item["payload"] if with_payload else None,
                    vector=item["vector"] if with_vectors else None,
                )
            )
        return out

    def count(self, *, collection_name: str, exact: bool = True):
        return type("R", (), {"count": len(self.points.get(collection_name, {}))})()

    def delete(self, *, collection_name: str, points_selector, wait: bool = True) -> None:
        bucket = self.points.get(collection_name, {})
        for point_id in getattr(points_selector, "points", []) or []:
            bucket.pop(str(point_id), None)

    def close(self) -> None:
        self.closed = True


class _FakePoint:
    def __init__(self, *, id, payload, vector=None) -> None:
        self.id = id
        self.payload = payload
        self.vector = vector


@pytest.fixture
def profile1024():
    return emb.get_profile("bge1024")


def _complete_generation(library, plan, *, files=(), chunks=None, qdrant):
    """把一份 generation 变成"真的重建完成"，然后才允许激活。

    生产口径下激活**不可绕过**：必须看到目标 collection 里**真的有点**，
    并逐段核对 ``file_id`` / ``ordinal`` / ``text``。所以测试也要构造真正完整的
    generation——直接往目标 collection 写点，而不是只改元数据（那正是被否掉的做法）。
    """
    for record in files:
        record_chunks = chunks if chunks is not None else list(record.chunks.order_by("ordinal"))
        for chunk in record_chunks:
            qdrant.upsert(
                collection_name=plan.collection,
                points=[
                    vectorindex.VectorPoint(
                        point_id=vectorindex.point_id_for(record, chunk.ordinal),
                        vector=[0.0] * 4,
                        payload={
                            "file_id": record.pk,
                            "ordinal": chunk.ordinal,
                            "text": chunk.text,
                            "library_id": library.pk,
                            "content_sha256": record.content_sha256,
                            # 激活校验会核对模型/版本：防止旧模型的点混进新代次
                            "model": VectorCollection.objects.get(
                                name=plan.collection
                            ).embedding_model,
                            "model_version": VectorCollection.objects.get(
                                name=plan.collection
                            ).model_version,
                        },
                    )
                ],
                wait=True,
            )
    return plan


def _make_chunks(library, make_file, count: int = 3) -> IngestedFile:
    """造一份带分段的资料（序号从 1 开始，与生产写入一致）。"""
    record = make_file(library, name="切换用例.md")
    for ordinal in range(1, count + 1):
        DocumentChunk.objects.create(
            file=record, ordinal=ordinal, text=f"第{ordinal}段内容", token_estimate=4
        )
    return record


def test_active_profile_is_1024_by_default():
    """用户明确要求：先兼容当前 1024 模型，active 不能自动变成 2048。"""
    assert emb.active_profile_name() == "bge1024"
    active = emb.active_profile()
    assert active.dimension == 1024
    assert active.model == "bge-large-zh-v1.5"


def test_2048_profile_is_preconfigured_but_not_ready():
    """目标 2048 必须预配置好，但未部署前不得就绪、不得激活。"""
    target = emb.get_profile("bge2048")
    assert target.dimension == 2048
    assert target.name != emb.active_profile_name()

    report = emb.probe_dimension(profile=target)
    assert report["ready"] is False
    # 未部署（没有 base_url/model）时必须给出原因，而不是假装可用
    assert report["error"]


def test_active_profile_is_configurable(monkeypatch):
    """维度/模型名/端点都要可配置，不能写死在代码里。"""
    monkeypatch.setenv("KB_EMBED_ACTIVE_PROFILE", "bge2048")
    monkeypatch.setenv(
        "KB_EMBED_PROFILES",
        '{"bge2048": {"model": "future-embed", "base_url": "http://10.0.0.9:8000/v1"}}',
    )
    emb.profiles.cache_clear()
    try:
        active = emb.active_profile()
        assert active.name == "bge2048"
        assert active.dimension == 2048
        assert active.model == "future-embed"
        assert active.base_url == "http://10.0.0.9:8000/v1"
    finally:
        emb.profiles.cache_clear()


def test_unknown_profile_field_is_rejected(monkeypatch):
    monkeypatch.setenv("KB_EMBED_PROFILES", '{"bge2048": {"dimensionn": 2048}}')
    emb.profiles.cache_clear()
    try:
        with pytest.raises(EmbeddingError, match="未知字段"):
            emb.profiles()
    finally:
        emb.profiles.cache_clear()


# ------------------------------------------------------------- 维度不匹配拒绝 ----


def test_embed_rejects_wrong_dimension_without_padding(profile1024):
    """模型返回 1024 维而 profile 声明 2048：必须拒绝，不补零、不截断。"""
    fake2048 = emb.ProfileInfo(
        name="bge2048",
        model="future-embed",
        model_version="target",
        base_url="http://10.0.0.9:8000/v1",
        dimension=2048,
    )
    client = _fake_http(1024)
    with pytest.raises(DimensionMismatch) as excinfo:
        emb.embed_texts(["测试"], profile=fake2048, client=client)
    message = str(excinfo.value)
    assert "2048" in message and "1024" in message
    assert "补零" in message


def test_embed_rejects_larger_dimension_too(profile1024):
    """反向也要拒绝：声明 1024、实际 2048，同样不能悄悄截断成 1024。"""
    client = _fake_http(2048)
    with pytest.raises(DimensionMismatch):
        emb.embed_texts(["测试"], profile=profile1024, client=client)


def test_embed_accepts_matching_dimension(profile1024):
    captured: dict = {}
    client = _fake_http(1024, captured=captured)
    vectors = emb.embed_texts(["甲", "乙"], profile=profile1024, client=client)
    assert len(vectors) == 2
    assert all(len(row) == 1024 for row in vectors)
    assert captured["payload"]["model"] == "bge-large-zh-v1.5"
    assert captured["url"].endswith("/embeddings")


def test_embed_error_when_service_unreachable(profile1024):
    """服务不可达：抛"服务不可用"（带尝试次数），绝不返回空向量凑数。"""
    calls = {"n": 0}

    def boom(request):
        calls["n"] += 1
        raise httpx.ConnectError("连接被拒绝", request=request)

    client = httpx.Client(transport=httpx.MockTransport(boom))
    breaker = CircuitBreaker("test-embed-unreachable", failure_threshold=99)
    with pytest.raises(ServiceUnavailable) as excinfo:
        emb.embed_texts(
            ["测试"], profile=profile1024, client=client, max_attempts=1, breaker=breaker
        )
    assert calls["n"] == 1  # max_attempts=1 就只打一次
    assert "不可用" in str(excinfo.value)


def test_embed_retries_then_gives_up_with_bounded_attempts(profile1024):
    """可重试失败按上限重试，重试用尽后明确报错（并累计熔断计数）。"""
    calls = {"n": 0}

    def boom(request):
        calls["n"] += 1
        raise httpx.ConnectError("连接被拒绝", request=request)

    client = httpx.Client(transport=httpx.MockTransport(boom))
    breaker = CircuitBreaker("test-embed-retry", failure_threshold=99)
    with pytest.raises(ServiceUnavailable):
        emb.embed_texts(
            ["测试"], profile=profile1024, client=client, max_attempts=3, breaker=breaker
        )
    assert calls["n"] == 3
    assert breaker.snapshot()["consecutive_failures"] == 3


def test_breaker_opens_and_fails_fast(profile1024):
    """连续失败到阈值即熔断：后续调用直接快速失败，不再打网络。"""
    calls = {"n": 0}

    def boom(request):
        calls["n"] += 1
        raise httpx.ConnectError("连接被拒绝", request=request)

    client = httpx.Client(transport=httpx.MockTransport(boom))
    breaker = CircuitBreaker("test-breaker-open", failure_threshold=2, cooldown_seconds=999)
    for _ in range(2):
        with pytest.raises(ServiceUnavailable):
            emb.embed_texts(
                ["x"], profile=profile1024, client=client, max_attempts=1, breaker=breaker
            )
    assert breaker.state == "open"
    before = calls["n"]
    with pytest.raises(ServiceUnavailable) as excinfo:
        emb.embed_texts(
            ["x"], profile=profile1024, client=client, max_attempts=1, breaker=breaker
        )
    assert calls["n"] == before  # 一次网络请求都没发出去
    assert excinfo.value.retry_after_seconds is not None


def test_breaker_recovers_after_success(profile1024):
    breaker = CircuitBreaker("test-breaker-recover", failure_threshold=2, cooldown_seconds=0)
    breaker.record_failure("boom")
    breaker.record_failure("boom")
    assert breaker.state == "half_open"  # cooldown=0 立刻半开
    breaker.record_success()
    assert breaker.state == "closed"
    assert breaker.snapshot()["consecutive_failures"] == 0


def test_probe_reports_not_ready_on_dimension_mismatch():
    """探测就是"就绪"的唯一来源：实测维度不符即未就绪。"""
    profile = emb.ProfileInfo(
        name="candidate",
        model="future-embed",
        model_version="v2",
        base_url="http://10.0.0.9:8000/v1",
        dimension=2048,
    )
    report = emb.probe_dimension(profile=profile, client=_fake_http(1024))
    assert report["ready"] is False
    assert report["actual_dimension"] == 1024
    assert "未就绪" in report["error"]


# ----------------------------------------------------- collection 命名与隔离 ----


def test_collection_name_carries_library_and_generation():
    """名字必须能看出"哪个库、第几代"——这是回退与排查的唯一线索。"""
    name = vectorindex.collection_name(7, 3)
    assert name == f"{vectorindex.collection_prefix()}-7-g3"


def test_logical_collection_name_follows_active_generation(library):
    assert vectorindex.logical_collection_name(library) == vectorindex.collection_name(
        library.pk, library.active_generation
    )
    library.active_generation = 5
    assert vectorindex.logical_collection_name(library).endswith("-g5")


def test_collections_are_isolated_between_libraries():
    assert vectorindex.collection_name(1, 1) != vectorindex.collection_name(2, 1)


# ------------------------------------------------------- generation 准备与切换 ----


def test_prepare_generation_creates_collection_and_registers_it(library):
    """新建 generation 只"准备"，不切线上指针。"""
    qdrant = FakeQdrant()
    plan = vectorindex.prepare_generation(library, profile=emb.get_profile("bge1024"), client=qdrant)

    assert plan.generation == 1
    assert plan.collection == vectorindex.collection_name(library.pk, 1)
    assert plan.created is True
    assert qdrant.collections[plan.collection] == 1024
    assert f"{plan.collection}.text" in qdrant.text_indexed  # 全文索引建了

    library.refresh_from_db()
    assert library.active_generation == 0  # 线上指针没动
    row = VectorCollection.objects.get(name=plan.collection)
    assert row.dimension == 1024
    assert row.embedding_model == "bge-large-zh-v1.5"
    assert row.profile == "bge1024"
    assert row.is_active is False


def test_prepare_generation_allocates_a_new_number_every_time(library):
    """连续两次 prepare 必须拿到**不同**编号（单调只增，不复用空壳）。

    旧断言"第二次应该复用同一个 generation"与需求冲突，已按新口径改写：
    我们没有 build_id / lease，无法判断一个未激活的空 collection 是否正被别的 worker
    使用，所以每次 prepare 都申请新编号。编号一旦分配就不再回收。
    """
    qdrant = FakeQdrant()
    first = vectorindex.prepare_generation(
        library, profile=emb.get_profile("bge1024"), client=qdrant
    )
    second = vectorindex.prepare_generation(
        library, profile=emb.get_profile("bge1024"), client=qdrant
    )
    assert second.generation == first.generation + 1
    assert second.collection != first.collection
    assert VectorCollection.objects.filter(library=library).count() == 2
    library.refresh_from_db()
    assert library.next_generation == second.generation  # 计数器被推到最新


def test_failed_generation_number_is_not_reused(library):
    """构建失败/取消后的编号不复用：下次 prepare 继续往后发号。"""
    qdrant = FakeQdrant()
    first = vectorindex.prepare_generation(
        library, profile=emb.get_profile("bge1024"), client=qdrant
    )
    # 模拟"这一代构建失败、被丢弃"（把登记行删掉，但计数器已经推进过）
    VectorCollection.objects.filter(library=library, name=first.collection).delete()
    qdrant.collections.pop(first.collection, None)

    second = vectorindex.prepare_generation(
        library, profile=emb.get_profile("bge1024"), client=qdrant
    )
    assert second.generation > first.generation
    assert second.collection != first.collection


def test_prepare_after_rollback_does_not_reuse_history(library, make_file):
    """回退之后再 prepare：必须拿到**比历史最大编号更大**的新号。

    这是审查意见 5 里最危险的一种：回退到 g1 后，如果按 active_generation+1 分配，
    会又拿到 g2——而 g2 是已经建好、装着旧数据的索引，新构建会写进去。
    """
    record = _make_chunks(library, make_file, count=1)
    qdrant = FakeQdrant()
    g1 = vectorindex.prepare_generation(
        library, profile=emb.get_profile("bge1024"), client=qdrant
    )
    _complete_generation(library, g1, files=[record], qdrant=qdrant)
    vectorindex.activate_generation(library, g1, client=qdrant)

    g2 = vectorindex.prepare_generation(
        library, profile=emb.get_profile("bge1024"), client=qdrant
    )
    _complete_generation(library, g2, files=[record], qdrant=qdrant)
    vectorindex.activate_generation(library, g2, client=qdrant)

    vectorindex.rollback_generation(library, to_generation=g1.generation, client=qdrant)
    library.refresh_from_db()
    assert library.active_generation == g1.generation

    g3 = vectorindex.prepare_generation(
        library, profile=emb.get_profile("bge1024"), client=qdrant
    )
    assert g3.generation == g2.generation + 1
    assert g3.collection not in {g1.collection, g2.collection}


def test_prepare_generation_refuses_dimension_change_on_same_name(library):
    """同名 collection 已存在且维度不同：必须拒绝，不能原地改维度。

    这里直接打 ``ensure_collection`` 这道闸门——``prepare_generation`` 复用旧名
    （generation 没变）时会走到它。用 ``QuerySet.update`` 拨回指针是为了绕开
    select_for_update 的读一致性，不是在生产路径上这么干。
    """
    qdrant = FakeQdrant()
    first = vectorindex.prepare_generation(
        library, profile=emb.get_profile("bge1024"), client=qdrant
    )
    # 人为把已存在的 collection 改成 2048 维，然后以同名再建一次
    qdrant.collections[first.collection] = 2048
    with pytest.raises(VectorDimensionMismatch):
        vectorindex.ensure_collection(
            qdrant, first.collection, dimension=int(first.dimension)
        )
    # prepare_generation 必须是走这道闸门，而不是自己另开一条路
    import inspect

    assert "ensure_collection(" in inspect.getsource(vectorindex.prepare_generation)


def test_activate_generation_switches_library_and_files_atomically(library, make_file):
    record = _make_chunks(library, make_file, count=2)
    qdrant = FakeQdrant()
    plan = vectorindex.prepare_generation(
        library, profile=emb.get_profile("bge1024"), client=qdrant
    )
    _complete_generation(library, plan, files=[record], qdrant=qdrant)  # 构造真正完成的重建
    vectorindex.activate_generation(library, plan, client=qdrant)

    library.refresh_from_db()
    record.refresh_from_db()
    assert library.active_generation == plan.generation
    assert library.vector_collection == plan.collection
    assert record.vector_collection_name == plan.collection
    assert record.vector_generation == plan.generation

    actives = VectorCollection.objects.filter(library=library, is_active=True)
    assert actives.count() == 1
    assert actives.first().name == plan.collection


def test_activate_generation_refuses_empty_index(library, make_file):
    """空索引不准上线：目标 collection 里没有对应的点，激活必须被拒绝。"""
    _make_chunks(library, make_file, count=1)
    qdrant = FakeQdrant()
    plan = vectorindex.prepare_generation(
        library, profile=emb.get_profile("bge1024"), client=qdrant
    )
    with pytest.raises(vectorindex.IncompleteIndex, match="覆盖不完整"):
        vectorindex.activate_generation(library, plan, client=qdrant)
    library.refresh_from_db()
    assert library.active_generation == 0  # 指针没动
    assert VectorCollection.objects.filter(library=library, is_active=True).count() == 0


def test_activate_generation_refuses_incomplete_coverage(library, make_file):
    """写了一半的索引也不准上线：真的缺点（不是改元数据）必须被拒绝。"""
    record = _make_chunks(library, make_file, count=3)
    qdrant = FakeQdrant()
    plan = vectorindex.prepare_generation(
        library, profile=emb.get_profile("bge1024"), client=qdrant
    )
    # 只写入 3 段中的 1 段：这才是真实的"半成品"
    _complete_generation(
        library,
        plan,
        files=[record],
        chunks=list(record.chunks.order_by("ordinal"))[:1],
        qdrant=qdrant,
    )
    with pytest.raises(vectorindex.IncompleteIndex, match="缺"):
        vectorindex.activate_generation(library, plan, client=qdrant)


def test_activate_generation_refuses_mismatched_text(library, make_file):
    """点存在但正文对不上（索引内容过期）：也必须拒绝。"""
    record = _make_chunks(library, make_file, count=1)
    qdrant = FakeQdrant()
    plan = vectorindex.prepare_generation(
        library, profile=emb.get_profile("bge1024"), client=qdrant
    )
    _complete_generation(library, plan, files=[record], qdrant=qdrant)
    bucket = qdrant.points[plan.collection]
    key = next(iter(bucket))
    bucket[key]["payload"]["text"] = "这是一段过期的正文"
    with pytest.raises(vectorindex.IncompleteIndex, match="正文不符"):
        vectorindex.activate_generation(library, plan, client=qdrant)


def test_activate_generation_refuses_missing_collection(library, make_file):
    """Qdrant 里 collection 不存在（例如被人手工删了）时不准激活。"""
    record = _make_chunks(library, make_file, count=1)
    qdrant = FakeQdrant()
    plan = vectorindex.prepare_generation(
        library, profile=emb.get_profile("bge1024"), client=qdrant
    )
    _complete_generation(library, plan, files=[record], qdrant=qdrant)
    qdrant.collections.pop(plan.collection)  # 模拟索引真的没了
    with pytest.raises(vectorindex.CollectionNotFound):
        vectorindex.activate_generation(library, plan, client=qdrant)


def test_activate_generation_refuses_plan_from_other_library(library, make_file, make_library):
    """plan 属于别的库时不准用在这个库上。"""
    other = make_library(code="other-gen", name="别的库")
    record = _make_chunks(library, make_file, count=1)
    qdrant = FakeQdrant()
    plan = vectorindex.prepare_generation(
        other, profile=emb.get_profile("bge1024"), client=qdrant
    )
    _complete_generation(other, plan, files=[], qdrant=qdrant)
    with pytest.raises(vectorindex.VectorIndexError, match="属于库"):
        vectorindex.activate_generation(library, plan, client=qdrant)


def test_activate_generation_leaves_only_one_active(library, make_file):
    record = _make_chunks(library, make_file, count=1)
    qdrant = FakeQdrant()
    first = vectorindex.prepare_generation(
        library, profile=emb.get_profile("bge1024"), client=qdrant
    )
    _complete_generation(library, first, files=[record], qdrant=qdrant)
    vectorindex.activate_generation(library, first, client=qdrant)
    library.refresh_from_db()

    second = vectorindex.prepare_generation(
        library, profile=emb.get_profile("bge1024"), client=qdrant
    )
    _complete_generation(library, second, files=[record], qdrant=qdrant)
    vectorindex.activate_generation(library, second, client=qdrant)

    assert list(
        VectorCollection.objects.filter(library=library, is_active=True).values_list(
            "name", flat=True
        )
    ) == [second.collection]
    # 旧 generation 的登记行留着（回退要用）
    assert VectorCollection.objects.filter(library=library, name=first.collection).exists()


def test_rollback_switches_back_without_reembedding(library, make_file):
    record = _make_chunks(library, make_file, count=2)
    qdrant = FakeQdrant()
    first = vectorindex.prepare_generation(
        library, profile=emb.get_profile("bge1024"), client=qdrant
    )
    _complete_generation(library, first, files=[record], qdrant=qdrant)
    vectorindex.activate_generation(library, first, client=qdrant)
    library.refresh_from_db()

    second = vectorindex.prepare_generation(
        library, profile=emb.get_profile("bge1024"), client=qdrant
    )
    _complete_generation(library, second, files=[record], qdrant=qdrant)
    vectorindex.activate_generation(library, second, client=qdrant)

    rolled = vectorindex.rollback_generation(library, client=qdrant)
    library.refresh_from_db()
    record.refresh_from_db()
    assert rolled.vector_collection == first.collection
    assert library.vector_collection == first.collection
    assert record.vector_collection_name == first.collection
    # 回退必须恢复**真实登记维度**，而不是写个 0
    assert rolled.active_generation == first.generation
    assert VectorCollection.objects.get(
        library=library, name=first.collection
    ).dimension == 1024


def test_rollback_to_missing_generation_reports_explicitly(library):
    """回退目标没登记过：必须明确报错，而不是把一个空 collection 当成新版切过去。"""
    plan = vectorindex.GenerationPlan(
        library_id=library.pk,
        generation=99,
        collection=vectorindex.collection_name(library.pk, 99),
        dimension=1024,
        model="m",
        model_version="v",
        previous_generation=0,
        previous_collection="",
        created=True,
    )
    with pytest.raises(vectorindex.VectorIndexError, match="可回退"):
        vectorindex.rollback_generation(library, to_generation=plan.generation)


# --------------------------------------------------- 嵌入写索引：维度与幂等 ----


def test_embed_chunks_writes_points_with_stable_ids(library, make_file):
    """同一份资料的同一段落，两次嵌入必须落到同一个点 id（重试不产生重复向量）。"""
    record = _make_chunks(library, make_file, count=3)
    qdrant = FakeQdrant()
    plan = vectorindex.prepare_generation(
        library, profile=emb.get_profile("bge1024"), client=qdrant
    )
    http = _fake_http(1024)

    # 重建到目标 generation：只写点，不动当前指针
    step = pipeline.embed_chunks(
        record,
        profile=emb.get_profile("bge1024"),
        client=qdrant,
        http_client=http,
        target_collection=plan.collection,
        target_generation=plan.generation,
    )
    assert step.metrics["points"] == 3
    assert step.metrics["pointer_switched"] is False
    assert qdrant.count(collection_name=plan.collection).count == 3

    record.refresh_from_db()
    # 重建目标写完会记录"这份资料已写进目标代次"（下次重跑可续），
    # 但**库的线上指针**必须还没动——检索仍读旧代次。
    KnowledgeLibrary.objects.get(pk=library.pk)
    library.refresh_from_db()
    assert library.vector_collection != plan.collection

    ids_first = set(qdrant.points[plan.collection])
    # 重跑同一个目标：点 id 不变、点数不翻倍（幂等）
    pipeline.embed_chunks(
        record,
        profile=emb.get_profile("bge1024"),
        client=qdrant,
        http_client=http,
        target_collection=plan.collection,
        target_generation=plan.generation,
    )
    assert set(qdrant.points[plan.collection]) == ids_first
    assert qdrant.count(collection_name=plan.collection).count == 3

    # 校验通过后切换：指针与完成计数一起更新
    vectorindex.activate_generation(library, plan, client=qdrant)
    record.refresh_from_db()
    assert record.vector_collection_name == plan.collection
    assert record.embedded_chunk_count == 3
    assert record.vector_status == "indexed"


def test_embed_chunks_refuses_dimension_mismatch_before_upsert(library, make_file):
    """嵌入维度不符：一个点都不许写进去，库仍指向旧 generation。"""
    record = _make_chunks(library, make_file, count=2)
    qdrant = FakeQdrant()
    plan = vectorindex.prepare_generation(
        library, profile=emb.get_profile("bge1024"), client=qdrant
    )

    http = _fake_http(768)  # 服务实际返回 768 维
    with pytest.raises(pipeline.PipelineError):
        pipeline.embed_chunks(
            record,
            profile=emb.get_profile("bge1024"),
            client=qdrant,
            http_client=http,
            target_collection=plan.collection,
            target_generation=plan.generation,
        )
    assert qdrant.points.get(plan.collection, {}) == {}  # 一个点都没写

    library.refresh_from_db()
    # conftest 的 library 夹具默认登记了一个集合（kb_default），但**没有任何 active**：
    # 线上索引确实没被这一次失败的嵌入动过。
    assert VectorCollection.objects.filter(library=library, is_active=True).count() == 0


def test_embed_chunks_without_generation_refuses(library, make_file):
    """库没有可用 collection 时：明确报"未初始化"，不按 generation 猜名字。"""
    record = _make_chunks(library, make_file, count=1)
    # 让 file 与库都没有指针（模拟"全新库、还没建索引"）
    IngestedFile._base_manager.filter(pk=record.pk).update(
        vector_collection_name="", vector_generation=0, vector_collection=""
    )
    KnowledgeLibrary.objects.filter(pk=library.pk).update(
        vector_collection="", active_generation=0
    )
    record.refresh_from_db()
    with pytest.raises(pipeline.PipelineError, match="未初始化"):
        pipeline.embed_chunks(record, profile=emb.get_profile("bge1024"), client=FakeQdrant())


def test_upsert_refuses_vector_with_wrong_length(profile1024):
    """最后一道闸门：即使调用方绕过 embedding 层，upsert 也不收异构向量。"""
    qdrant = FakeQdrant()
    qdrant.create_collection(
        "kb_check", type("V", (), {"size": 1024})()
    )
    points = [
        vectorindex.VectorPoint(point_id="p1", vector=[0.0] * 768, payload={"text": "x"})
    ]
    with pytest.raises(VectorDimensionMismatch):
        vectorindex.upsert_points(qdrant, "kb_check", points, dimension=1024)


# ------------------------------------------------------------- 只读视图声明守卫 ----


def test_readonly_viewset_rejects_mutation_via_decorator(library, api, user):
    """装饰器把"只读"写在视图声明处：即使 URL 前缀没登记也拦得住。"""
    assert getattr(VectorCollectionViewSet, "is_immutable_readonly", False) is True
    api.force_authenticate(user=user)
    response = api.delete("/api/v1/knowledge/collections/1/")
    assert response.status_code in {401, 403}


def test_immutable_endpoint_wraps_function_view():
    from django.http import HttpResponse

    @immutable_endpoint
    def view(request):
        return HttpResponse("ok")

    class FakeRequest:
        method = "DELETE"

    assert view(FakeRequest()).status_code == 403
    assert getattr(view, "is_immutable_readonly") is True


# ------------------------------------------------------------- 只读接口可见性 ----


def test_collections_endpoint_hides_other_libraries(library, other_user, api, make_library):
    other = make_library(code="secret", name="别人的库")
    VectorCollection.objects.create(
        name="kb_secret", dimension=1024, embedding_model="m", library=other
    )
    VectorCollection.objects.create(
        name="kb_mine", dimension=1024, embedding_model="m", library=library
    )
    api.force_authenticate(user=other_user)
    payload = api.get("/api/v1/knowledge/collections/").json()
    names = (
        [row["name"] for row in payload["results"]]
        if "results" in payload
        else [row["name"] for row in payload]
    )
    assert "kb_mine" not in names
    assert "kb_secret" not in names


def test_collection_serializer_exposes_no_storage_path(library, api, user):
    """storage_path 属于内部路径信息，不应出现在接口里。"""
    VectorCollection.objects.create(
        name="kb_path_check",
        dimension=1024,
        embedding_model="m",
        library=library,
        storage_path="/internal/vector_data/qdrant",
    )
    api.force_authenticate(user=user)
    payload = api.get("/api/v1/knowledge/collections/").json()
    rows = payload["results"] if "results" in payload else payload
    for row in rows:
        assert "storage_path" not in row
