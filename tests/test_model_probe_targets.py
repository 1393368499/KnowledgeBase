"""系统配置页"测试连通"必须按被点击目标探测（真实页面验收缺陷）。

缺陷：4 行按钮都调 `?probe=1` 且只探 embedding profiles → 点重排/OCR/LLM 实际也只测 BGE，
目标行仍"未检测"；点 BGE 还会把**未配置**的 2048 空 model 一起探、报一串失败。

**契约更新（本次）**：重排 / OCR / LLM 三个目标已改成**真实客户端调用**探测
（不再用 `GET /models` 冒充推理能力），所以三者的 `connectivity_only` 一律是
``False`` —— 它们证明的是"真的完成了一次最小调用"，而不是"端口后面有 HTTP 服务"。

这里的用例统一把**客户端类**换成替代类（`RerankClient` / `OcrClient` / `LlmClient`），
让请求走完视图里的真实调用与解析路径，同时**绝不外呼 AS711**。
"""

from __future__ import annotations

import pytest

URL = "/api/v1/knowledge/models/"


def test_unknown_target_is_rejected(db, auth_api, admin_user):
    client = auth_api(admin_user)
    resp = client.get(URL + "?probe=1&target=evil.example.com")
    assert resp.status_code == 400, resp.content[:200]
    assert "不支持的目标" in resp.json()["detail"]


def test_default_probe_only_touches_active_embedding_profile(db, auth_api, admin_user, monkeypatch):
    """未指定目标时（兼容旧行为）**只探当前生效的 embedding profile**，
    不得把未配置的空 profile 也探了。"""
    from apps.knowledge import embedding

    calls = []

    def fake_ready(profile, **kwargs):
        calls.append(profile.model)
        return {"ready": True}

    monkeypatch.setattr(embedding, "assert_profile_ready", fake_ready)
    client = auth_api(admin_user)
    resp = client.get(URL + "?probe=1")
    assert resp.status_code == 200, resp.content[:200]
    assert len(calls) == 1, f"应只探 1 个 profile，实际 {calls}"
    body = resp.json()
    probed = [p for p in body["profiles"] if "probe_latency_ms" in p]
    assert len(probed) == 1 and probed[0]["active"] is True, probed


# ------------------------------- 目标级探测：替代客户端 + 真实调用路径 ----


@pytest.fixture
def service_profiles(settings):
    """显式给出 rerank / ocr / llm 端点。

    为什么必须显式给：视图在端点为空的**分支之前**就返回 `probe_error="未配置端点"`，
    若本机 ``.env`` 没配 ``AS711_*``，用例就会因为环境而不是代码失败。
    只覆盖这三个键，embedding 等保持原样（`profiles()` 仍要遍历它们）。
    """
    services = dict(getattr(settings, "MODEL_SERVICES", {}) or {})
    services["rerank"] = {"base_url": "http://rerank.test/v1", "model": "bge-reranker-test"}
    services["ocr"] = {"base_url": "http://ocr.test/v1", "model": "paddleocr-test"}
    services["llm"] = {"base_url": "http://llm.test/v1", "model": "test-llm"}
    settings.MODEL_SERVICES = services
    return settings


@pytest.fixture
def fake_rerank(monkeypatch):
    """替代 `RerankClient`：记录调用，按脚本返回真实 `RerankHit` 或抛异常。"""
    from apps.knowledge import rerank as rerank_module
    from apps.knowledge.rerank import RerankHit

    class FakeRerankClient:
        calls: list = []
        error: Exception | None = None
        hits = [RerankHit(index=0, score=0.93), RerankHit(index=1, score=0.02)]

        def __init__(self, profile=None, **kwargs):  # noqa: ARG002 - 与真实签名一致
            self.profile = profile

        def rerank(self, query, documents, *, top_n=None):
            FakeRerankClient.calls.append(
                {"query": query, "documents": list(documents), "top_n": top_n}
            )
            if FakeRerankClient.error is not None:
                raise FakeRerankClient.error
            return FakeRerankClient.hits

    monkeypatch.setattr(rerank_module, "RerankClient", FakeRerankClient)
    return FakeRerankClient


@pytest.fixture
def fake_ocr(monkeypatch):
    """替代 `OcrClient`：记录送进去的样图字节，按脚本返回识别文本或抛异常。"""
    from apps.knowledge import ocr as ocr_module

    class FakeOcrClient:
        calls: list = []
        error: Exception | None = None
        text = (
            "质保期为验收合格之日起 12 个月。\n"
            "合同编号 HT-2026-0417，甲方：华东石化。\n"
            "Warranty period: 12 months after acceptance."
        )

        def __init__(self, profile=None, **kwargs):  # noqa: ARG002 - 与真实签名一致
            self.profile = profile

        def recognize_image(self, image_bytes, *, hint=""):
            FakeOcrClient.calls.append({"image_bytes": image_bytes, "hint": hint})
            if FakeOcrClient.error is not None:
                raise FakeOcrClient.error
            return FakeOcrClient.text

    monkeypatch.setattr(ocr_module, "OcrClient", FakeOcrClient)
    return FakeOcrClient


@pytest.fixture
def fake_llm(monkeypatch):
    """替代 `LlmClient`：记录 profile / messages，按脚本返回 `LlmReply` 或抛异常。"""
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


def test_rerank_target_runs_real_rerank_call(
    db, auth_api, admin_user, service_profiles, fake_rerank
):
    """点重排 → 用真实 `RerankClient` 跑一次**最小重排**（2 条极短文本）。

    新口径：这是真实调用，`connectivity_only` 必须是 ``False``；
    不再有"只证明连通"的说法，也不再有 `GET /models` 那条旁路。
    """
    client = auth_api(admin_user)
    resp = client.get(URL + "?probe=1&target=rerank")
    assert resp.status_code == 200, resp.content[:200]
    rerank = resp.json()["rerank"]

    assert rerank["probe_target"] == "rerank"
    assert rerank["connectivity_only"] is False, "重排已是真实调用，不该再标成只证明连通"
    assert rerank["connected"] is True
    assert "probe_error" not in rerank, rerank
    assert rerank["top_index"] == 0
    assert rerank["top_score"] == pytest.approx(0.93)
    assert rerank["probe_latency_ms"] >= 0

    # 确认真的是"查询 + 2 条极短文本 + top_n"的最小重排，而不是只探端口
    assert fake_rerank.calls, "没有经过 RerankClient 调用路径"
    call = fake_rerank.calls[0]
    assert call["query"] == "质保期"
    assert call["documents"] == ["质保期 12 个月", "与之无关的一段文字"]
    assert call["top_n"] == 2


def test_rerank_target_reports_real_call_failure(
    db, auth_api, admin_user, service_profiles, fake_rerank
):
    """重排真实调用失败 → 如实报错（异常类名 + 原因），**不谎报连通**，仍给耗时。"""
    from apps.knowledge.rerank import RerankError

    fake_rerank.error = RerankError("重排请求失败：connection refused")
    client = auth_api(admin_user)
    rerank = client.get(URL + "?probe=1&target=rerank").json()["rerank"]

    assert rerank.get("connected") is not True, rerank
    assert rerank["probe_error"].startswith("RerankError: "), rerank
    assert "connection refused" in rerank["probe_error"], rerank
    assert rerank["probe_latency_ms"] >= 0


def test_ocr_target_runs_real_ocr_call(db, auth_api, admin_user, service_profiles, fake_ocr):
    """点 OCR → 把仓库里**固定的自检样图**交给真实 `OcrClient`，判据是识别出关键词。"""
    from apps.knowledge import ocr_selfcheck

    client = auth_api(admin_user)
    resp = client.get(URL + "?probe=1&target=ocr")
    assert resp.status_code == 200, resp.content[:200]
    ocr = resp.json()["ocr"]

    assert ocr["probe_target"] == "ocr"
    assert ocr["connectivity_only"] is False, "OCR 已是真实调用，不该再标成只证明连通"
    assert ocr["connected"] is True
    assert ocr["missing_tokens"] == []
    assert set(ocr["matched_tokens"]) == set(ocr_selfcheck.EXPECTED_NEEDLES)
    assert "HT-2026-0417" in ocr["sample_text"]
    assert "probe_error" not in ocr, ocr
    assert ocr["probe_latency_ms"] >= 0

    # 送进去的必须是仓库里的固定样图（不是现场渲染的、也不是空的）
    assert fake_ocr.calls, "没有经过 OcrClient 调用路径"
    call = fake_ocr.calls[0]
    assert call["hint"] == "自检"
    assert call["image_bytes"] == ocr_selfcheck.sample_bytes()
    assert call["image_bytes"][:8] == b"\x89PNG\r\n\x1a\n"


def test_ocr_target_reports_missing_needles_for_unrelated_text(
    db, auth_api, admin_user, service_profiles, fake_ocr
):
    """负例：OCR 有响应但**没识别出关键词** → 不算连通，并逐条说明缺了哪些词。"""
    from apps.knowledge import ocr_selfcheck

    fake_ocr.text = "这是一段与自检样图无关的文字"
    client = auth_api(admin_user)
    ocr = client.get(URL + "?probe=1&target=ocr").json()["ocr"]

    assert ocr.get("connected") is not True, "没识别出样图关键词就不能算连通"
    assert ocr["missing_tokens"], ocr
    assert set(ocr["missing_tokens"]) == set(ocr_selfcheck.EXPECTED_NEEDLES), ocr
    assert ocr["matched_tokens"] == []
    assert "关键词" in ocr["probe_error"], ocr
    for needle in ocr["missing_tokens"]:
        assert needle in ocr["probe_error"], f"缺词未如实报出：{needle}"
    assert ocr["probe_latency_ms"] >= 0


def test_llm_target_runs_real_llm_call(db, auth_api, admin_user, service_profiles, fake_llm):
    """LLM 目标走真实 `LlmClient` 做一次最小 `chat`；仍只证明**连通**，不冒充能力已验证。

    这条原先断言"LLM 尚未接入（implemented=False）"，后又断言
    `connectivity_only=True`（只 GET /models 的口径）。现在契约是**真实最小调用**，
    所以断言 `connectivity_only is False`，并保留原意：不编造延迟、如实回传结果。
    """
    from apps.planner.llm import LlmReply

    fake_llm.reply = LlmReply(
        content="pong", finish_reason="stop", usage={"completion_tokens": 3}
    )
    client = auth_api(admin_user)
    resp = client.get(URL + "?probe=1&target=llm")
    assert resp.status_code == 200, resp.content[:200]
    llm = resp.json()["llm"]

    assert llm["implemented"] is True, "LLM 目标未接入"
    assert llm["connectivity_only"] is False, "已经是真实调用了，不该再标成只证明连通"
    assert llm["connected"] is True
    assert "probe_error" not in llm, llm
    assert llm["finish_reason"] == "stop"
    assert llm["content_empty"] is False
    assert llm["probe_latency_ms"] is not None, "已接入就必须给实测延迟"
    assert "不代表回答质量" in llm["note"]

    # 真的走了一次 LlmClient.chat，而且是**极小预算**的探测（不是生产预算）
    assert fake_llm.instances, "没有经过 LlmClient 调用路径"
    probe = fake_llm.instances[0]
    assert probe.messages == [{"role": "user", "content": "ping"}]
    assert probe.profile.max_tokens == 64
    assert probe.profile.temperature == 0.0
