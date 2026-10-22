"""按代次**快照固定**绑定模型：本轮增量审查的三点（快照优先/指纹键/拒绝 name 覆盖）。

这些用例刻意不连真实模型、不写正式索引：只验证"该用哪个模型"的裁决与拒绝语义。
"""

from __future__ import annotations

import pytest

from apps.knowledge import embedding as E

pytestmark = pytest.mark.django_db


def _reload_profiles(monkeypatch, env: dict[str, str]):
    """改环境变量后清掉 ``profiles()`` 的 lru_cache 再读取。"""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    E.profiles.cache_clear()
    return E.profiles()


def test_default_profile_is_1024_and_2048_stays_unconfigured():
    table = E.profiles()
    assert table[E.DEFAULT_PROFILE].dimension == 1024
    # 1024 是当前生效；2048 预配置但**未配置端点/模型**，因此不等于就绪
    assert E.active_profile_name() == "bge1024"
    reserved = table["bge2048"]
    assert reserved.dimension == 2048
    assert reserved.configured is False
    report = E.probe_dimension(profile=reserved)
    assert report["ready"] is False and report["actual_dimension"] is None


def test_unknown_profile_name_is_rejected_not_ignored(monkeypatch):
    with pytest.raises(E.EmbeddingError, match="未登记的 profile 名"):
        _reload_profiles(monkeypatch, {"KB_EMBED_PROFILES": '{"bge3072": {"dimension": 3072}}'})
    E.profiles.cache_clear()


def test_name_override_is_rejected(monkeypatch):
    with pytest.raises(E.EmbeddingError, match="不允许覆盖 name"):
        _reload_profiles(
            monkeypatch, {"KB_EMBED_PROFILES": '{"bge2048": {"name": "bge1024"}}'}
        )
    E.profiles.cache_clear()


@pytest.mark.parametrize("bad", ["0", "-1", "abc", "9000", "true"])
def test_illegal_dimension_is_rejected(monkeypatch, bad):
    with pytest.raises(E.EmbeddingError):
        _reload_profiles(
            monkeypatch, {"KB_EMBED_PROFILES": '{"bge2048": {"dimension": "%s"}}' % bad}
        )
    E.profiles.cache_clear()


def test_complete_snapshot_wins_even_if_config_drifted(monkeypatch):
    """同名 profile 的端点被改掉后，**旧代仍固定用快照**（这是本次修正的核心）。"""
    snap = E.profile_for_registration(
        profile="bge2048",
        model="future-embed",
        model_version="v9",
        dimension=2048,
        base_url="http://old-host:8000/v1",
        where="测试代次",
    )
    assert snap.base_url == "http://old-host:8000/v1"
    assert snap.dimension == 2048 and snap.model == "future-embed"
    # 配置改成"新服务"后，快照解析结果不变（只是 note 里提示漂移）
    _reload_profiles(
        monkeypatch,
        {
            "KB_EMBED_PROFILES": '{"bge2048": {"model": "future-embed", "model_version": "v9",'
            ' "base_url": "http://new-host:8000/v1", "dimension": 2048}}'
        },
    )
    again = E.profile_for_registration(
        profile="bge2048",
        model="future-embed",
        model_version="v9",
        dimension=2048,
        base_url="http://old-host:8000/v1",
        where="测试代次",
    )
    assert again.base_url == "http://old-host:8000/v1", "旧代不得悄悄转向新服务"
    assert "配置漂移" in again.note
    E.profiles.cache_clear()


def test_incomplete_snapshot_requires_strict_config_match(monkeypatch):
    _reload_profiles(
        monkeypatch,
        {
            "KB_EMBED_PROFILES": '{"bge2048": {"model": "future-embed", "model_version": "v9",'
            ' "base_url": "http://cfg:8000/v1", "dimension": 2048}}'
        },
    )
    # 严格相符 → 允许用配置补齐端点（历史行缺端点）
    ok = E.profile_for_registration(
        profile="bge2048", model="future-embed", model_version="v9", dimension=2048, base_url=""
    )
    assert ok.base_url == "http://cfg:8000/v1"
    # 维度不符 → 显式拒绝，绝不"补齐后查询"
    with pytest.raises(E.EmbeddingError, match="不严格相符"):
        E.profile_for_registration(
            profile="bge2048", model="future-embed", model_version="v9", dimension=1024, base_url=""
        )
    # profile 已从配置里移除且快照不完整 → 拒绝（不回落全局）
    with pytest.raises(E.EmbeddingError):
        E.profile_for_registration(
            profile="gone", model="", model_version="", dimension=0, base_url=""
        )
    E.profiles.cache_clear()


def test_fingerprint_distinguishes_same_name_different_endpoint():
    a = E.ProfileInfo(
        name="bge2048", model="m", model_version="v1", base_url="http://a:1/v1", dimension=2048
    )
    b = E.ProfileInfo(
        name="bge2048", model="m", model_version="v1", base_url="http://b:1/v1", dimension=2048
    )
    assert E.profile_fingerprint(a) != E.profile_fingerprint(b)
