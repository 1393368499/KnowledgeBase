"""按**完整快照指纹**分组的熔断隔离（审查建议，2026-10-17）。

真实缺陷：熔断键早先是 ``embed:<profile 名>``。profile 名是**配置项的名字**，
不是模型身份 —— 同一个名字换了端点/版本之后，两套服务共用同一个熔断器：

* 旧端点挂了 → 把新端点那套一起熔断（健康组被降级）；
* 旧端点"健康" → 新端点持续的故障被它的成功计数掩盖。

换模型期间两代索引同时在线上，这是实打实的串扰。所以 HTTP 侧
（``embedding.embed_texts``）与搜索侧的预检查（``pipeline._group_circuit_open``）
必须共用 ``embedding.embedding_service_name``（完整快照指纹的无歧义哈希键）。
"""

from __future__ import annotations

import pytest

from apps.knowledge import embedding as emb
from apps.knowledge import pipeline, resilience
from apps.knowledge.models import IngestedFile, KnowledgeLibrary, ParseStatus, VectorCollection
from apps.knowledge.vectorindex import SearchCandidate

pytestmark = pytest.mark.django_db

ENDPOINT_A = "http://172.16.100.114:8000/v1"
ENDPOINT_B = "http://172.16.100.200:8000/v1"


def _candidate(file_id: int) -> SearchCandidate:
    return SearchCandidate(
        point_id=f"p{file_id}", score=0.9, collection="c", payload={"file_id": file_id}
    )


def _snapshot(*, base_url: str, dimension: int = 1024, name: str = "bge1024"):
    """同名 profile、不同端点/版本的两套服务（合法：两端都返回声明维度）。"""
    return emb.ProfileInfo(
        name=name,
        model="bge-large-zh-v1.5",
        model_version="v1",
        dimension=dimension,
        base_url=base_url,
    )


def test_service_name_differs_for_same_profile_name_with_different_endpoint():
    a = _snapshot(base_url=ENDPOINT_A)
    b = _snapshot(base_url=ENDPOINT_B)
    assert a.name == b.name, "前提：两个 profile 的名字相同"
    key_a = emb.embedding_service_name(a)
    key_b = emb.embedding_service_name(b)
    assert key_a != key_b, "同名不同端点必须落成不同的熔断键（否则会互相拖垮）"
    # 仍然可读：带 profile 名，便于日志/指标里认出来
    assert key_a.startswith("embed:bge1024#")
    assert key_b.startswith("embed:bge1024#")
    # **SHA256 全量**（不是截断摘要）：截断只能说"碰撞概率很低"，
    # 而"两套不同配置被当成同一个服务"是要命的错。
    assert len(key_a.split("#", 1)[1]) == 64, key_a
    # 每次调用结果稳定（同一个快照 → 同一个键）
    assert emb.embedding_service_name(a) == key_a


def test_profile_fingerprint_has_no_field_separator_collision():
    """指纹必须**规范无歧义**：字段里含分隔符不能拼出另一组的同一串。

    早先是 ``"|".join([...])``：``model="a|b", version="c"`` 与
    ``model="a", version="b|c"`` 会得到完全相同的指纹 —— 两组不同模型共用同一个
    查询向量缓存键与熔断器，是静默的串模型。现在用规范 JSON 数组编码。
    """
    left = _snapshot(base_url=ENDPOINT_A)
    right = _snapshot(base_url=ENDPOINT_A)
    # 手工构造"分隔符搬移"的两种组合
    left = emb.ProfileInfo(
        name=left.name, model="a|b", model_version="c",
        dimension=left.dimension, base_url=left.base_url,
    )
    right = emb.ProfileInfo(
        name=right.name, model="a", model_version="b|c",
        dimension=right.dimension, base_url=right.base_url,
    )
    assert emb.profile_fingerprint(left) != emb.profile_fingerprint(right)
    assert emb.embedding_service_name(left) != emb.embedding_service_name(right)
    # 端点里含分隔符同样不能碰撞
    x = emb.ProfileInfo(
        name="p", model="m", model_version="v", dimension=1024,
        base_url="http://h/v1|x",
    )
    y = emb.ProfileInfo(
        name="p", model="m", model_version="v", dimension=1024,
        base_url="http://h/v1",
    )
    assert emb.profile_fingerprint(x) != emb.profile_fingerprint(y)


def test_embed_texts_and_search_precheck_use_the_same_key(monkeypatch):
    """HTTP 侧与搜索侧必须共用同一个键（否则预检查看的是另一个熔断器）。"""
    prof = _snapshot(base_url=ENDPOINT_A)
    expected = emb.embedding_service_name(prof)
    seen: list[str] = []

    real_breaker_for = resilience.breaker_for

    def _spy(name, **kwargs):
        seen.append(name)
        return real_breaker_for(name, store=False, **kwargs)

    # embedding 模块与 pipeline 模块都从 .resilience 导入 breaker_for
    monkeypatch.setattr(emb, "breaker_for", _spy)
    monkeypatch.setattr(resilience, "breaker_for", _spy)

    assert pipeline._group_circuit_open(prof, prof.name) is False
    assert seen and seen[-1] == expected, f"搜索侧键不对：{seen}"
    assert emb.embedding_service_name(prof) == expected


def test_one_broken_endpoint_does_not_degrade_the_healthy_group(monkeypatch):
    """同名不同端点：一套熔断只降级那一组，健康组照常走 dense。"""
    library_a = KnowledgeLibrary.objects.create(code="lib-a", name="A 库")
    library_b = KnowledgeLibrary.objects.create(code="lib-b", name="B 库")
    prof_a = _snapshot(base_url=ENDPOINT_A)
    prof_b = _snapshot(base_url=ENDPOINT_B)

    for library, prof in ((library_a, prof_a), (library_b, prof_b)):
        VectorCollection.objects.create(
            name=f"kb-{library.pk}-g1",
            dimension=prof.dimension,
            embedding_model=prof.model,
            model_version=prof.model_version,
            profile=prof.name,          # **同名**
            base_url=prof.base_url,     # **不同端点**
            library=library,
            storage_mode="server",
            storage_path="data/vector_data/qdrant",
            is_active=True,
            build_status="ready",
        )
        IngestedFile.objects.create(
            library=library,
            original_name=f"{library.code}.md",
            content_sha256=f"{library.pk:064d}",
            size_bytes=10,
            parse_status=ParseStatus.DONE,
            vector_collection=f"kb-{library.pk}-g1",
            vector_collection_name=f"kb-{library.pk}-g1",
            chunk_count=1,
            embedded_chunk_count=1,
        )

    # A 组熔断打开（进程内后端：测试不碰共享 Redis）。
    # 先丢掉注册表里可能被别的用例建过的同键实例：``breaker_for`` 对已存在的
    # 条目**不会**用新参数重建（这是它的正确行为），直接复用会让本用例的
    # failure_threshold=1 失效，从而"看起来熔断没生效"。
    key_a = emb.embedding_service_name(prof_a)
    resilience.BREAKERS.pop(key_a, None)
    breaker_a = resilience.breaker_for(
        key_a, failure_threshold=1, cooldown_seconds=300, store=False
    )
    breaker_a.record_failure("模拟端点 A 故障")
    assert breaker_a.allow_request() is False, "前提自检：A 组必须已熔断"
    # 健康组（不同端点 → 不同键）必须仍然放行
    key_b = emb.embedding_service_name(prof_b)
    assert key_b != key_a
    assert resilience.breaker_for(key_b, store=False).allow_request() is True

    queried: list[str] = []

    def _dense(client, *, collections, **kwargs):
        name = collections[0]
        queried.append(name)
        if name == f"kb-{library_a.pk}-g1":
            raise AssertionError("熔断组不该被查询")
        return [_candidate(200)]

    monkeypatch.setattr(pipeline, "build_qdrant_client", lambda: object())
    monkeypatch.setattr(pipeline.vectorindex, "search_dense", _dense)
    monkeypatch.setattr(pipeline.vectorindex, "search_sparse", lambda *a, **k: [])
    monkeypatch.setattr(pipeline, "_db_keyword_search", lambda **k: [])
    monkeypatch.setattr(pipeline, "_drop_tombstoned", lambda items, report: items)
    monkeypatch.setattr(
        pipeline, "embed_texts", lambda texts, profile=None: [[0.0] * 1024]
    )
    monkeypatch.setattr(pipeline.vectorindex, "ensure_collection", lambda *a, **k: None)

    outcome = pipeline.search(
        query="质保期",
        libraries=[library_a, library_b],
        permission_codes=None,
        limit=10,
        use_rerank=False,
        client=object(),
    )

    report = outcome["report"]
    # 健康组确实被查了，熔断组没有被查
    assert queried == [f"kb-{library_b.pk}-g1"], queried
    # 逐组降级的事实如实回报：只报 A 组，不能渲染成"整个语义检索不可用"
    assert report["dense_groups_skipped"] == [prof_a.name]
    assert report["dense_groups_queried"] == [prof_b.name]
    assert report["dense_skipped_by_breaker"] is False, (
        "还有健康组在跑 dense，不能声称整条路都被跳过"
    )
    failed = [item["collection"] for item in report["failed_collections"]]
    assert failed == [f"kb-{library_a.pk}-g1"], failed
    assert {hit.get("file_id") for hit in outcome["results"]} == {200}
