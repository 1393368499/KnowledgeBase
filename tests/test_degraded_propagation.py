"""范围说明必须**真的送达模型**，degraded 必须在 API 与历史消息里保留。"""

from __future__ import annotations

import uuid

import pytest

from apps.knowledge.models import DocumentChunk, IngestedFile, KnowledgeLibrary, LibraryMember
from apps.planner import services
from apps.planner.llm import LlmReply

pytestmark = pytest.mark.django_db


@pytest.fixture
def setup_env():
    from django.contrib.auth import get_user_model

    from apps.planner.models import Conversation

    user = get_user_model().objects.create_user(
        username=f"deg-{uuid.uuid4().hex[:8]}", password="x", is_staff=True
    )
    library = KnowledgeLibrary.objects.create(code=f"d-{uuid.uuid4().hex[:6]}", name="库")
    LibraryMember.objects.create(library=library, user=user, role="admin")
    record = IngestedFile._base_manager.create(
        library=library,
        original_name="质保期.txt",
        content_sha256=uuid.uuid4().hex,
        size_bytes=10,
        file_type="txt",
        parse_status="done",
        chunk_count=1,
    )
    DocumentChunk.objects.create(
        file=record, ordinal=0, text="质保期 36 个月。", page=1,
        token_estimate=8, permission_code=f"library:{library.pk}",
    )
    return user, Conversation.objects.create(user=user, title="会话")


def test_scope_note_is_delivered_and_degraded_is_retained(monkeypatch, setup_env, settings):
    """有上下文**且**降级时：模型必须收到范围说明；API/历史消息都要保留 degraded。

    这条回归针对两个真实缺陷：
    - `if context:` 曾用 `system_content = SYSTEM_PROMPT + …` 重新赋值，
      把范围说明整段覆盖掉；
    - 只在前端插一条 DOM 提示，重渲染就丢；必须写进消息 payload。
    """
    user, conversation = setup_env
    from apps.planner.rag.contracts import Evidence
    evidence = Evidence.from_chunk(DocumentChunk.objects.select_related("file__library").first()).to_dict()
    settings.KB_RAG_VERIFY_SEMANTIC = False

    monkeypatch.setattr(
        services,
        "build_context_report",
        lambda *a, **k: {
            "context": "[质保期.txt] 质保期 36 个月。",
            "evidence": [evidence],
            "degraded": True,
            "degraded_reason": "部分信息库的向量检索不可用；结果可能不完整",
            "failed_collections": [{"collection": "kb-9-g1", "reason": "OutputTooSmall"}],
            "collections": ["kb-1-g1", "kb-9-g1"],
        },
    )

    seen: dict = {}

    class FakeClient:
        def chat(self, messages, tools=None):
            seen["messages"] = messages
            return LlmReply(content=f"质保期为 36 个月。[{evidence['evidence_id']}]", model="fake", finish_reason="stop")

    payload = services.reply_to_message(user, conversation, "质保期多久", client=FakeClient())

    system = seen["messages"][0]["content"]
    assert seen["messages"][0]["role"] == "system"
    # 范围说明**和**引用资料都要在（覆盖 bug 就是只剩后者）
    assert "检索范围说明" in system, "范围说明被覆盖掉了"
    assert "不要声称已完整检索" in system
    assert "质保期 36 个月" in system, "引用资料丢了"

    # API 返回体保留 degraded
    assert payload["retrieval_degraded"] is True
    assert "不可用" in payload["retrieval_degraded_reason"]
    assert payload["failed_collections"]

    # **历史消息**里也保留（刷新/翻历史仍在）
    from apps.planner.models import Message

    assistant = Message.objects.get(pk=payload["assistant_message_id"])
    assert assistant.payload["retrieval_degraded"] is True


def test_healthy_retrieval_has_no_scope_note(monkeypatch, setup_env):
    """没有降级时不应出现范围说明（避免误导成"没搜全"）。"""
    user, conversation = setup_env
    monkeypatch.setattr(
        services,
        "build_context_report",
        lambda *a, **k: {
            "context": "片段", "degraded": False, "degraded_reason": "",
            "failed_collections": [], "collections": [],
        },
    )
    seen: dict = {}

    class FakeClient:
        def chat(self, messages, tools=None):
            seen["messages"] = messages
            return LlmReply(content="好的", model="fake", finish_reason="stop")

    payload = services.reply_to_message(user, conversation, "在吗", client=FakeClient())
    assert "检索范围说明" not in seen["messages"][0]["content"]
    assert payload["retrieval_degraded"] is False
