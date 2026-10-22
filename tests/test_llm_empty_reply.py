"""LLM 空答复 / 推理预算 的回归测试（纯离线，不打真实模型）。

背景（用户实测留证 artifacts/acceptance/llm-resume-20261015.json）：
AS711 的 Qwen3.6 在 max_tokens=128 时返回 HTTP 200，但 content=""、tool_calls=0。
根因是推理模型的思考过程先吃掉预算（我复测：默认预算下回答 4 个字用掉 239
completion_tokens）。当时的代码把这种响应当"成功"，会写出**空文档**、留下**空回答**。

所以这里锁死四条性质：
1. 空 content + 无 tool_calls → 抛 LLMError（不能静默成功）；
2. finish_reason=length 时错误信息要点明是预算问题，并带上 usage；
3. reasoning_content **绝不**被当成答复内容；
4. 只有工具调用、没有正文 → 仍然算成功（这类响应是合法的）。
"""

from __future__ import annotations

import pytest

from apps.planner.llm import LLMError, LlmClient, LlmReply, default_profile, document_profile


def make_client(payload: dict) -> LlmClient:
    """构造一个只返回固定响应的客户端（不发网络请求）。"""

    class _FakeTransport:
        def post(self, *_args, **_kwargs):
            class _Response:
                status_code = 200

                @staticmethod
                def json():
                    return payload

                text = "{}"

                @staticmethod
                def raise_for_status():
                    return None

            return _Response()

    client = LlmClient()
    client._client = _FakeTransport()  # type: ignore[assignment]
    return client


def reply_body(*, content=None, tool_calls=None, finish_reason="stop", usage=None):
    message: dict = {"role": "assistant"}
    if content is not None:
        message["content"] = content
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "model": "Qwen3.6-35B-A3B-FP8",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": usage or {},
    }


# ---------------------------------------------------- 1) 空答复必须失败 ----


def test_empty_content_without_tool_calls_is_an_error():
    client = make_client(reply_body(content="", tool_calls=[]))
    with pytest.raises(LLMError) as excinfo:
        client.chat([{"role": "user", "content": "在吗"}])
    assert "空答复" in str(excinfo.value)


def test_whitespace_only_content_is_also_an_error():
    """只有空白字符也算空——不能因为 len>0 就通过。"""
    client = make_client(reply_body(content="   \n\t  ", tool_calls=[]))
    with pytest.raises(LLMError):
        client.chat([{"role": "user", "content": "在吗"}])


# ------------------------------------------ 2) length 要点明预算问题 ----


def test_length_finish_reason_explains_token_budget():
    client = make_client(
        reply_body(
            content="",
            tool_calls=[],
            finish_reason="length",
            usage={"prompt_tokens": 28, "completion_tokens": 128, "total_tokens": 156},
        )
    )
    with pytest.raises(LLMError) as excinfo:
        client.chat([{"role": "user", "content": "写一份文档"}])
    message = str(excinfo.value)
    assert "finish_reason=length" in message
    assert "KB_LLM_MAX_TOKENS" in message, "应提示如何提高预算"
    assert "completion_tokens=128" in message, "应带上 usage 便于定位"


# --------------------------------- 3) reasoning_content 不能当答复 ----


def test_reasoning_content_is_never_used_as_the_answer():
    """有些推理模型把思考过程放在 reasoning_content 里——那**不是**答复。

    这里不但要保证不被采用，还要保证这种响应被判为**空答复失败**，
    否则用户会在文档里看到模型的草稿。
    """
    body = reply_body(content="", tool_calls=[], finish_reason="stop")
    body["choices"][0]["message"]["reasoning_content"] = "让我想想……用户问的是质保期。"
    client = make_client(body)
    with pytest.raises(LLMError) as excinfo:
        client.chat([{"role": "user", "content": "质保期多久"}])
    assert "让我想想" not in str(excinfo.value), "推理内容泄漏进了答复"


# -------------------------------------- 4) 只有工具调用仍然是成功 ----


def test_tool_call_only_reply_is_valid():
    client = make_client(
        reply_body(
            content="",
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "list_files", "arguments": "{}"},
                }
            ],
            finish_reason="tool_calls",
        )
    )
    reply = client.chat([{"role": "user", "content": "列出资料"}])
    assert reply.has_tool_calls
    assert reply.tool_calls[0].name == "list_files"
    assert not reply.is_empty


# ------------------------------------------------ 预算与 finish_reason ----


def test_budget_defaults_are_large_enough_for_reasoning_models():
    """实测：4 个字的回答用掉 239 completion_tokens，预算不能太小。"""
    assert default_profile().max_tokens >= 4096
    assert document_profile().max_tokens >= default_profile().max_tokens
    assert document_profile().timeout_seconds >= 120


def test_finish_reason_and_usage_are_exposed_on_success():
    client = make_client(
        reply_body(
            content="连接正常",
            finish_reason="stop",
            usage={"prompt_tokens": 28, "completion_tokens": 239, "total_tokens": 267},
        )
    )
    reply = client.chat([{"role": "user", "content": "在吗"}])
    assert isinstance(reply, LlmReply)
    assert reply.finish_reason == "stop"
    assert reply.usage["completion_tokens"] == 239
    assert reply.content == "连接正常"
