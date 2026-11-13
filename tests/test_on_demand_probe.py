"""按需探测 + 真实延迟（目标 ③："系统配置按需探测并显示延迟"）。

边界（硬约束）：
  * **不做自动探测** —— 默认只报告配置状态；只有显式请求才真实调用；
  * 延迟是**实测**值（失败也记延迟 + 错误），**绝不编造**"看起来正常"的延迟；
  * 不启停、不修改任何外部服务（只发只读探测请求）。
"""

from __future__ import annotations

from apps.planner.operations import get


def _armed(admin_user, params):
    from django.utils import timezone

    from apps.planner.models import Conversation, OperationRecord
    from tests.planner_fixtures import require_spec as get_spec
    from apps.planner.services import _describe_target

    ref, label = _describe_target(get_spec("test_model_services"), params)
    conversation = Conversation.objects.create(user=admin_user)
    return OperationRecord.objects.create(
        user=admin_user,
        conversation=conversation,
        intent="test_model_services",
        params=params,
        target_ref=ref,
        target_label=label,
        status=OperationRecord.Status.ARMED,
        armed_at=timezone.now(),
        armed_expires_at=timezone.now() + timezone.timedelta(minutes=10),
    )


def test_intent_accepts_probe_param():
    spec = get("test_model_services")
    assert spec is not None
    assert "probe" in spec.params, "必须能显式请求探测（否则无法'按需探测'）"


def test_default_does_not_probe(db, admin_user, monkeypatch):
    """**默认不探测**：不请求就不该发生任何外呼。"""
    from apps.knowledge import embedding
    from apps.planner import services

    called = {"n": 0}

    def spy(*_a, **_k):
        called["n"] += 1
        return {}

    monkeypatch.setattr(embedding, "assert_profile_ready", spy)
    operation = _armed(admin_user, {})
    result = services.execute_operation(admin_user, operation)

    payload = result["result"]
    assert payload["probe"] is False
    assert called["n"] == 0, "默认路径不得发起真实探测"
    assert "latency_ms" not in payload["embedding"], "没探测就不该有延迟数字"


def test_explicit_probe_reports_real_latency(db, admin_user, monkeypatch):
    """显式请求时才探测，并给出**实测**延迟。"""
    from apps.knowledge import embedding
    from apps.planner import services

    def fake_ready(profile, **_k):
        return {"ready": True, "dimension": getattr(profile, "dimension", 1024)}

    monkeypatch.setattr(embedding, "assert_profile_ready", fake_ready)
    operation = _armed(admin_user, {"probe": "1"})
    result = services.execute_operation(admin_user, operation)

    payload = result["result"]
    assert payload["probe"] is True
    latency = payload["embedding"].get("latency_ms")
    assert isinstance(latency, (int, float)) and latency >= 0, payload["embedding"]
    assert payload["embedding"]["ready"] is True


def test_probe_failure_is_reported_with_latency(db, admin_user, monkeypatch):
    """探测失败也要给出延迟与错误 —— 不隐藏、不编造成功。"""
    from apps.knowledge import embedding
    from apps.planner import services

    def boom(*_a, **_k):
        raise RuntimeError("模拟探测失败")

    monkeypatch.setattr(embedding, "assert_profile_ready", boom)
    operation = _armed(admin_user, {"probe": "true"})
    result = services.execute_operation(admin_user, operation)

    payload = result["result"]
    assert payload["embedding"]["ready"] is False
    assert "模拟探测失败" in payload["embedding"]["probe_error"]
    assert isinstance(payload["embedding"]["latency_ms"], (int, float))
