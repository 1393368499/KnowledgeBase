"""模型侧失败时，文档与问答接口的行为（离线，不打真实模型）。

审查指出的风险：DerivedDocumentView.post 原先只捕获 ServiceUnavailable，
新加的"空答复 LLMError"会冒成 500，并且如果异常发生在写库之后还会留下
一份空/截断的文档。这里锁死：

- 空答复 → HTTP 503（统一不可用响应），且**文档数为 0**；
- 被截断（finish_reason=length，正文非空）→ 同样 503，且不落文档；
- 消息接口同理，且不留下空回答的助手消息。
"""

from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.knowledge.models import (
    DocumentChunk,
    IngestedFile,
    KnowledgeLibrary,
    LibraryMember,
)
from apps.planner.llm import LLMError
from apps.planner.models import DerivedDocument, Message

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin():
    return get_user_model().objects.create_user(
        username=f"api-{uuid.uuid4().hex[:8]}", password="x", is_staff=True
    )


@pytest.fixture
def library(admin):
    item = KnowledgeLibrary.objects.create(code=f"l-{uuid.uuid4().hex[:6]}", name="库")
    LibraryMember.objects.create(library=item, user=admin, role="admin")
    # 必须真的有一份"可撰写"的资料：否则文档接口会在前置校验（没有可用资料）
    # 就返回 400，压根走不到模型调用，测试也就测不到目标行为。
    record = IngestedFile._base_manager.create(
        library=item,
        original_name="质保期说明.txt",
        content_sha256=uuid.uuid4().hex,
        size_bytes=64,
        file_type="txt",
        parse_status="done",
        chunk_count=1,
    )
    DocumentChunk.objects.create(
        file=record,
        ordinal=0,
        text="本项目质保期为 36 个月，自验收通过之日起计算。",
        page=1,
        token_estimate=20,
        permission_code=f"library:{item.pk}",
    )
    return item


def fail_with(monkeypatch, reason: str):
    """让模型调用抛出与真实客户端一致的 LLMError。"""
    from apps.planner import services

    def boom(*_args, **_kwargs):
        raise LLMError(reason)

    monkeypatch.setattr(services.LlmClient, "chat", boom, raising=False)


def test_empty_reply_returns_503_and_writes_no_document(client, admin, library, monkeypatch):
    client.force_login(admin)
    before = DerivedDocument.objects.count()
    fail_with(monkeypatch, "模型返回了空答复（content 为空且没有工具调用）")

    response = client.post(
        "/api/v1/planner/documents/",
        {"title": "空答复文档", "topic": "质保期"},
        content_type="application/json",
    )
    assert response.status_code == 503, response.content[:300]
    assert DerivedDocument.objects.count() == before, "空答复竟然落了文档"


def test_truncated_reply_returns_503_and_writes_no_document(client, admin, library, monkeypatch):
    """finish_reason=length（正文非空但被截断）同样不能当成功。"""
    client.force_login(admin)
    before = DerivedDocument.objects.count()
    fail_with(monkeypatch, "模型输出被 token 上限截断（finish_reason=length），本次结果不可用")

    response = client.post(
        "/api/v1/planner/documents/",
        {"title": "截断文档", "topic": "质保期"},
        content_type="application/json",
    )
    assert response.status_code == 503
    assert DerivedDocument.objects.count() == before, "截断的结果竟然落了文档"


def test_empty_reply_leaves_no_assistant_message(client, admin, monkeypatch):
    client.force_login(admin)
    fail_with(monkeypatch, "模型返回了空答复（content 为空且没有工具调用）")

    response = client.post(
        "/api/v1/planner/chat/",
        {"message": "在吗", "conversation_id": None},
        content_type="application/json",
    )
    assert response.status_code == 503, response.content[:300]
    empties = [
        row for row in Message.objects.filter(role=Message.Role.ASSISTANT) if not row.content.strip()
    ]
    assert not empties, "留下了空回答的助手消息"


def test_empty_reply_persists_system_note_so_refresh_explains(client, admin, monkeypatch):
    """模型报错（含空答复）必须**留痕**：刷新会话后能看到"助手不可用：…"。

    否则用户刷新后只剩自己那句提问，看不到"为什么这次没有回答"——与审查条目 57
    （被拒提案原因只存在于当次响应里）是同一类问题。
    """
    client.force_login(admin)
    fail_with(monkeypatch, "模型返回了空答复（content 为空且没有工具调用）")

    response = client.post(
        "/api/v1/planner/chat/",
        {"message": "在吗", "conversation_id": None},
        content_type="application/json",
    )
    assert response.status_code == 503, response.content[:300]

    notes = list(Message.objects.filter(role=Message.Role.SYSTEM))
    assert len(notes) == 1, "模型报错没有留下系统提示"
    note = notes[0]
    assert "助手不可用" in note.content
    assert note.payload.get("unavailable") is True
    assert "空答复" in str(note.payload.get("reason"))

    # 会话历史接口把它一并返回 → 刷新后界面能渲染这条系统提示（不再伪装成 AI 气泡）
    history = client.get(f"/api/v1/planner/conversations/{note.conversation_id}/")
    assert history.status_code == 200, history.content[:300]
    roles = [row["role"] for row in history.json()["messages"]]
    assert "system" in roles, roles


def test_chat_success_status_is_201(client, admin, monkeypatch):
    """ChatView 正常返回 201 —— 实测脚本曾只认 200，导致误报失败。"""
    from apps.planner.llm import LlmReply

    from apps.planner import services

    monkeypatch.setattr(
        services.LlmClient,
        "chat",
        lambda self, *a, **k: LlmReply(content="连接正常", model="fake", finish_reason="stop"),
        raising=False,
    )
    client.force_login(admin)
    response = client.post(
        "/api/v1/planner/chat/",
        {"message": "在吗", "conversation_id": None},
        content_type="application/json",
    )
    assert response.status_code == 201, response.content[:300]
    assert response.json()["content"] == "连接正常"
