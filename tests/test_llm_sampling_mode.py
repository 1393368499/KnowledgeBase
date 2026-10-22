"""LLM 采样口径回归：全系统统一 greedy（AS711 混采崩引擎的**客户端侧**防线）。

背景（证据与复现见 ``docs/as711-sophgo-vllm-mixed-sampling-crash.md``）：
AS711 的算能 vLLM-TPU 引擎在**同一个 batch** 里混到 greedy 与 random 请求时，
会因 ``aten::where.self_out`` 未实现而 EngineCore fatal 退出、整个模型服务连不上。
服务器一行不动，本系统要保证的只有一件事：**我们永远不发 random 请求**。

验收点：
  * 默认（``KB_LLM_GREEDY_ONLY`` 打开）下，profile 与**实际请求体**的
    ``temperature`` 都是 0.0；
  * 直接手搓的随机 profile 也会在请求体里被压成 0.0（单点出口，绕不过去）；
  * ``KB_LLM_GREEDY_ONLY=0`` 时配置值原样生效（厂家修好后恢复随机采样不用改代码）；
  * 分类 profile 与对话同口径（0.0）；
  * ``KB_LLM_TEMPERATURE`` 真的在 settings 里有定义 —— 原先它只出现在
    ``getattr`` 兜底里，写进环境变量并不生效，这个缺陷一并回归。
"""

from __future__ import annotations

import pytest
from django.conf import settings as django_settings
from django.test import override_settings

from apps.planner import llm as llm_module


@pytest.fixture
def captured_payloads(monkeypatch):
    """把外呼截成"只记录请求体"，**一个字节都不发出去**。"""
    calls: list[dict] = []

    def fake_post_json(**kwargs):
        calls.append(kwargs["payload"])
        return {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {},
        }

    monkeypatch.setattr(llm_module, "post_json", fake_post_json)
    return calls


def _chat(profile) -> None:
    llm_module.LlmClient(profile).chat([{"role": "user", "content": "ping"}])


def test_default_profile_is_greedy():
    """默认口径：profile 本身就如实反映"会发 greedy"，而不是发请求前再偷偷改。"""
    assert llm_module.greedy_only() is True
    assert llm_module.default_profile().temperature == 0.0


def test_document_profile_keeps_greedy():
    """撰写文档 profile 从对话 profile 派生：换预算不能又变回随机。"""
    assert llm_module.document_profile().temperature == 0.0


def test_effective_temperature_downgrades_random_values():
    assert llm_module.effective_temperature(0.2) == 0.0
    assert llm_module.effective_temperature(1.0) == 0.0


def test_request_body_never_carries_random_temperature(captured_payloads):
    """手搓随机 profile 也绕不过单点出口：请求体里的 temperature 必须是 0.0。"""
    profile = llm_module.LlmProfile(
        base_url="http://llm.example/v1", model="test-llm", temperature=0.7
    )
    _chat(profile)
    assert captured_payloads[0]["temperature"] == 0.0


@override_settings(KB_LLM_GREEDY_ONLY=False, KB_LLM_TEMPERATURE=0.7)
def test_switch_off_restores_configured_temperature(captured_payloads):
    """厂家镜像修好后：只翻开关就能回到随机采样，不用改代码。"""
    assert llm_module.greedy_only() is False
    profile = llm_module.default_profile()
    assert profile.temperature == 0.7
    _chat(profile)
    assert captured_payloads[0]["temperature"] == 0.7


@override_settings(KB_LLM_GREEDY_ONLY=False)
def test_replace_style_profile_is_respected_when_switch_off():
    """探测路径用 ``dataclasses.replace(profile, ...)``，开关关闭时不能被顺手压掉。"""
    import dataclasses

    profile = dataclasses.replace(llm_module.default_profile(), temperature=0.9)
    assert llm_module.effective_temperature(profile.temperature) == 0.9


def test_classifier_profile_stays_greedy():
    """资料性质分类是本系统另一条会调 LLM 的路径，必须同口径。"""
    from apps.knowledge.classification import classifier_profile

    assert classifier_profile().temperature == 0.0


def test_sampling_settings_exist_so_env_vars_actually_work():
    """回归：原先 ``KB_LLM_TEMPERATURE`` 只在 getattr 兜底里出现，写环境变量也不生效。"""
    assert hasattr(django_settings, "KB_LLM_GREEDY_ONLY")
    assert hasattr(django_settings, "KB_LLM_TEMPERATURE")
