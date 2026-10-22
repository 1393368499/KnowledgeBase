"""系统配置页「嵌入模型 profile（当前 / 预留）」的回归。

用户要求（2026-10-17）：设置页保留四类远程服务表，**另**显示当前 1024 与预留 2048，
并写清"是否已配置 / 是否实测就绪"；可以对**指定的已配置非当前 profile** 预检；
未配置的不能假装就绪；默认不主动探测。

这里做的是**源码级契约检查**（和 ``tests/test_settings_probe_targets.py`` 同一手法）：
页面是纯静态 JS，没有构建步骤，靠断言"必须出现/必须不出现哪些结构"来防回退。
真正的探测行为由 ``tests/test_on_demand_probe.py`` 与 ``test_vector_profiles.py``
在后端侧覆盖。
"""

from __future__ import annotations

from pathlib import Path

PAGES = Path("apps/webui/static/webui/kb-pages.js")


def _source() -> str:
    return PAGES.read_text(encoding="utf-8")


# ------------------------------------------------------- profile 明细表 ----

def test_profile_table_is_rendered_next_to_the_four_service_rows():
    """四类远程服务表保留，并且**另有一张** profile 明细表（当前/预留）。"""
    src = _source()
    # 四类服务的行仍然在（不能被 profile 表取代）
    for target in ('target: "embedding"', 'target: "rerank"', 'target: "ocr"', 'target: "llm"'):
        assert target in src, f"四类服务表被破坏：缺少 {target}"
    assert "远程模型服务" in src
    assert "嵌入模型 profile（当前 / 预留）" in src
    # 明细表必须逐 profile 渲染（不是只渲染当前生效那一个）
    assert "profiles.map(function (item)" in src


def test_each_profile_row_exposes_status_dimension_and_endpoint():
    src = _source()
    assert "item.configured === true" in src, "未显式判断 configured，可能把未配置当已配置"
    assert "当前生效" in src and "已配置（预留）" in src
    assert "未配置（未部署）" in src, "未配置必须明确写成未配置，不能只写未就绪"
    assert "String(item.dimension || \"\")" in src, "声明维度没显示出来"
    assert "item.endpoint || \"未配置\"" in src


def test_unconfigured_profile_cannot_be_probed():
    """未配置的 profile **不给可点的预检按钮**（不能假装就绪）。"""
    src = _source()
    # 已配置 → 有 data-probe-profile；未配置 → disabled 且写明原因
    assert "data-probe-profile='" in src
    assert "该 profile 未配置 model/端点，无法预检" in src
    assert "未配置不等于未就绪，也不代表可以启用" in src


def test_probe_is_on_demand_and_never_automatic():
    """默认不探测：初始加载只取配置；真实探测只在点击处理器里。"""
    src = _source()
    # 初始加载：不带 probe 参数
    assert 'KB.api("/api/v1/knowledge/models/")' in src, "初始加载不该主动探测"
    # 真实探测调用必须带 profile 白名单键
    assert "?probe=1&target=embedding&profile=" in src
    assert "encodeURIComponent(profileName)" in src
    # 结果必须按 profile 名回填（不能串行）
    assert "cell.getAttribute(\"data-profile-latency\") !== profileName" in src


def test_probe_result_reports_the_measured_dimension():
    """预检结果必须带**实测维度**：那才是"这套模型真的返回 N 维"的证据。"""
    src = _source()
    assert "actual_dimension" in src
    assert "实测维度" in src
    assert "未就绪" in src, "探测失败要显示未就绪，不能显示成正常"


def test_profile_probe_button_is_admin_gated():
    """预检是管理员能力：普通账号必须被置灰（后端也会 403）。"""
    src = _source()
    assert '"[data-probe-profile]"' in src, "预检按钮没进管理员专属选择器"
