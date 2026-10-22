"""rerank / ocr 的真实外呼代码路径（用 MockTransport，不打真实网络）。

为什么要单独测：这两个客户端的 HTTP 分支此前**完全没有测试覆盖**，
结果里面藏着一个真实缺陷——它们引用了 ``self.profile.name``，而
``RerankProfile`` / ``OcrProfile`` 都没有 ``name`` 字段，真实调用会抛
``AttributeError``。异常又被上层兜住（"重排失败，回退融合结果"），
所以在界面上只表现为"重排没生效"，很难发现。

这里用一个假的 httpx transport 走完整分支：组包 → 发请求 → 解析 → 熔断记录。
离线守卫（conftest 的 ``_no_unmocked_external_calls``）会挡住漏注入的用例。
"""

from __future__ import annotations

import json

import httpx
import pytest

from apps.knowledge.ocr import OcrClient, OcrProfile
from apps.knowledge.rerank import RerankClient, RerankProfile
from apps.knowledge.resilience import BREAKERS, ServiceUnavailable

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clean_breakers():
    BREAKERS.clear()
    yield
    BREAKERS.clear()


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


# ------------------------------------------------------------------ 重排 ----


def test_rerank_real_path_parses_scores():
    """真实分支：POST /rerank → 解析 scores（这里曾经因 profile.name 直接炸）。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 1, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.2},
                ]
            },
        )

    profile = RerankProfile(base_url="http://as711.test:8002/v1", model="bge-reranker-large")
    client = RerankClient(profile, client=_client(handler))
    hits = client.rerank(query="质保期", documents=["甲", "乙"])

    assert captured["url"].endswith("/rerank")
    assert captured["body"]["model"] == "bge-reranker-large"
    assert [hit.index for hit in hits] == [1, 0]  # 按分数降序
    assert hits[0].score == pytest.approx(0.9)
    assert client.available is True  # 成功过一次，才算"已被证明可用"


def test_rerank_service_name_is_stable_and_has_no_profile_name():
    """熔断器标识不能依赖不存在的 ``profile.name``。"""
    profile = RerankProfile(base_url="http://as711.test:8002/v1", model="m")
    client = RerankClient(profile)
    assert client.service_name == "rerank:http://as711.test:8002/v1|m"
    assert not hasattr(profile, "name")


def test_rerank_server_error_becomes_rerank_error_and_opens_breaker():
    """5xx 会被重试到用尽，然后包装成明确的重排失败（不是裸 httpx 异常）。"""
    from apps.knowledge.rerank import RerankError

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, text="boom")

    profile = RerankProfile(base_url="http://as711.test:8002/v1", model="m")
    client = RerankClient(profile, client=_client(handler))
    with pytest.raises(RerankError):
        client.rerank(query="q", documents=["a"])
    assert calls["n"] == 2  # KB_RERANK_RETRIES 默认 1 → 总尝试 2 次
    assert client.last_error


def test_rerank_open_breaker_short_circuits_without_http_call():
    """熔断打开后：直接在本地快速失败，不再打网络。"""
    from apps.knowledge.rerank import RerankError

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, text="boom")

    profile = RerankProfile(base_url="http://as711.test:8002/v1", model="m")
    client = RerankClient(profile, client=_client(handler))
    for _ in range(2):  # 打到阈值（默认 3：两次调用共 4 次尝试）
        with pytest.raises(RerankError):
            client.rerank(query="q", documents=["a"])

    before = calls["n"]
    with pytest.raises(RerankError) as excinfo:
        client.rerank(query="q", documents=["a"])
    assert calls["n"] == before  # 一次网络都没发
    assert "不可用" in str(excinfo.value)


# -------------------------------------------------------------------- OCR ----


def test_ocr_real_path_parses_choice_content():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "识别出来的文字"}}]},
        )

    profile = OcrProfile(base_url="http://as711.test:8003/v1", model="PaddleOCR-VL-1.5")
    client = OcrClient(profile, client=_client(handler))
    text = client.recognize_image(b"\x89PNG\r\n\x1a\nfake")

    assert text == "识别出来的文字"
    assert captured["url"].endswith("/chat/completions")
    # 图片以 data URL 形式传过去
    content = captured["body"]["messages"][0]["content"]
    image_part = [part for part in content if part.get("type") == "image_url"]
    assert image_part and image_part[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert client.proven is True


def test_ocr_service_name_is_stable():
    profile = OcrProfile(base_url="http://as711.test:8003/v1", model="m")
    client = OcrClient(profile)
    assert client.service_name == "ocr:http://as711.test:8003/v1|m"
    assert not hasattr(profile, "name")


def test_ocr_malformed_response_becomes_ocr_error():
    from apps.knowledge.ocr import OcrError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    profile = OcrProfile(base_url="http://as711.test:8003/v1", model="m")
    client = OcrClient(profile, client=_client(handler))
    with pytest.raises(OcrError):
        client.recognize_image(b"fake")
    assert client.proven is False  # 没成功过，就不算"已被证明可用"


def test_ocr_unavailable_service_reports_service_unavailable():
    """熔断打开时抛的是 ServiceUnavailable 系（服务不可用），不是"识别出空字符串"。"""
    from apps.knowledge.ocr import OcrError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    profile = OcrProfile(base_url="http://as711.test:8003/v1", model="m")
    client = OcrClient(profile, client=_client(handler))
    errors = []
    for _ in range(3):
        try:
            client.recognize_image(b"fake")
        except OcrError as exc:
            errors.append(str(exc))
    assert errors
    assert any("不可用" in message or "503" in message for message in errors)
    assert isinstance(ServiceUnavailable("x", "y"), RuntimeError)
