"""F-05：失败响应不得把内部细节回传给客户端（2026-11-11 复审）。

对外只允许稳定错误码/类别 + 可执行提示；异常原文（上游端点、响应体片段、
本地路径）只能进日志。
"""

from __future__ import annotations

import pytest

from apps.planner.views import _llm_unavailable_response
from apps.voice.services import _public_voice_error

pytestmark = pytest.mark.django_db


def test_llm_unavailable_response_hides_upstream_detail():
    exc = RuntimeError(
        "connect to http://10.9.8.7:8000/v1 failed: response body=SECRET-TRACE"
    )
    response = _llm_unavailable_response(exc)
    assert response.status_code == 503
    assert response.data is not None
    assert response.data["code"] == "assistant_unavailable"
    text = str(response.data)
    assert "10.9.8.7" not in text
    assert "SECRET-TRACE" not in text
    assert response.data["reason"] in {
        "upstream_unavailable",
        "upstream_timeout",
        "upstream_rate_limited",
    }
    # 仍然给用户可执行的下一步
    assert "文件管理" in response.data["hint"]


def test_llm_unavailable_response_marks_timeout():
    response = _llm_unavailable_response(TimeoutError("timed out after 60s"))
    assert response.data is not None
    assert response.data["reason"] == "upstream_timeout"


@pytest.mark.parametrize(
    ("stage", "exc", "needle"),
    [
        ("answer", TimeoutError("timed out after 60s"), "超时"),
        ("answer", RuntimeError("boom"), "AI 处理未完成"),
        ("render", RuntimeError("boom"), "显示"),
        ("transcribe", RuntimeError("boom"), "录音"),
    ],
)
def test_voice_public_error_is_stable_and_hides_detail(stage: str, exc: Exception, needle: str):
    message = _public_voice_error(stage, exc)
    assert needle in message
    assert "secret" not in message
    assert "10.9.8.7" not in message
    assert len(message) <= 500


def test_voice_public_error_never_leaks_exception_text():
    message = _public_voice_error("answer", RuntimeError(r"C:\secret\path 10.9.8.7 boom"))
    assert "secret" not in message
    assert "10.9.8.7" not in message
