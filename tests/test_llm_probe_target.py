"""LLM 目标按需探测（`?probe=1&target=llm`）的回归。

验收点：
  * 真的走一次 `LlmClient.chat` 的**最小**调用（不是只 GET /models），返回实测延迟；
  * 结果**明确标注** ``connectivity_only=False``：这是真实调用，不再是"只证明连通"的口径；
  * 推理型模型在小预算下正文可能为空 / 被截断 —— 真实客户端会抛
    ``LLMError``（文案含"token 上限截断（finish_reason=length）"），视图必须把这种
    情况算成**连通**但截断，如实带出 ``finish_reason``，且**不算错误**；
  * HTTP 错误与非 200 分支如实报错（不编造延迟、不谎报连通）；
  * 离线开关打开时**一个请求都不发**；
  * 未配置端点/模型时明确报错且不发请求；
  * **绝不回显 API key**。

所有测试都把 ``LlmClient`` 换成替代类（记录 profile / messages，按脚本返回或抛异常），
**不会真的外呼 AS711**，也不会碰熔断/限流用的 Redis。
"""

from __future__ import annotations

import json

import httpx
import pytest
from django.test import override_settings

pytestmark = pytest.mark.django_db

URL = "/api/v1/knowledge/models/"


@pytest.fixture
def llm_configured(settings):
    settings.MODEL_SERVICES = {
        "llm": {"base_url": "http://llm.example/v1", "model": "test-llm"},
    }
    settings.MODEL_API_KEY = "super-secret-key-should-never-leak"
    settings.KB_MODEL_CALLS_DISABLED = False
    return settings


@pytest.fixture
def fake_llm(monkeypatch):
    """替代 `apps.planner.llm.LlmClient`。

    为什么换**客户端类**而不是补 httpx 层：视图现在是
    ``LlmClient(probe_profile).chat([...])``；换掉这个类才能既验证"真的走了一次
    真实客户端调用"，又完全不外呼（连带绕开真实链路里的熔断/跨进程限流 Redis）。
    """
    from apps.planner import llm as llm_module

    class FakeLlmClient:
        instances: list = []
        reply = None
        error: Exception | None = None

        def __init__(self, profile=None, **kwargs):  # noqa: ARG002 - 与真实签名一致
            self.profile = profile
            self.messages = None
            FakeLlmClient.instances.append(self)

        def chat(self, messages, **kwargs):  # noqa: ARG002 - 与真实签名一致
            self.messages = messages
            if FakeLlmClient.error is not None:
                raise FakeLlmClient.error
            return FakeLlmClient.reply

    monkeypatch.setattr(llm_module, "LlmClient", FakeLlmClient)
    return FakeLlmClient


def _truncated_error() -> Exception:
    """用**产品自己的**截断文案造异常。

    为什么不硬编码字符串：视图正是按"token 上限截断 / finish_reason=length"来识别
    "被探测预算截断"的。用 `_truncated_reason` 生成，产品文案与视图判据一旦脱钩，
    这条用例立刻失败 —— 这正是要守住的耦合。
    """
    from apps.planner.llm import LLMError, LlmReply, _truncated_reason

    return LLMError(_truncated_reason(LlmReply(finish_reason="length", content="")))


def test_llm_probe_reports_real_call_latency(admin_client, llm_configured, fake_llm):
    """成功路径：真的调一次 `LlmClient.chat`（极小预算），返回实测延迟。"""
    from apps.planner.llm import LlmReply

    fake_llm.reply = LlmReply(
        content="pong", finish_reason="stop", usage={"completion_tokens": 3}
    )
    body = admin_client.get(URL, {"probe": "1", "target": "llm"}).json()
    payload = body["llm"]

    assert payload["implemented"] is True, "llm 目标仍未接入"
    assert payload["connectivity_only"] is False, "已是真实调用，不再是只证明连通"
    assert payload["connected"] is True
    assert "probe_error" not in payload, payload
    assert payload["probe_latency_ms"] >= 0
    assert payload["finish_reason"] == "stop"
    assert payload["content_empty"] is False
    # 真的打了 chat（而不是只 GET /models），而且探测预算**很小**
    assert fake_llm.instances, "没有经过 LlmClient 调用路径"
    probe = fake_llm.instances[0]
    assert probe.messages == [{"role": "user", "content": "ping"}]
    assert probe.profile.max_tokens == 64, "探测预算必须很小"
    assert probe.profile.temperature == 0.0
    assert "不代表回答质量" in payload["note"]
    # **绝不回显凭证**
    assert "super-secret-key-should-never-leak" not in json.dumps(body)


def test_llm_probe_reports_non_empty_content(admin_client, llm_configured, fake_llm):
    """模型真的回了正文时，不带"正文为空"的说明，并如实带出 usage。"""
    from apps.planner.llm import LlmReply

    fake_llm.reply = LlmReply(
        content="pong", finish_reason="stop", usage={"total_tokens": 9}
    )
    payload = admin_client.get(URL, {"probe": "1", "target": "llm"}).json()["llm"]
    assert payload["connected"] is True
    assert payload["content_empty"] is False
    assert payload["usage"] == {"total_tokens": 9}
    assert "正文为空" not in payload["note"]


def test_llm_probe_truncated_by_tiny_budget_is_still_connected(
    admin_client, llm_configured, fake_llm
):
    """被探测预算截断 **≠** 服务不可用。

    真实客户端在 `finish_reason=length` 时抛 `LLMError`（文案含"token 上限截断"），
    视图必须把它判成"连通但被预算截断"：``connected is True`` 且**没有** probe_error，
    同时如实带出 ``finish_reason="length"``。
    """
    fake_llm.error = _truncated_error()
    payload = admin_client.get(URL, {"probe": "1", "target": "llm"}).json()["llm"]

    assert payload["connected"] is True
    assert "probe_error" not in payload, payload
    assert payload["finish_reason"] == "length"
    assert payload["content_empty"] is True
    assert payload["probe_latency_ms"] >= 0
    assert "预算" in payload["note"] or "截断" in payload["note"], payload["note"]


def test_llm_probe_reports_http_error(admin_client, llm_configured, fake_llm):
    """HTTP 500：如实报错（异常类名 + 状态码），不谎报连通，也仍给耗时。

    旧口径里这里断言 ``probe_error`` 直接以 "HTTP 500" 开头（那时是视图自己手搓
    httpx 分支拼的字符串）。现在真实链路是
    ``post_json`` →（5xx 可重试）→ 重试耗尽 → ``RetryExhausted``，错误文本里带着
    "服务返回 500：…"，类名则由视图统一前缀。
    """
    from apps.knowledge.resilience import RetryExhausted

    fake_llm.error = RetryExhausted(
        "llm:http://llm.example/v1",
        "post 尝试 2 次仍未成功：服务返回 500：boom",
        attempts=2,
    )
    payload = admin_client.get(URL, {"probe": "1", "target": "llm"}).json()["llm"]

    assert payload["probe_error"].startswith("RetryExhausted: "), payload
    assert "服务返回 500" in payload["probe_error"], payload
    assert payload.get("connected") is not True
    assert payload["probe_latency_ms"] >= 0  # 失败也要给出耗时（诊断用）


def test_llm_probe_reports_connection_error(admin_client, llm_configured, fake_llm):
    """连接错误：异常类名与原因必须如实带出来（"连不上"≠"模型答不好"）。"""
    fake_llm.error = httpx.ConnectError("connection refused")
    payload = admin_client.get(URL, {"probe": "1", "target": "llm"}).json()["llm"]

    assert "ConnectError" in payload["probe_error"], payload
    assert "connection refused" in payload["probe_error"], payload
    assert payload.get("connected") is not True
    assert payload["probe_latency_ms"] >= 0


@override_settings(KB_MODEL_CALLS_DISABLED=True)
def test_llm_probe_sends_nothing_when_disabled(admin_client, llm_configured, monkeypatch):
    """离线开关打开 → **一个请求都不发**（外呼即失败）。"""

    def must_not_be_called(*a, **k):
        raise AssertionError("离线模式下不应发起任何外呼")

    monkeypatch.setattr(httpx, "post", must_not_be_called)
    payload = admin_client.get(URL, {"probe": "1", "target": "llm"}).json()["llm"]
    assert payload["skipped"] is True
    assert "禁用" in payload["probe_error"]
    assert "probe_latency_ms" not in payload, "没发请求就不该有延迟"


def test_llm_probe_without_endpoint_reports_and_sends_nothing(
    admin_client, settings, monkeypatch
):
    settings.MODEL_SERVICES = {"llm": {"base_url": "", "model": ""}}
    settings.KB_MODEL_CALLS_DISABLED = False

    def must_not_be_called(*a, **k):
        raise AssertionError("未配置端点时不应发起外呼")

    monkeypatch.setattr(httpx, "post", must_not_be_called)
    payload = admin_client.get(URL, {"probe": "1", "target": "llm"}).json()["llm"]
    assert "未配置" in payload["probe_error"]
    assert payload["implemented"] is True


def test_llm_probe_requires_system_admin(auth_api, user):
    assert auth_api(user).get(URL, {"probe": "1", "target": "llm"}).status_code == 403
