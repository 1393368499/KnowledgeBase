"""P3–P5 回归用例（**待统一执行**；本文件只写不跑）。

用户要求：开发阶段不跑功能测试，统一测试阶段再执行。本文件覆盖
审查点名的场景，全部通过 MockTransport / 真实 DB 断言，**不依赖 AS711**：

会话与权限
- 真实 LLM 分支用 MockTransport（含工具调用解析）；
- 会话归属（不能读别人的会话）；
- 模型不可用时**不伪造回答**（503 + 明确原因）。

确认流程
- 跳步（confirmations 与状态不匹配）被拒；
- 一次性凭证**重放**被拒；
- 凭证**过期**被拒；
- 目标**变化**（改名/同名重建/库清单变化）被拒；
- **重复执行**只生效一次（CAS）。

受控删除
- 级联删除分段（授权感知触发器）；
- 向量清理失败 → job 停在 partial 且**可重试**；
- 删除后**检索不再返回**残留向量（tombstone 过滤 + fail-closed）。

派生文档
- 预览/编辑/下载（.md / .html）；
- **越权**：别人的文档读不到、改不了、下不了；
- 源资料**不可改**（派生文档的编辑不动源资料）。
"""

from __future__ import annotations

import datetime
import json

import httpx
import pytest
from django.test import override_settings
from django.utils import timezone

from apps.knowledge.models import (
    DocumentChunk,
    IngestedFile,
    KnowledgeLibrary,
    ParseStatus,
)
from apps.planner import services
from tests.planner_fixtures import legacy_proposal
from apps.planner.llm import LlmClient, LlmProfile
from apps.planner.models import (
    ActionTicket,
    Conversation,
    DeleteGrant,
    DeletionJob,
    DerivedDocument,
    Message,
    OperationRecord,
)

pytestmark = pytest.mark.django_db


# ------------------------------------------------------------------ 工具 ----


def _fake_llm(handler):
    """用 MockTransport 造一个 LLM 客户端（**走真实 HTTP 代码路径**）。"""
    return LlmClient(
        LlmProfile(base_url="http://llm.test/v1", model="test-model"),
        client=httpx.Client(transport=httpx.MockTransport(handler), trust_env=False),
    )


def _tool_call_reply(name: str, arguments: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "test-model",
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ],
                    }
                }
            ],
            "usage": {"total_tokens": 12},
        },
    )


def _text_reply(content: str) -> httpx.Response:
    """普通文本回复（无工具调用）。"""
    return httpx.Response(
        200,
        json={
            "model": "test-model",
            "choices": [{"message": {"content": content}}],
            "usage": {"total_tokens": 8},
        },
    )


def _chunked(library, make_file, *, name="用例.md", count=2, status=ParseStatus.DONE):
    record = make_file(library, name=name, status=status)
    for ordinal in range(0, count):  # 注意：**从 0 开始**（真实数据就有 ordinal=0）
        DocumentChunk.objects.create(
            file=record, ordinal=ordinal, text=f"第{ordinal}段", token_estimate=2
        )
    IngestedFile._base_manager.filter(pk=record.pk).update(chunk_count=count)
    record.refresh_from_db()
    return record


# --------------------------------------------------- 1) 会话与 LLM 分支 ----


def test_read_only_llm_tool_call_executes_immediately(library, make_file, user, grant_member):
    """模型提的工具调用只变成**提案**，绝不直接执行。

    ``user`` 默认对 ``library`` 没有权限，所以这里**按产品口径授权**（把用户
    加成该库成员），而不是放宽 ``check_permission`` —— 权限是产品红线。
    """
    grant_member(library, user)
    record = _chunked(library, make_file)
    conversation = Conversation.objects.create(user=user)

    def handler(request: httpx.Request) -> httpx.Response:
        return _tool_call_reply("read_file", {"file_id": record.pk})

    payload = services.reply_to_message(
        user, conversation, "帮我看看这份资料", client=_fake_llm(handler)
    )
    assert payload["proposals"], payload
    proposal = payload["proposals"][0]
    assert proposal["intent"] == "read_file"
    assert proposal.get("rejected") is not True, proposal
    # 只落了 proposed，没有执行
    operation = OperationRecord.objects.get(pk=proposal["operation_id"])
    assert operation.status == OperationRecord.Status.EXECUTED
    assert operation.result


def test_llm_illegal_tool_arguments_become_rejected_proposal(library, make_file, user):
    """模型给非法参数（缺必填）→ 记成"被拒提案"，不是崩掉也不是执行。"""
    conversation = Conversation.objects.create(user=user)

    def handler(request: httpx.Request) -> httpx.Response:
        # read_file 需要 file_id，这里故意不给
        return _tool_call_reply("read_file", {})

    payload = services.reply_to_message(user, conversation, "看看", client=_fake_llm(handler))
    assert payload["proposals"][0]["rejected"] is True
    assert OperationRecord.objects.count() == 0


def test_rejected_proposal_reason_is_persisted_for_refresh(library, make_file, user):
    """审查条目 57：被拒原因必须**随助手消息持久化**，刷新后仍能解释。

    如果只放在本次 HTTP 响应里，模型又不给正文时，前端刷新后就只剩一条**空气泡**
    （无正文、无待办卡），用户看不到"AI 提了 read_file，但因为参数不合法被拒"。
    """
    conversation = Conversation.objects.create(user=user)

    def handler(request: httpx.Request) -> httpx.Response:
        return _tool_call_reply("read_file", {})   # 缺必填 file_id → 被拒提案

    payload = services.reply_to_message(user, conversation, "看看", client=_fake_llm(handler))
    assert payload["proposals"][0]["rejected"] is True

    message = Message.objects.get(pk=payload["assistant_message_id"])
    assert message.role == Message.Role.ASSISTANT
    assert message.content == ""
    rejected = message.payload["rejected_proposals"]
    assert len(rejected) == 1, rejected
    assert rejected[0]["intent"] == "read_file"
    assert rejected[0]["reason"], "被拒原因不能是空字符串"


def test_empty_reply_is_rejected_by_llm_layer_no_fake_bubble(user):
    """模型返回空正文且无工具调用 → 由 ``llm`` 层判为**失败**，不落假助手消息。

    这条锁住"空气泡"的另一半：产品里不存在"助手回了空内容"这种成功状态
    （``apps/planner/llm.py`` 抛 ``LLMError``，接口层留 system 提示），
    所以界面不需要、也不允许自己编一个"模型什么也没说"的分支。
    """
    from apps.planner.llm import LLMError

    conversation = Conversation.objects.create(user=user)

    def handler(request: httpx.Request) -> httpx.Response:
        return _text_reply("")

    with pytest.raises(LLMError):
        services.reply_to_message(user, conversation, "在吗", client=_fake_llm(handler))
    assert not Message.objects.filter(
        conversation=conversation, role=Message.Role.ASSISTANT
    ).exists()


def test_assistant_unavailable_does_not_fabricate_answer(user):
    """离线模式：抛 ServiceUnavailable，**不返回任何假回答**。"""
    from apps.knowledge.resilience import ServiceUnavailable

    conversation = Conversation.objects.create(user=user)
    with override_settings(KB_MODEL_CALLS_DISABLED=True):
        with pytest.raises(ServiceUnavailable):
            services.reply_to_message(user, conversation, "在吗")
    # 没有生成助手回复
    assert not Message.objects.filter(
        conversation=conversation, role=Message.Role.ASSISTANT
    ).exists()


def test_single_system_message_with_context(
    library, make_file, user, grant_member, monkeypatch, settings
):
    """带检索上下文时，发出去的载荷**只有一条 system 且在最前**。

    这是真实浏览器链路里的阻塞：AS711 的服务模板要求 system 只能在开头，
    早期拼了两条 system（系统提示 + 资料片段）→ **HTTP 400
    "System message must be at the beginning."** → 有资料的问句必然 503。
    这里直接抓取 MockTransport 收到的**最终载荷**来断言，而不是只看返回值。
    """
    grant_member(library, user)
    record = _chunked(library, make_file, name="合同.md", count=2)
    from apps.planner.rag.contracts import Evidence
    evidence = Evidence.from_chunk(record.chunks.select_related("file__library").first()).to_dict()
    settings.KB_RAG_VERIFY_SEMANTIC = False
    conversation = Conversation.objects.create(user=user)

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return _text_reply(f"已根据资料回答。[{evidence['evidence_id']}]")

    # 让检索**一定**返回片段，确保走的是"带上下文"那条分支。
    # 注意：reply_to_message 现在调用的是**结构化入口** build_context_report
    # （旧的 build_context 已不在该路径上）。这里必须隔离新入口，
    # 否则既注入不了上下文，还会意外发起真实检索。
    monkeypatch.setattr(
        services,
        "build_context_report",
        lambda _user, _q, **kw: {
            "context": f"《{record.original_name}》\n质保期 12 个月。",
            "evidence": [evidence],
            # 健康：不降级，因此不会额外插入"检索范围说明"
            "degraded": False,
            "degraded_reason": "",
            "failed_collections": [],
            "collections": ["kb-test-g1"],
        },
    )
    payload = services.reply_to_message(
        user, conversation, "质保期多久？", client=_fake_llm(handler)
    )
    assert payload["content"].startswith("已根据资料回答。")
    sent = captured["payload"]["messages"]
    roles = [message["role"] for message in sent]
    assert roles.count("system") == 1, roles
    assert roles[0] == "system", roles
    # 资料并进了这一条 system，并标注为不可信引用数据
    system_content = sent[0]["content"]
    assert evidence["text"] in system_content
    assert services.UNTRUSTED_CONTEXT_HEADER.split("\n")[0] in system_content
    # 指令部分仍在最前面
    assert system_content.startswith(services.SYSTEM_PROMPT)


def test_compose_document_sends_single_system(user, library, make_file, grant_member):
    """撰写文档同样只能有一条 system（否则有资料的撰写必然 400）。"""
    grant_member(library, user)
    record = _chunked(library, make_file, name="素材.md", count=2)
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return _text_reply("# 生成文档\n\n正文")

    services.compose_document(
        user,
        title="测试文档",
        topic="质保",
        source_file_ids=[record.pk],
        client=_fake_llm(handler),
    )
    sent = captured["payload"]["messages"]
    roles = [message["role"] for message in sent]
    assert roles.count("system") == 1, roles
    assert roles[0] == "system", roles


def test_compose_document_does_not_reuse_the_planner_tool_prompt(
    user, library, make_file, grant_member
):
    """撰写正文**不能**再用策划用的 SYSTEM_PROMPT。

    实测（2026-10-16，用户反馈"写一个出行报告"还是没写）：沿用策划提示时，
    模型把工具调用语法本身当成了正文。撰写阶段没有工具可调，系统提示里
    就不该出现"必须直接调用 compose_document"这类话。
    """
    grant_member(library, user)
    record = _chunked(library, make_file, name="素材.md", count=1)
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return _text_reply("# 出行报告\n\n正文")

    services.compose_document(
        user, title="出行报告", topic="陵水出差", source_file_ids=[record.pk],
        client=_fake_llm(handler),
    )
    system_content = captured["payload"]["messages"][0]["content"]
    assert system_content.startswith(services.DOCUMENT_WRITING_PROMPT)
    assert services.SYSTEM_PROMPT not in system_content
    # 撰写提示里要明确"不要输出工具调用语法"
    assert "工具调用" in services.DOCUMENT_WRITING_PROMPT
    # 资料片段仍然按不可信引用数据包裹
    assert services.UNTRUSTED_CONTEXT_HEADER.split("\n")[0] in system_content


@pytest.mark.parametrize(
    "text, expected",
    [
        ('compose_document(title="出行报告", topic="陵水")', True),
        ('  search_knowledge(query="质保期")  ', True),
        ("read_file(file_id=40)", True),
        # 真正的正文：多行、有 Markdown 结构
        ("# 出行报告\n\n- 10-14 去程\n\n《海口东-陵水.pdf》", False),
        ("compose_document 是工具名，但它出现在句子里", False),
        ("", False),
    ],
)
def test_tool_call_echo_detection(text, expected):
    """只把"整篇就是一个工具调用"判成空壳；正常正文（多行）不受影响。"""
    assert services._looks_like_tool_call_echo(text) is expected


def test_compose_document_retries_once_when_model_echoes_the_tool_call(
    user, library, make_file, grant_member
):
    """模型把工具调用当正文 → **纠正一次**，正文正常就如实保存（不是存空壳）。"""
    grant_member(library, user)
    record = _chunked(library, make_file, name="素材.md", count=1)
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        calls.append(payload)
        if len(calls) == 1:
            return _text_reply('compose_document(title="出行报告", topic="陵水出差")')
        return _text_reply("# 出行报告\n\n《素材.md》里的行程。")

    result = services.compose_document(
        user, title="出行报告", topic="陵水出差", source_file_ids=[record.pk],
        client=_fake_llm(handler),
    )
    assert len(calls) == 2, "第一次是工具调用回显，必须再要一次正文"
    assert result["content_markdown"].startswith("# 出行报告")
    document = DerivedDocument.objects.get(pk=result["document_id"])
    assert document.content_markdown.startswith("# 出行报告")
    # 纠正用的第二条请求：把回显当上一轮助手输出，并明确要求只写正文
    retry_messages = calls[1]["messages"]
    assert retry_messages[-2]["role"] == "assistant"
    assert "compose_document(" in retry_messages[-2]["content"]
    assert retry_messages[-1]["role"] == "user"
    assert "工具" in retry_messages[-1]["content"]


def test_compose_document_fails_closed_when_tool_call_echo_repeats(
    user, library, make_file, grant_member
):
    """纠正后仍然只是工具调用 → **明确报错、不存草稿**（绝不留下空壳文档）。"""
    grant_member(library, user)
    record = _chunked(library, make_file, name="素材.md", count=1)
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content.decode("utf-8")))
        return _text_reply('compose_document(title="出行报告", topic="陵水出差")')

    with pytest.raises(services.PlannerError) as excinfo:
        services.compose_document(
            user, title="出行报告", topic="陵水出差", source_file_ids=[record.pk],
            client=_fake_llm(handler),
        )
    assert len(calls) == 2, "先纠正一次，再判定失败"
    assert "空壳" in str(excinfo.value)
    assert DerivedDocument.objects.filter(created_by=user).count() == 0


def test_normalize_messages_collapses_and_demotes():
    """规范化：开头连续 system 合并；中间 system 降级为 user（不丢内容）。"""
    from apps.planner.llm import normalize_messages

    normalized = normalize_messages(
        [
            {"role": "system", "content": "A"},
            {"role": "system", "content": "B"},
            {"role": "user", "content": "问"},
            {"role": "system", "content": "C"},
            {"role": "assistant", "content": "答"},
        ]
    )
    assert [m["role"] for m in normalized] == ["system", "user", "user", "assistant"]
    assert normalized[0]["content"] == "A\n\nB"
    assert "C" in normalized[2]["content"]

    # 没有 system 的输入不凭空造一条
    assert normalize_messages([{"role": "user", "content": "x"}]) == [
        {"role": "user", "content": "x"}
    ]
    assert normalize_messages([]) == []


def test_armed_expires_and_execute_is_refused(admin_user, library, make_file):
    """**先三确认 → 执行窗口过期 → execute 拒绝**（审查点名的安全边界）。

    为什么必须单独测这一条：只测"确认时凭证过期"是不够的——那证不了
    "ARMED 之后能不能无限期执行"。早期 ARMED 是永久的，等于把三次确认
    退化成"某一天确认过"。这里走完整三次确认，再把执行窗口推到过期，
    断言 execute 被拒且操作落到 EXPIRED（要求重新提案）。
    """
    record = _chunked(library, make_file)
    proposal = legacy_proposal(
        admin_user, intent="delete_file", params={"file_id": record.pk}
    )
    operation = OperationRecord.objects.get(pk=proposal["operation_id"])

    token = proposal["confirm_token"]
    for _ in range(services.DESTRUCTIVE_CONFIRMATIONS):
        result = services.confirm_operation(
            admin_user, operation, token=token, typed_name=record.original_name
        )
        token = result.get("next_confirm_token", "")
        operation.refresh_from_db()

    assert operation.status == OperationRecord.Status.ARMED
    # 进入 ARMED 时必须**有**执行窗口（fail-closed：没有窗口也视为不可执行）
    assert operation.armed_at is not None
    assert operation.armed_expires_at is not None
    assert operation.armed_expires_at > timezone.now()

    # 把执行窗口推到过去（模拟"确认完放了几小时才点执行"）
    OperationRecord.objects.filter(pk=operation.pk).update(
        armed_expires_at=timezone.now() - datetime.timedelta(seconds=1)
    )
    operation.refresh_from_db()
    with pytest.raises(services.PlannerError, match="过期"):
        services.execute_operation(admin_user, operation)

    # 过期后操作落到 EXPIRED（终态），不能被再次执行
    operation.refresh_from_db()
    assert operation.status == OperationRecord.Status.EXPIRED
    with pytest.raises(services.PlannerError):
        services.execute_operation(admin_user, operation)
    # **资料没有被删除**（过期确认不能产生副作用）
    assert IngestedFile._base_manager.filter(pk=record.pk).exists()


def test_armed_without_expiry_is_fail_closed(admin_user, library, make_file):
    """没有执行窗口信息（``armed_expires_at`` 为空）一律视为过期，不放行。"""
    record = _chunked(library, make_file)
    proposal = legacy_proposal(
        admin_user, intent="read_file", params={"file_id": record.pk}
    )
    operation = OperationRecord.objects.get(pk=proposal["operation_id"])
    services.confirm_operation(admin_user, operation, token=proposal["confirm_token"])
    OperationRecord.objects.filter(pk=operation.pk).update(armed_expires_at=None)
    operation.refresh_from_db()
    assert operation.armed_is_expired() is True
    with pytest.raises(services.PlannerError, match="过期"):
        services.execute_operation(admin_user, operation)


def test_resume_refuses_expired_armed(api, admin_user, library, make_file):
    """刷新恢复接口对已过期的 ARMED 返回 409 `armed_expired`（要求重新提案）。"""
    record = _chunked(library, make_file)
    proposal = legacy_proposal(
        admin_user, intent="read_file", params={"file_id": record.pk}
    )
    operation = OperationRecord.objects.get(pk=proposal["operation_id"])
    services.confirm_operation(admin_user, operation, token=proposal["confirm_token"])
    OperationRecord.objects.filter(pk=operation.pk).update(
        armed_expires_at=timezone.now() - datetime.timedelta(seconds=1)
    )
    api.force_authenticate(user=admin_user)
    response = api.post(f"/api/v1/planner/operations/{operation.pk}/resume/", {}, format="json")
    assert response.status_code == 409, response.content[:200]
    assert response.json().get("code") == "armed_expired"
    operation.refresh_from_db()
    assert operation.status == OperationRecord.Status.EXPIRED


def test_resume_returns_confirmations_for_correct_step(api, admin_user, library, make_file):
    """resume 必须把后端确认进度返回给界面（否则会从第 1 弹窗重来）。

    真实缺陷：resumeOperation 没把 `confirmations` 带进 proposal →
    proposalCard 把 step 设为 0 → runConfirmFlow 从第 1 次开始弹，
    而手里的票证是 final 的 → 第 1 个弹窗不要求输入名称 → 提交空 typed_name
    必然失败。界面必须能知道"下一次是第几次"。
    """
    record = _chunked(library, make_file)
    proposal = legacy_proposal(
        admin_user, intent="delete_file", params={"file_id": record.pk}
    )
    operation = OperationRecord.objects.get(pk=proposal["operation_id"])

    # 走完第 1、2 次确认 → 现在停在 confirmed_2 / confirmations=2
    step1 = services.confirm_operation(
        admin_user, operation, token=proposal["confirm_token"]
    )
    operation.refresh_from_db()
    services.confirm_operation(admin_user, operation, token=step1["next_confirm_token"])
    operation.refresh_from_db()
    assert operation.confirmations == 2
    assert operation.status == OperationRecord.Status.CONFIRMED_2

    api.force_authenticate(user=admin_user)
    response = api.post(f"/api/v1/planner/operations/{operation.pk}/resume/", {}, format="json")
    assert response.status_code == 200, response.content[:200]
    body = response.json()
    assert body["confirmations"] == 2, body
    assert body["required_confirmations"] == 3, body
    # 第 3 次才要求输名称；且必须是 final 用途的票证
    assert body["typed_name_required"] == record.original_name
    ticket = ActionTicket.objects.filter(operation=operation, consumed_at__isnull=True).first()
    assert ticket is not None and ticket.scope == ActionTicket.Scope.CONFIRM_FINAL


def test_name_check_accepts_name_with_surrounding_spaces(admin_user, library, make_file):
    """目标名本身带首尾空格时，**原样**输入必须能通过（不能 trim 后永远失败）。"""
    record = _chunked(library, make_file, name="  合同 v2 .md  ")  # 首尾都有空格
    assert record.original_name == "  合同 v2 .md  ", repr(record.original_name)

    proposal = legacy_proposal(
        admin_user, intent="delete_file", params={"file_id": record.pk}
    )
    operation = OperationRecord.objects.get(pk=proposal["operation_id"])
    token = proposal["confirm_token"]
    for _ in range(services.DESTRUCTIVE_CONFIRMATIONS):
        result = services.confirm_operation(
            admin_user, operation, token=token, typed_name=record.original_name
        )
        token = result.get("next_confirm_token", "")
        operation.refresh_from_db()
    assert operation.status == OperationRecord.Status.ARMED


def test_name_check_still_rejects_trimmed_variant(admin_user, library, make_file):
    """但**去掉空格**的变体仍要被拒（原样比对不等于放宽）。"""
    record = _chunked(library, make_file, name="  合同 v3 .md  ")
    proposal = legacy_proposal(
        admin_user, intent="delete_file", params={"file_id": record.pk}
    )
    operation = OperationRecord.objects.get(pk=proposal["operation_id"])
    token = proposal["confirm_token"]
    step1 = services.confirm_operation(admin_user, operation, token=token)
    operation.refresh_from_db()
    step2 = services.confirm_operation(
        admin_user, operation, token=step1["next_confirm_token"]
    )
    operation.refresh_from_db()
    with pytest.raises(services.PlannerError, match="名称不匹配"):
        services.confirm_operation(
            admin_user,
            operation,
            token=step2["next_confirm_token"],
            typed_name="合同 v3 .md",  # 少了首尾空格 → 拒绝
        )


def test_operation_list_exposes_armed_window(api, admin_user, library, make_file):
    """列表要暴露 ARMED 及其 TTL，界面才能给受窗口约束的执行入口。"""
    record = _chunked(library, make_file)
    proposal = legacy_proposal(
        admin_user, intent="read_file", params={"file_id": record.pk}
    )
    operation = OperationRecord.objects.get(pk=proposal["operation_id"])
    services.confirm_operation(admin_user, operation, token=proposal["confirm_token"])
    operation.refresh_from_db()
    assert operation.status == OperationRecord.Status.ARMED

    api.force_authenticate(user=admin_user)
    response = api.get("/api/v1/planner/operations/")
    assert response.status_code == 200
    row = next(
        item for item in response.json()["results"] if item["id"] == operation.pk
    )
    assert row["armed_expires_at"], row
    assert row["armed_expired"] is False


def test_conversation_belongs_to_user(api, user, other_user):
    """会话归属：不能读别人的会话（404，不泄露存在性）。"""
    conversation = Conversation.objects.create(user=other_user)
    Message.objects.create(conversation=conversation, role=Message.Role.USER, content="秘密")
    api.force_authenticate(user=user)
    response = api.get(f"/api/v1/planner/conversations/{conversation.pk}/")
    assert response.status_code == 404


def test_chat_requires_staff_for_system_operations(user):
    """系统级操作对普通用户 fail-closed（未声明权限名也拒绝）。"""
    from tests.planner_fixtures import require_spec as get_spec

    spec = get_spec("reset_breaker")
    with pytest.raises(services.PlannerError):
        services.check_permission(user, spec, {})


# ------------------------------------------- 2) 确认流程的边界与攻击面 ----


def _destructive_operation(user, library, record) -> OperationRecord:
    payload = legacy_proposal(
        user, intent="delete_file", params={"file_id": record.pk}
    )
    operation = OperationRecord.objects.get(pk=payload["operation_id"])
    return operation


def test_destructive_requires_three_confirmations_and_exact_name(admin_user, library, make_file):
    """删除类操作：必须三次确认，且第 3 次名称必须完全一致。"""
    record = _chunked(library, make_file)
    proposal = legacy_proposal(
        admin_user, intent="delete_file", params={"file_id": record.pk}
    )
    operation = OperationRecord.objects.get(pk=proposal["operation_id"])
    assert operation.required_confirmations == services.DESTRUCTIVE_CONFIRMATIONS == 3

    # 第 1、2 步：只需凭证
    step1 = services.confirm_operation(admin_user, operation, token=proposal["confirm_token"])
    assert step1["next"] == "confirm_step_2"
    operation.refresh_from_db()
    step2 = services.confirm_operation(
        admin_user, operation, token=step1["next_confirm_token"]
    )
    assert step2["next"] == "confirm_final"
    assert step2["typed_name_required"] == record.original_name

    # 第 3 步：名称不对 → 拒绝，且状态仍停在 CONFIRMED_2（没有进入 ARMED）
    operation.refresh_from_db()
    with pytest.raises(services.PlannerError, match="名称不匹配"):
        services.confirm_operation(
            admin_user, operation, token=step2["next_confirm_token"], typed_name="写错了"
        )
    operation.refresh_from_db()
    assert operation.status == OperationRecord.Status.CONFIRMED_2

    # 名称正确 → 进入 ARMED
    step3 = services.confirm_operation(
        admin_user,
        operation,
        token=step2["next_confirm_token"],
        typed_name=record.original_name,
    )
    assert step3["next"] == "execute"
    operation.refresh_from_db()
    assert operation.status == OperationRecord.Status.ARMED
    assert operation.confirmations == 3


def test_confirmation_skipping_is_rejected(admin_user, library, make_file):
    """跳过第 1 步直接拿第 2 步的凭证 → 拒（确认次数与状态必须匹配）。"""
    record = _chunked(library, make_file)
    operation = _destructive_operation(admin_user, library, record)
    # 手工造一张 CONFIRM_STEP_2 的凭证：状态仍是 PROPOSED，应当被拒
    token = services.issue_ticket(
        admin_user,
        operation=operation,
        conversation=None,
        scope=ActionTicket.Scope.CONFIRM_STEP_2,
        object_ref=operation.target_ref,
    )
    with pytest.raises(services.PlannerError):
        services.confirm_operation(
            admin_user, operation, token=token, typed_name=record.original_name
        )


def test_ticket_replay_is_rejected(admin_user, library, make_file):
    """一次性凭证用过即废（重放拒绝）。"""
    record = _chunked(library, make_file)
    proposal = legacy_proposal(
        admin_user, intent="delete_file", params={"file_id": record.pk}
    )
    operation = OperationRecord.objects.get(pk=proposal["operation_id"])
    token = proposal["confirm_token"]
    services.confirm_operation(admin_user, operation, token=token)
    operation.refresh_from_db()
    with pytest.raises(services.PlannerError, match="已被使用|不匹配"):
        services.confirm_operation(admin_user, operation, token=token)


def test_expired_ticket_is_rejected(admin_user, library, make_file):
    record = _chunked(library, make_file)
    proposal = legacy_proposal(
        admin_user, intent="delete_file", params={"file_id": record.pk}
    )
    ActionTicket.objects.update(
        expires_at=timezone.now() - datetime.timedelta(seconds=1)
    )
    operation = OperationRecord.objects.get(pk=proposal["operation_id"])
    with pytest.raises(services.PlannerError, match="过期"):
        services.confirm_operation(admin_user, operation, token=proposal["confirm_token"])


def test_target_change_invalidates_operation(admin_user, library, make_file, monkeypatch):
    """目标身份变化 → 旧确认不能再推进。

    **不能**去直接 UPDATE 受保护的 ``content_sha256``：那是篡改，数据库触发器
    会（正确地）拒绝——用它造"指纹变化"等于在测试里试图绕过不可篡改，
    测试本身就不该那么写。

    这里改为**合法地**让身份解析结果变化：把 ``_describe_target`` 的目标身份
    换成另一个值（模拟"同名重建/内容被替换"这类真实变化），
    断言 ``_verify_target_unchanged`` 会拒。守卫本身没有被摘除。
    """
    record = _chunked(library, make_file)
    operation = _destructive_operation(admin_user, library, record)
    # 原始身份是 file:<pk>:<sha>，且此刻是"未变化"状态
    services._verify_target_unchanged(operation)

    # 模拟身份解析结果变化（例如记录被合法地换成了另一份内容/重建）
    monkeypatch.setattr(
        services,
        "_describe_target",
        lambda spec, params: (f"file:{record.pk}:{'f' * 64}", record.original_name),
    )
    with pytest.raises(services.PlannerError, match="身份|名称"):
        services._verify_target_unchanged(operation)


def test_library_rename_invalidates_operation(admin_user, library, make_file):
    """库**合法改名**后，旧提案的展示名不再匹配 → 拒绝（无需碰任何受保护字段）。"""
    _chunked(library, make_file, name="rename.md")
    proposal = legacy_proposal(
        admin_user, intent="delete_library", params={"library": library.code}
    )
    operation = OperationRecord.objects.get(pk=proposal["operation_id"])
    KnowledgeLibrary.objects.filter(pk=library.pk).update(name="改过名字的库")
    with pytest.raises(services.PlannerError, match="名称"):
        services._verify_target_unchanged(operation)


def test_library_manifest_change_requires_reproposal(admin_user, library, make_file):
    """整库删除：库内资料清单变化 → 必须重新提案。"""
    _chunked(library, make_file, name="a.md")
    proposal = legacy_proposal(
        admin_user, intent="delete_library", params={"library": library.code}
    )
    operation = OperationRecord.objects.get(pk=proposal["operation_id"])
    # 提案后又多了一份资料
    _chunked(library, make_file, name="b.md")
    with pytest.raises(services.PlannerError, match="清单|变化"):
        services._verify_target_unchanged(operation)


def test_duplicate_execute_only_runs_once(admin_user, library, make_file):
    """并发/重复执行：CAS 只允许一次成功。"""
    record = _chunked(library, make_file)
    proposal = legacy_proposal(
        admin_user, intent="read_file", params={"file_id": record.pk}
    )
    operation = OperationRecord.objects.get(pk=proposal["operation_id"])
    services.confirm_operation(admin_user, operation, token=proposal["confirm_token"])
    first = services.execute_operation(admin_user, operation)
    assert first["status"] == OperationRecord.Status.EXECUTED
    with pytest.raises(services.PlannerError):
        services.execute_operation(admin_user, operation)  # 第二次必拒


# --------------------------------------------------- 3) 受控删除与清理 ----


def test_controlled_delete_cascades_chunks(admin_user, library, make_file):
    """级联删除分段必须成功（授权感知触发器；旧实现会被无条件拒绝挡住）。"""
    record = _chunked(library, make_file, count=3)
    proposal = legacy_proposal(
        admin_user, intent="delete_file", params={"file_id": record.pk}
    )
    operation = OperationRecord.objects.get(pk=proposal["operation_id"])
    token = proposal["confirm_token"]
    for _ in range(services.DESTRUCTIVE_CONFIRMATIONS):
        result = services.confirm_operation(
            admin_user,
            operation,
            token=token,
            typed_name=record.original_name,
        )
        token = result.get("next_confirm_token", "")
        operation.refresh_from_db()
    outcome = services.execute_operation(admin_user, operation)
    assert outcome["result"]["deleted"] == "ingested_file"
    assert not IngestedFile._base_manager.filter(pk=record.pk).exists()
    assert DocumentChunk.objects.filter(file_id=record.pk).count() == 0


def test_deletion_reports_partial_when_vector_cleanup_fails(
    admin_user, library, make_file, monkeypatch
):
    """向量清理失败 → job 停 partial，**不谎报完成**，且可重试。"""
    record = _chunked(library, make_file)
    proposal = legacy_proposal(
        admin_user, intent="delete_file", params={"file_id": record.pk}
    )
    operation = OperationRecord.objects.get(pk=proposal["operation_id"])
    token = proposal["confirm_token"]
    for _ in range(services.DESTRUCTIVE_CONFIRMATIONS):
        result = services.confirm_operation(
            admin_user, operation, token=token, typed_name=record.original_name
        )
        token = result.get("next_confirm_token", "")
        operation.refresh_from_db()

    monkeypatch.setattr(
        services,
        "_cleanup_vectors",
        lambda snapshot: {"ok": False, "error": "QdrantUnavailable"},
    )
    outcome = services.execute_operation(admin_user, operation)
    assert outcome["result"]["complete"] is False
    job = DeletionJob.objects.get(pk=outcome["result"]["job_id"])
    assert job.status == DeletionJob.Status.PARTIAL

    # 重试：这次向量清理成功 → 变成 completed
    monkeypatch.setattr(
        services, "_cleanup_vectors", lambda snapshot: {"ok": True, "collections": {}}
    )
    retried = services.retry_deletion_job(admin_user, job)
    assert retried["complete"] is True


def test_lease_conflict_blocks_deletion(admin_user, library, make_file):
    """正在处理（租约未过期）的资料不能删。"""
    record = _chunked(library, make_file, status=ParseStatus.RUNNING)
    IngestedFile._base_manager.filter(pk=record.pk).update(
        parse_lease_token="busy",
        parse_lease_expires_at=timezone.now() + datetime.timedelta(minutes=5),
    )
    record.refresh_from_db()
    with pytest.raises(services.PlannerError, match="正在处理"):
        services._assert_not_being_processed(record)


def test_search_drops_residual_vectors_of_deleted_file(db, library):
    """删除后残留向量**不得**出现在检索结果里（tombstone + fail-closed）。"""
    from apps.knowledge.pipeline import _drop_tombstoned

    class _Candidate:
        def __init__(self, payload, point_id="p"):
            self.payload = payload
            self.point_id = point_id
            self.collection = "kb-x"
            self.score = 1.0

    case = _Candidate(
        {
            "file_id": 999999,  # 数据库里不存在 → 必须丢弃
            "content_sha256": "a" * 64,
            "library_id": library.pk,
            "text": "已删除内容",
        }
    )
    # 缺 content_sha256 / library_id 的一律丢弃（fail-closed，不是"有才比"）
    missing = _Candidate({"file_id": 1, "text": "无指纹"})
    report: dict = {"_authorized_library_ids": [library.pk]}
    kept = _drop_tombstoned([case, missing], report)
    assert kept == []
    assert report["tombstoned_dropped"] == 2
    assert report["tombstoned_reasons"]


# ------------------------------------------------------- 4) 派生文档 ----


def _document(user, library) -> DerivedDocument:
    document = DerivedDocument.objects.create(
        created_by=user, title="派生", content_markdown="# 标题\n\n正文"
    )
    document.libraries.set([library])
    return document


def test_derived_document_edit_is_allowed(api, user, library):
    """派生文档**可编辑**（不能被不可变层挡住——那是误伤）。"""
    document = _document(user, library)
    api.force_authenticate(user=user)
    response = api.put(
        f"/api/v1/planner/documents/{document.pk}/",
        {"content_markdown": "# 改过了"},
        format="json",
    )
    assert response.status_code == 200, response.content[:300]
    document.refresh_from_db()
    assert document.content_markdown == "# 改过了"
    assert document.status == DerivedDocument.Status.EDITED


def test_derived_document_does_not_touch_sources(user, library, make_file):
    """编辑派生文档**绝不动源资料**（内容/归属/指纹都不变）。"""
    record = _chunked(library, make_file)
    before = IngestedFile._base_manager.filter(pk=record.pk).values().first()
    document = _document(user, library)
    document.sources.set([record])
    services.update_derived_document(user, document.pk, content_markdown="换内容")
    after = IngestedFile._base_manager.filter(pk=record.pk).values().first()
    assert after == before


def test_derived_document_download_md_and_html(api, user, library):
    """下载：真实 .md 与独立 .html，且响应头与实际格式一致。"""
    document = _document(user, library)
    api.force_authenticate(user=user)
    md = api.get(f"/api/v1/planner/documents/{document.pk}/download/?export_format=md")
    assert md.status_code == 200
    assert md["Content-Type"].startswith("text/markdown")
    assert md["X-Document-Format"] == "md"
    assert ".md" in md["Content-Disposition"]

    html = api.get(f"/api/v1/planner/documents/{document.pk}/download/?export_format=html")
    assert html.status_code == 200
    assert html["Content-Type"].startswith("text/html")
    assert html["X-Document-Format"] == "html"
    assert ".html" in html["Content-Disposition"]


def test_derived_document_isolation(api, user, other_user, library):
    """越权：别人的派生文档读不到、改不了、下不了。"""
    document = _document(other_user, library)
    api.force_authenticate(user=user)
    assert api.get(f"/api/v1/planner/documents/{document.pk}/").status_code == 404
    assert (
        api.put(
            f"/api/v1/planner/documents/{document.pk}/",
            {"content_markdown": "偷改"},
            format="json",
        ).status_code
        == 404
    )
    assert (
        api.get(f"/api/v1/planner/documents/{document.pk}/download/").status_code == 404
    )


def test_compose_document_refuses_without_sources(user, library, grant_member, monkeypatch):
    """没有依据时**明确拒绝**（不生成无依据的"全库文档"）。

    先给用户**可读授权**：否则用例会因为"没有可用的信息库"提前失败，
    根本走不到"检索为空"这条断言——那是在测权限，不是在测这个行为。
    """
    grant_member(library, user)
    monkeypatch.setattr(
        services, "knowledge_search", lambda **kwargs: {"results": [], "report": {}}
    )
    with pytest.raises(services.PlannerError, match="没有检索到"):
        services.compose_document(user, title="空文档", topic="不存在的主题")


def test_compose_document_respects_source_permissions(user, other_user, library, make_file):
    """勾选来源也要过权限：别人库里的资料不能当来源。"""
    other_library = KnowledgeLibrary.objects.create(code="other-load", name="别人的库")
    record = _chunked(other_library, make_file, name="别人的.md")
    with pytest.raises(services.PlannerError):
        services.compose_document(
            user, title="偷用", topic="x", source_file_ids=[record.pk]
        )


# ------------------------------------------- 5) 受控删除授权表本身 ----


def test_delete_grant_is_required_and_consumed(admin_user, library, make_file):
    """授权行必须存在才放行；用过即标记 consumed（不可复用）。"""
    record = _chunked(library, make_file)
    operation = _destructive_operation(admin_user, library, record)
    grant = DeleteGrant.objects.create(
        operation=operation,
        user=admin_user,
        target_kind=DeleteGrant.TargetKind.INGESTED_FILE,
        target_ref=str(record.pk),
        expires_at=timezone.now() + datetime.timedelta(minutes=5),
    )
    assert grant.consumed_at is None

    # 没有授权时删除应被触发器拒绝
    from django.db import transaction

    other = _chunked(library, make_file, name="没有授权.md")
    with pytest.raises(Exception):
        with transaction.atomic():
            IngestedFile._base_manager.filter(pk=other.pk).delete()
