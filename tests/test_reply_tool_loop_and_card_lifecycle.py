"""回答用正文、卡片只用于待确认：多轮工具回路 + 卡片生命周期。

2026-10-19 用户要求（原话）：
- "回答不要用卡片，卡片只用在确认操作上，确认完毕后就消失。"

对应两条实现约束：
1. 只读意图（``search_knowledge``）提案即执行后，检索结果要**回填给模型**，
   让它用一段正常正文回答，而不是把结果留在卡片里；
2. 会话接口只返回**还没结束**的操作（待确认/待执行），执行完/放弃/过期的
   卡片不再出现在会话里（审计记录仍在「我的操作记录」）。
"""

from __future__ import annotations

import json

import httpx
import pytest
from django.urls import reverse

from apps.planner import services
from apps.planner.llm import LlmClient, LlmProfile
from apps.planner.models import Conversation, Message, OperationRecord

pytestmark = pytest.mark.django_db


# ------------------------------------------------------------------ 工具 ----


def _fake_llm(handler) -> LlmClient:
    """走真实 HTTP 代码路径的假模型（MockTransport）。"""
    return LlmClient(
        LlmProfile(base_url="http://llm.test/v1", model="test-model"),
        client=httpx.Client(transport=httpx.MockTransport(handler), trust_env=False),
    )


def _call_payload(name: str, arguments: dict, call_id: str = "call_1") -> dict:
    return {
        "message": {
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
        }
    }


def _text_payload(text: str) -> dict:
    return {"message": {"content": text}}


def _response(choice: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={"model": "test-model", "choices": [choice], "usage": {"total_tokens": 12}},
    )


def _conversation(user) -> Conversation:
    return Conversation.objects.create(user=user)


@pytest.fixture()
def search_calls(monkeypatch):
    """假检索：返回两条结构完整的命中（避免依赖 qdrant）。"""
    calls: list[dict] = []

    def fake_search(**kwargs):
        calls.append(kwargs)
        return {
            "results": [
                {
                    "file_id": 1,
                    "original_name": "出差申请.pdf",
                    "library_code": "contract-test",
                    "page": 1,
                    "text": "陶鑫宇于 2026 年 10 月 14 日前往陵水出差，同行孙海涛。",
                },
                {
                    "file_id": 2,
                    "original_name": "行程单.pdf",
                    "library_code": "contract-test",
                    "page": 2,
                    "text": "10 月 14 日 海口东 - 陵水。",
                },
            ],
            "report": {},
        }

    monkeypatch.setattr(services, "knowledge_search", fake_search)
    return calls


# ------------------------------------------------- 1. 检索结果回填 → 正文 ----


def test_search_result_is_fed_back_and_answer_is_plain_text(
    user, library, grant_member, search_calls
):
    grant_member(library, user)
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload)
        if len(seen) == 1:
            return _response(_call_payload("search_knowledge", {"query": "谁出差了"}))
        return _response(_text_payload("根据资料，出差的是陶鑫宇，同行孙海涛。"))

    conversation = _conversation(user)
    payload = services.reply_to_message(
        user, conversation, "谁出差了？", client=_fake_llm(handler)
    )

    # 模型被问了两次：第一次发起检索，第二次读到结果后写正文
    assert len(seen) == 2
    second = seen[1]["messages"]
    tool_messages = [item for item in second if item.get("role") == "tool"]
    assert tool_messages, "第二轮必须把检索结果作为 tool 消息喂回模型"
    assert "没有可核对的原文证据" in tool_messages[0]["content"]
    assert "出差申请.pdf" in tool_messages[0]["content"]
    assistant_turns = [
        item for item in second
        if item.get("role") == "assistant" and item.get("tool_calls")
    ]
    assert assistant_turns, "工具调用回合必须以 assistant+tool_calls 形式回放"
    assert assistant_turns[0]["tool_calls"][0]["function"]["name"] == "search_knowledge"

    # 用户看到的是正文，不是"这一轮没有正文回答"的占位
    assert payload["content"] == "根据资料，出差的是陶鑫宇，同行孙海涛。"
    assert payload["proposals"][0]["status"] == OperationRecord.Status.EXECUTED
    message = Message.objects.get(pk=payload["assistant_message_id"])
    assert message.content.startswith("根据资料")
    # 会话里没有待确认的操作（检索已免确认执行完）
    assert not OperationRecord.objects.filter(
        conversation=conversation,
        status__in=[OperationRecord.Status.PROPOSED, OperationRecord.Status.ARMED],
    ).exists()


def test_loop_is_capped_and_does_not_spin_forever(user, library, grant_member, search_calls):
    """模型反复检索时要停下来：最多 KB_LLM_MAX_TOOL_ROUNDS 轮；
    之后还要**关掉工具**再问一次，逼它写正文（否则用户只看到一句"没有正文"）。"""
    from django.conf import settings

    grant_member(library, user)
    rounds = int(getattr(settings, "KB_LLM_MAX_TOOL_ROUNDS", 3))
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload)
        if len(seen) <= rounds:
            return _response(_call_payload("search_knowledge", {"query": "谁出差了"}))
        return _response(_text_payload("根据资料，出差的是陶鑫宇。"))

    conversation = _conversation(user)
    payload = services.reply_to_message(
        user, conversation, "谁出差了？", client=_fake_llm(handler)
    )

    assert len(seen) == rounds + 1
    assert "tools" not in seen[-1], "最后一轮必须关掉工具，避免模型再调工具"
    # 背景检索 1 次（回复前的上下文检索）+ 每轮工具调用各 1 次
    assert len(search_calls) == 2, "背景检索一次，相同工具查询只执行一次，其余轮次回填重复提示"
    assert payload["content"].startswith("根据资料，出差的是陶鑫宇。")
    assert payload["research"]["stop_reason"] == "round_limit"


def test_delete_operation_still_stops_at_pending_card(admin_user, library, make_file):
    """Only deletion still waits for confirmation under the current policy."""
    record = make_file(library=library, name="要归档.md")
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _response(_call_payload("delete_file", {"file_id": record.pk}))

    conversation = _conversation(admin_user)
    payload = services.reply_to_message(
        admin_user, conversation, "删除这份资料", client=_fake_llm(handler)
    )

    assert len(seen) == 1, "删除尚未执行，没有可回填的结果"
    assert payload["proposals"][0]["status"] == OperationRecord.Status.PROPOSED
    assert payload["proposals"][0]["confirm_token"]


def test_chat_without_tools_is_single_round(user):
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _response(_text_payload("你好，有什么可以帮你？"))

    conversation = _conversation(user)
    payload = services.reply_to_message(
        user, conversation, "你好", client=_fake_llm(handler)
    )

    assert len(seen) == 1
    assert payload["content"] == "你好，有什么可以帮你？"
    assert payload["proposals"] == []


# ------------------------------------- 2. 卡片只留"待确认"，执行完就消失 ----


def test_conversation_hides_finished_cards_but_keeps_pending(
    admin_user, library, search_calls, make_file, api
):
    record = make_file(library=library, name="要归档.md")
    conversation = _conversation(admin_user)

    # 检索：提案即执行 → 卡片不该出现在会话里
    executed = services.propose_operation(
        admin_user, intent="search_knowledge", params={"query": "出差"},
        conversation=conversation,
    )
    assert executed["status"] == OperationRecord.Status.EXECUTED
    # 删除：待确认 → 卡片必须出现
    pending = services.propose_operation(
        admin_user, intent="delete_file", params={"file_id": record.pk},
        conversation=conversation,
    )
    assert pending["status"] == OperationRecord.Status.PROPOSED

    api.force_authenticate(user=admin_user)
    response = api.get(
        reverse("planner:conversation", args=[conversation.pk])
    )
    assert response.status_code == 200, response.content[:300]
    ids = [row["id"] for row in response.json()["operations"]]
    assert pending["operation_id"] in ids
    assert executed["operation_id"] not in ids


def test_executed_card_disappears_after_execution(admin_user, library, make_file, api):
    record = make_file(library=library, name="要归档.md")
    conversation = _conversation(admin_user)
    proposal = services.propose_operation(
        admin_user, intent="classify_file", params={"file_id": record.pk, "classification": "合同"},
        conversation=conversation,
    )
    operation = OperationRecord.objects.get(pk=proposal["operation_id"])
    assert operation.status == OperationRecord.Status.EXECUTED
    assert not proposal.get("confirm_token")

    api.force_authenticate(user=admin_user)
    response = api.get(reverse("planner:conversation", args=[conversation.pk]))
    assert response.status_code == 200
    assert response.json()["operations"] == []
    # 审计记录还在（"卡片消失"不等于"历史消失"）
    assert OperationRecord.objects.filter(pk=operation.pk).exists()


def test_abandoned_and_expired_cards_are_hidden(admin_user, library, make_file, api):
    record = make_file(library=library, name="要归档.md")
    conversation = _conversation(admin_user)
    proposal = services.propose_operation(
        admin_user, intent="delete_file", params={"file_id": record.pk},
        conversation=conversation,
    )
    operation = OperationRecord.objects.get(pk=proposal["operation_id"])
    services.abandon_operation(admin_user, operation)

    api.force_authenticate(user=admin_user)
    response = api.get(reverse("planner:conversation", args=[conversation.pk]))
    assert response.json()["operations"] == []


def test_tool_result_content_is_readable(user, library, grant_member, search_calls):
    """给模型看的工具结果要含文件名/页/正文，否则模型只能瞎猜。"""
    grant_member(library, user)
    proposal = services.propose_operation(
        user, intent="search_knowledge", params={"query": "出差"},
        conversation=_conversation(user),
    )
    content = services._tool_result_content(proposal)
    assert "命中条数：2" in content
    assert "出差申请.pdf" in content
    assert "第 1 页" in content
    assert "陶鑫宇" in content


def test_tool_round_budget_is_a_real_setting():
    """`getattr(settings, ...)` 兜底不算数：变量必须真的定义，否则环境变量不生效。"""
    from django.conf import settings

    assert isinstance(settings.KB_LLM_MAX_TOOL_ROUNDS, int)
    assert settings.KB_LLM_MAX_TOOL_ROUNDS >= 1


@pytest.mark.parametrize("budget", [1, 2])
def test_round_budget_override_keeps_the_forced_answer(
    user, library, grant_member, search_calls, budget
):
    """调小轮数预算：检索照样免确认执行，最终仍有关掉工具换来的正文。"""
    from django.test import override_settings

    grant_member(library, user)
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload)
        if "tools" in payload:
            return _response(_call_payload("search_knowledge", {"query": "谁出差了"}))
        return _response(_text_payload("出差的是陶鑫宇。"))

    conversation = _conversation(user)
    with override_settings(KB_LLM_MAX_TOOL_ROUNDS=budget):
        payload = services.reply_to_message(
            user, conversation, "谁出差了？", client=_fake_llm(handler)
        )

    assert len(seen) == budget + 1
    assert payload["content"].startswith("出差的是陶鑫宇。")
    assert payload["research"]["stop_reason"] == "round_limit"
    assert len(search_calls) == 2
