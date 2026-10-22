"""离线测试适配器的安全边界测试。

核心命题（用户 2026-10-13 指示）：**假向量绝不能污染正式索引**。
所以这里测的重点不是"适配器能不能用"，而是"它能不能被误用"。
"""

from __future__ import annotations

import pytest
from django.test import override_settings

from apps.knowledge import testing_adapter as ta
from apps.knowledge.testing_adapter import (
    TEST_COLLECTION_SUFFIX,
    OfflineAdapter,
    TestAdapterDisabled,
    TestAdapterForbidden,
)

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------- 默认关闭 ----


def test_adapter_is_disabled_by_default():
    """没有显式开关时，适配器不可用——生产路径拿不到假向量。"""
    assert ta.enabled() is False
    with pytest.raises(TestAdapterDisabled):
        OfflineAdapter()


@override_settings(KB_TEST_ADAPTER="offline")
def test_adapter_enabled_only_with_explicit_switch():
    assert ta.enabled() is True
    adapter = OfflineAdapter(dimension=8)
    assert adapter.tag == ta.TAG == "offline-test"


@override_settings(KB_TEST_ADAPTER="OFFLINE")
def test_switch_is_case_insensitive():
    assert ta.enabled() is True


@override_settings(KB_TEST_ADAPTER="1")
def test_unknown_switch_value_does_not_enable():
    """只认 "offline" 这一个值，避免有人随手写个 1 就把假向量打开了。"""
    assert ta.enabled() is False


# ------------------------------------------------------ 只能写测试 collection ----


def test_collection_name_has_obvious_suffix():
    name = ta.test_collection_name(library_id=3, generation=2)
    assert name.endswith(TEST_COLLECTION_SUFFIX)
    assert "__offline_test" in name


@pytest.mark.parametrize(
    "name",
    [
        "kb-1-g1",  # 正式 collection 命名
        "kb-1-g1-copy",
        "offline_test",  # 后缀不对（前面没有 __）
        "",
    ],
)
def test_fake_vectors_refused_for_real_collections(name):
    with pytest.raises(TestAdapterForbidden):
        ta.assert_test_collection(name)


def test_fake_vectors_allowed_for_test_collection():
    ta.assert_test_collection(ta.test_collection_name(1, 1))  # 不抛即通过


@override_settings(KB_TEST_ADAPTER="offline")
def test_adapter_embed_validates_collection_when_given():
    adapter = OfflineAdapter(dimension=4)
    # 不给 collection：不校验（纯本地计算）
    assert len(adapter.embed(["甲"])[0]) == 4
    # 给正式 collection：直接拒绝
    with pytest.raises(TestAdapterForbidden):
        adapter.embed(["甲"], collection="kb-1-g1")
    # 给测试 collection：放行
    assert len(adapter.embed(["甲"], collection=ta.test_collection_name(1, 1))[0]) == 4


# ------------------------------------------------- 经过 embedding 选路仍安全 ----


@override_settings(KB_TEST_ADAPTER="offline")
def test_embed_for_indexing_refuses_real_collection():
    """这是最关键的一条：即使有人走"要写索引"的正式入口，也进不去正式 collection。"""
    from apps.knowledge.embedding import embed_for_indexing

    with pytest.raises(TestAdapterForbidden):
        embed_for_indexing(["甲"], collection="kb-9-g1")


@override_settings(KB_TEST_ADAPTER="offline")
def test_embed_for_indexing_marks_adapter_usage():
    from apps.knowledge.embedding import embed_for_indexing

    vectors, used = embed_for_indexing(
        ["甲", "乙"], collection=ta.test_collection_name(9, 1)
    )
    assert used is True
    assert len(vectors) == 2
    assert all(len(row) == 1024 for row in vectors)


@override_settings(KB_TEST_ADAPTER="")
def test_embed_for_indexing_without_adapter_does_not_fake():
    """没开适配器时，走真实路径：网络不可达就必须失败，不能静默返回假向量。"""
    from apps.knowledge.embedding import embed_for_indexing
    from apps.knowledge.resilience import ServiceUnavailable

    import httpx

    def boom(request):
        raise httpx.ConnectError("连接被拒绝", request=request)

    client = httpx.Client(transport=httpx.MockTransport(boom))
    with pytest.raises(ServiceUnavailable):
        embed_for_indexing(["甲"], collection="kb-9-g1", client=client, max_attempts=1)


# ------------------------------------------------------------- 确定性行为 ----


@override_settings(KB_TEST_ADAPTER="offline")
def test_embedding_is_deterministic_and_unit_length():
    adapter = OfflineAdapter(dimension=16)
    first = adapter.embed(["同一段文本"])[0]
    second = adapter.embed(["同一段文本"])[0]
    assert first == second
    norm = sum(value * value for value in first) ** 0.5
    assert abs(norm - 1.0) < 1e-9


@override_settings(KB_TEST_ADAPTER="offline")
def test_different_text_gives_different_vector():
    adapter = OfflineAdapter(dimension=16)
    assert adapter.embed(["甲"])[0] != adapter.embed(["乙"])[0]


@override_settings(KB_TEST_ADAPTER="offline")
def test_dimension_follows_setting():
    adapter = OfflineAdapter()
    assert len(adapter.embed(["甲"])[0]) == 1024  # KB_TEST_ADAPTER_DIMENSION 默认


@override_settings(KB_TEST_ADAPTER="offline")
def test_rerank_is_deterministic_and_coverage_based():
    adapter = OfflineAdapter(dimension=4)
    docs = ["质保期 12 个月", "完全无关的一段", "质保期"]
    ranked = adapter.rerank("质保期", docs)
    assert ranked[0][0] == 0  # 命中且最短 -> 覆盖率并列时按稳定顺序
    scores = dict(ranked)
    assert scores[1] == 0.0  # 完全没命中
    # 同样的输入永远同样的结果
    assert adapter.rerank("质保期", docs) == ranked


@override_settings(KB_TEST_ADAPTER="offline")
def test_ocr_returns_marked_placeholder():
    adapter = OfflineAdapter(dimension=4)
    text = adapter.ocr(b"\x89PNG fake", hint="付款节点确认单")
    assert "离线测试适配器" in text
    assert "付款节点确认单" in text


def test_mark_adds_visible_tag():
    marked = ta.mark({"text": "x"})
    assert marked["is_test_adapter"] is True
    assert marked["adapter_tag"] == "offline-test"
    assert marked["text"] == "x"
    # 不修改原字典（避免调用方被副作用影响）
    original = {"text": "x"}
    ta.mark(original)
    assert "is_test_adapter" not in original
