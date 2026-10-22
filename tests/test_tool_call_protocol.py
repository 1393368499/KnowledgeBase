"""独立审查条目 3 的回归：工具调用协议的畸形输入必须被**明确拒绝**。

反例（修复前的真实行为）：非法 JSON 的 arguments 被吞成 `{}`；
对 `list_files`（参数全可选）这类工具，`{}` 恰好合法 →
**畸形输入会变成有效提案**，只是"偶然"没出事。

这里逐项覆盖：
  tool_calls 非数组 / 项非对象 / function 非对象 / 缺函数名 /
  非法 JSON / JSON 但非对象 / 参数类型不支持 / 无参数仍合法。
"""

from __future__ import annotations

import pytest

from apps.planner.llm import LLMError, LlmClient


def parse(payload: dict):
    """用真实 _parse_reply 解析（不发网络请求）。"""
    client = LlmClient()
    return client._parse_reply(payload)


def body(message: dict, finish_reason: str = "tool_calls") -> dict:
    return {
        "model": "test",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {},
    }


def call(arguments, name="list_files", cid="c1"):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": arguments}}


# ------------------------------------------------------- 明确拒绝的形态 ----


def test_invalid_json_arguments_is_rejected_not_swallowed():
    """非法 JSON 必须标记为无效，且 arguments **不得**被当成 {}。"""
    reply = parse(body({"role": "assistant", "content": "", "tool_calls": [call("{not json")]}))
    tc = reply.tool_calls[0]
    assert not tc.is_valid
    assert "JSON" in tc.invalid_reason
    assert tc.arguments == {}, "无效调用不应带出可执行参数"
    # 关键：即便 list_files 的参数全可选，也不能算有效
    assert reply.has_tool_calls, "调用本身要被记录（便于告知用户）"


def test_json_but_not_object_is_rejected():
    reply = parse(body({"role": "assistant", "content": "", "tool_calls": [call('"just a string"')]}))
    assert not reply.tool_calls[0].is_valid
    assert "对象" in reply.tool_calls[0].invalid_reason


def test_missing_function_name_is_rejected():
    reply = parse(body({"role": "assistant", "content": "",
                        "tool_calls": [{"id": "x", "function": {"arguments": "{}"}}]}))
    tc = reply.tool_calls[0]
    assert not tc.is_valid
    assert "函数名" in tc.invalid_reason


def test_non_dict_function_is_rejected():
    reply = parse(body({"role": "assistant", "content": "",
                        "tool_calls": [{"id": "x", "function": "list_files"}]}))
    assert not reply.tool_calls[0].is_valid


def test_non_object_tool_call_item_is_rejected():
    reply = parse(body({"role": "assistant", "content": "", "tool_calls": ["list_files"]}))
    assert not reply.tool_calls[0].is_valid


def test_tool_calls_not_a_list_is_a_protocol_error():
    with pytest.raises(LLMError) as excinfo:
        parse(body({"role": "assistant", "content": "", "tool_calls": {"a": 1}}))
    assert "tool_calls" in str(excinfo.value)


def test_unsupported_argument_type_is_rejected():
    reply = parse(body({"role": "assistant", "content": "", "tool_calls": [call([1, 2, 3])]}))
    assert not reply.tool_calls[0].is_valid


# ------------------------------------------------------------ 合法形态 ----


def test_valid_json_object_is_accepted():
    reply = parse(body({"role": "assistant", "content": "",
                        "tool_calls": [call('{"status": "done"}')]}))
    tc = reply.tool_calls[0]
    assert tc.is_valid
    assert tc.arguments == {"status": "done"}


def test_missing_arguments_is_still_valid():
    """真正的"无参数"是合法的（例如 get_stats），不能与畸形输入混为一谈。"""
    reply = parse(body({"role": "assistant", "content": "",
                        "tool_calls": [call(None, name="get_stats")]}))
    tc = reply.tool_calls[0]
    assert tc.is_valid
    assert tc.arguments == {}


def test_dict_arguments_object_is_accepted():
    reply = parse(body({"role": "assistant", "content": "",
                        "tool_calls": [call({"status": "done"})]}))
    assert reply.tool_calls[0].is_valid
    assert reply.tool_calls[0].arguments == {"status": "done"}
