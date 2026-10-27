"""「本次检索范围」必须一路生效到**工具执行层**（2026-10-19 用户报的 bug）。

现场（截图 + 数据库取证）：用户在输入栏把「本次检索」勾成「开发验收资料库」
（code ``codex-acceptance``）后问"谁出差了?"，模型调了
``search_knowledge(query="出差")`` —— **没带 library 参数**。旧实现于是按
"全部有权查看的库"检索，把测试库（``contract-test``）里三份出差资料也搜了出来。
也就是说：勾选只收窄了**背景上下文**（``build_context_report``），模型发起的
工具调用完全绕过了它。

这批用例锁住的正是"从勾选到执行"这条链，逐段都有断言：

1. 提案时把勾选范围**落库**（``OperationRecord.library_scope``）；
2. 执行 ``search_knowledge`` 时检索集合 = 范围内可用库（不再回落全库）；
3. 模型显式指定的库落在范围外 → **明确拒绝**（不静默放宽）；
4. 勾的库已删除/停用/无权限 → **拒绝执行并说明**，绝不悄悄改成全库；
5. ``list_files`` / ``read_file`` / ``compose_document`` 同样只能落在范围内；
6. 没勾库（空范围）时行为与以前完全一致（这是对旧用例的兼容底线）。
"""

from __future__ import annotations

import json

import httpx
import pytest

from apps.planner import services
from tests.planner_fixtures import prepare_automatic_operation
from apps.planner.llm import LlmClient, LlmProfile
from apps.planner.models import Conversation, Message, OperationRecord

pytestmark = pytest.mark.django_db


# ------------------------------------------------------------------ 工具 ----


def _fake_llm(handler):
    """走真实 HTTP 代码路径的假模型（MockTransport）。"""
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


def _conversation(user) -> Conversation:
    return Conversation.objects.create(user=user)


def _arm(user, intent: str, params: dict, *, scope=None) -> OperationRecord:
    """Pause before execution to test racing scope changes and denied dispatch."""
    payload = prepare_automatic_operation(user, intent=intent, params=params, conversation=_conversation(user), library_scope=scope)
    return OperationRecord.objects.get(pk=payload["operation_id"])


def _run(user, intent: str, params: dict, *, scope=None) -> dict:
    """提案并跑到结束：免确认意图（搜索）直接拿 proposal 里的结果。"""
    proposal = services.propose_operation(
        user,
        intent=intent,
        params=params,
        conversation=_conversation(user),
        library_scope=scope,
    )
    if proposal["status"] == OperationRecord.Status.EXECUTED:
        return {"status": proposal["status"], "result": proposal["result"]}
    operation = OperationRecord.objects.get(pk=proposal["operation_id"])
    services.confirm_operation(user, operation, token=proposal["confirm_token"])
    return services.execute_operation(user, operation)


@pytest.fixture
def search_calls(monkeypatch):
    """把向量检索换成"只记录参数"的假实现（用例测的是范围，不是向量库）。"""
    calls: list[dict] = []

    def fake_search(**kwargs):
        calls.append(kwargs)
        return {
            "results": [
                {
                    "file_id": 1,
                    "chunk_ordinal": 0,
                    "text": "出差行程",
                    "score": 0.9,
                }
            ],
            "report": {},
        }

    monkeypatch.setattr(services, "knowledge_search", fake_search)
    return calls


@pytest.fixture
def two_libraries(user, make_library, grant_member):
    """两个都已授权、都启用的库：用来区分"范围内"与"范围外"。"""
    scope_lib = make_library(code="scope-lib", name="本次范围库")
    other_lib = make_library(code="other-lib", name="范围外库")
    grant_member(scope_lib, user)
    grant_member(other_lib, user)
    return scope_lib, other_lib


# =============================================== 1) 勾选范围要落库、随提案走 ===


def test_chat_scope_is_persisted_on_the_proposed_operation(
    user, library, grant_member, monkeypatch
):
    """模型发起工具调用时，勾选范围必须写进 OperationRecord（执行层才拿得到）。"""
    grant_member(library, user)
    monkeypatch.setattr(
        services, "knowledge_search", lambda **kwargs: {"results": [], "report": {}}
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return _tool_call_reply("search_knowledge", {"query": "出差"})

    payload = services.reply_to_message(
        user,
        _conversation(user),
        "谁出差了?",
        client=_fake_llm(handler),
        library_codes=[library.code],
    )
    proposal = payload["proposals"][0]
    assert proposal.get("rejected") is not True, proposal
    assert proposal["library_scope"] == [library.code]

    operation = OperationRecord.objects.get(pk=proposal["operation_id"])
    # 落库（不是只在本次响应里）
    assert operation.library_scope == [library.code]
    # 用户消息也留了"这一问基于哪些库"的原始输入
    user_message = Message.objects.get(pk=payload["user_message_id"])
    assert user_message.payload["library_codes"] == [library.code]


def test_chat_without_scope_keeps_operation_unrestricted(user, library, grant_member, monkeypatch):
    grant_member(library, user)
    monkeypatch.setattr(
        services, "knowledge_search", lambda **kwargs: {"results": [], "report": {}}
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return _tool_call_reply("search_knowledge", {"query": "出差"})

    payload = services.reply_to_message(
        user, _conversation(user), "谁出差了?", client=_fake_llm(handler)
    )
    operation = OperationRecord.objects.get(pk=payload["proposals"][0]["operation_id"])
    assert operation.library_scope == [], "没勾库不该凭空产生范围限制"


def test_scope_codes_are_normalized(monkeypatch):
    """去空白、去重、保序（容忍模型/前端重复勾同一个库）。"""
    assert services._library_scope_codes(
        [" b ", "", None, "a", "b", "a"]
    ) == ["b", "a"]
    assert services._library_scope_codes(None) == []


# ======================================= 2) 执行 search_knowledge 时范围硬生效 ===


def test_scoped_search_only_covers_selected_library(user, two_libraries, search_calls):
    scope_lib, _other = two_libraries
    outcome = _run(user, "search_knowledge", {"query": "出差"}, scope=[scope_lib.code])

    assert [row.code for row in search_calls[0]["libraries"]] == [scope_lib.code]
    # 结果里如实回报"实际检索了哪些库"，卡片据此可核对
    assert outcome["result"]["library_codes"] == [scope_lib.code]


def test_unscoped_search_still_covers_all_readable_libraries(user, two_libraries, search_calls):
    """兼容底线：没勾库时必须还是"全部可读库"（与引入范围之前一致）。"""
    scope_lib, other_lib = two_libraries
    outcome = _run(user, "search_knowledge", {"query": "出差"})
    assert {row.code for row in search_calls[0]["libraries"]} == {
        scope_lib.code,
        other_lib.code,
    }
    assert sorted(outcome["result"]["library_codes"]) == sorted(
        [scope_lib.code, other_lib.code]
    )


def test_explicit_library_outside_scope_is_rejected(user, two_libraries, search_calls):
    """模型显式点名范围外的库 → 拒绝，而不是"照它的要求扩大范围"。

    搜索知识库是免确认意图（提案即执行），所以拒绝发生在 ``propose_operation``
    里；记录会落成 FAILED 并带原因，审计照样留痕。
    """
    scope_lib, other_lib = two_libraries
    with pytest.raises(services.PlannerError) as exc:
        services.propose_operation(
            user,
            intent="search_knowledge",
            params={"query": "出差", "library": other_lib.code},
            conversation=_conversation(user),
            library_scope=[scope_lib.code],
        )
    assert "不在本次检索范围" in str(exc.value)
    assert other_lib.code in str(exc.value)
    # 拒绝发生在检索之前：不能"先搜了再报错"
    assert search_calls == []
    failed = OperationRecord.objects.get(intent="search_knowledge", status=OperationRecord.Status.FAILED)
    assert "不在本次检索范围" in (failed.error or "")


def test_deactivated_scope_library_refuses_instead_of_widening(
    user, make_library, grant_member, search_calls
):
    """勾的库被停用 → 明确拒绝（绝不静默回落到"全部可读库"）。"""
    disabled = make_library(code="gone-lib", name="已停用库", active=False)
    grant_member(disabled, user)
    other = make_library(code="ok-lib", name="另一个库")
    grant_member(other, user)

    with pytest.raises(services.PlannerError) as exc:
        services.propose_operation(
            user,
            intent="search_knowledge",
            params={"query": "出差"},
            conversation=_conversation(user),
            library_scope=[disabled.code],
        )
    assert "不可用" in str(exc.value)
    assert disabled.code in str(exc.value)
    assert search_calls == []


def test_partially_unusable_scope_uses_the_usable_part(
    user, make_library, grant_member, search_calls
):
    """部分勾选已失效时：只搜仍可用的部分（不因个别失效就整单失败）。"""
    usable = make_library(code="usable-lib", name="可用库")
    grant_member(usable, user)
    outcome = _run(
        user, "search_knowledge", {"query": "出差"}, scope=[usable.code, "ghost-lib"]
    )
    assert [row.code for row in search_calls[0]["libraries"]] == [usable.code]
    assert outcome["result"]["library_codes"] == [usable.code]


# ================================================= 3) 其它只读意图同样收敛 ===


def test_list_files_respects_scope(user, make_library, make_file, grant_member):
    scope_lib = make_library(code="scope-lib", name="范围库")
    other_lib = make_library(code="other-lib", name="范围外库")
    grant_member(scope_lib, user)
    grant_member(other_lib, user)
    make_file(scope_lib, name="范围内.md")
    make_file(other_lib, name="范围外.md")

    outcome = _run(user, "list_files", {}, scope=[scope_lib.code])
    names = [item["name"] for item in outcome["result"]["items"]]
    assert names == ["范围内.md"]
    assert outcome["result"]["count"] == 1


def test_list_files_rejects_explicit_out_of_scope_library(
    user, make_library, make_file, grant_member
):
    scope_lib = make_library(code="scope-lib", name="范围库")
    other_lib = make_library(code="other-lib", name="范围外库")
    grant_member(scope_lib, user)
    grant_member(other_lib, user)
    operation = _arm(user, "list_files", {"library": other_lib.code}, scope=[scope_lib.code])
    with pytest.raises(services.PlannerError) as exc:
        services.execute_operation(user, operation)
    assert "不在本次检索范围" in str(exc.value)


def test_list_files_scope_accepts_numeric_library_id(
    user, make_library, make_file, grant_member
):
    """模型常把库写成数字 id（`_target_library_ids` 两种都认），范围比对也要认。"""
    scope_lib = make_library(code="scope-lib", name="范围库")
    grant_member(scope_lib, user)
    make_file(scope_lib, name="范围内.md")
    outcome = _run(
        user, "list_files", {"library": str(scope_lib.pk)}, scope=[scope_lib.code]
    )
    assert outcome["result"]["count"] == 1


def test_read_file_outside_scope_is_rejected(user, make_library, make_file, grant_member):
    scope_lib = make_library(code="scope-lib", name="范围库")
    other_lib = make_library(code="other-lib", name="范围外库")
    grant_member(scope_lib, user)
    grant_member(other_lib, user)
    outside = make_file(other_lib, name="范围外.pdf")
    operation = _arm(user, "read_file", {"file_id": outside.pk}, scope=[scope_lib.code])
    with pytest.raises(services.PlannerError) as exc:
        services.execute_operation(user, operation)
    assert "不在本次检索范围" in str(exc.value)
    assert "范围外.pdf" in str(exc.value)


def test_read_file_inside_scope_still_works(user, make_library, make_file, grant_member):
    scope_lib = make_library(code="scope-lib", name="范围库")
    grant_member(scope_lib, user)
    inside = make_file(scope_lib, name="范围内.pdf")
    outcome = _run(user, "read_file", {"file_id": inside.pk}, scope=[scope_lib.code])
    assert outcome["result"]["name"] == "范围内.pdf"


def test_compose_document_requires_explicit_source_selection(
    user, two_libraries, make_file
):
    """写文档不再"空选回落全库"（2026-10-22 契约变更）。

    旧行为：没指定来源 → 按主题检索全部可读库（见 2026-10-19 交付文档）。
    现在：确认/执行都要求至少一份具体参考文件；空选直接拒绝，绝不悄悄放宽到全库。
    """
    scope_lib, _other = two_libraries
    make_file(scope_lib, name="范围内.md")
    with pytest.raises(services.PlannerError) as exc:
        _arm(
            user,
            "compose_document",
            {"title": "出行报告", "topic": "陶鑫宇出差行程"},
            scope=[scope_lib.code],
        )
    assert "参考文件" in str(exc.value)


def test_compose_document_scoped_sources_pin_the_library_scope(
    user, two_libraries, monkeypatch, make_file
):
    """选了范围内的资料时，撰写只能落在本次范围库上（不失为全库）。"""
    scope_lib, _other = two_libraries
    inside = make_file(scope_lib, name="范围内.md")
    captured: dict = {}

    def fake_compose(*args, **kwargs):
        captured.update(kwargs)
        return {"document_id": 1, "title": kwargs.get("title", "")}

    monkeypatch.setattr(services, "compose_document", fake_compose)
    _run(
        user,
        "compose_document",
        {"title": "出行报告", "topic": "陶鑫宇出差行程", "sources": [inside.pk]},
        scope=[scope_lib.code],
    )
    assert captured["library_codes"] == [scope_lib.code]


def test_compose_document_rejects_out_of_scope_sources(
    user, two_libraries, monkeypatch, make_file
):
    scope_lib, other_lib = two_libraries
    outside = make_file(other_lib, name="范围外.md")
    monkeypatch.setattr(
        services, "compose_document", lambda *args, **kwargs: {"document_id": 1}
    )
    operation = _arm(
        user,
        "compose_document",
        {"title": "出行报告", "topic": "出差", "sources": [outside.pk]},
        scope=[scope_lib.code],
    )
    with pytest.raises(services.PlannerError) as exc:
        services.execute_operation(user, operation)
    assert "不在本次检索范围" in str(exc.value)


# ============================================= 4) 界面/接口要能回显范围 ===


def test_operation_payload_exposes_scope_with_names(
    user, two_libraries, api
):
    """确认卡要显示「检索范围：库名」——只给 code 对不上界面上的库名。"""
    scope_lib, _other = two_libraries
    proposal = services.propose_operation(
        user,
        intent="search_knowledge",
        params={"query": "出差"},
        conversation=_conversation(user),
        library_scope=[scope_lib.code],
    )
    api.force_authenticate(user=user)
    response = api.get("/api/v1/planner/operations/")
    assert response.status_code == 200, response.content[:300]
    rows = {row["id"]: row for row in response.json()["results"]}
    body = rows[proposal["operation_id"]]
    assert body["library_scope"] == [scope_lib.code]
    assert body["library_scope_labels"] == [scope_lib.name]


def test_scope_prompt_tells_the_model_about_the_range(user, library, grant_member, monkeypatch):
    """范围要写进 system 提示：模型知道范围，才不会提案去范围外检索。"""
    grant_member(library, user)
    monkeypatch.setattr(
        services,
        "knowledge_search",
        lambda **kwargs: {
            "results": [
                {"file_id": 1, "chunk_ordinal": 0, "text": "资料片段", "score": 0.9}
            ],
            "report": {},
        },
    )
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return _tool_call_reply("search_knowledge", {"query": "出差"})

    services.reply_to_message(
        user,
        _conversation(user),
        "谁出差了?",
        client=_fake_llm(handler),
        library_codes=[library.code],
    )
    system = captured["body"]["messages"][0]["content"]
    assert system.startswith("你是知识库系统的助手"), "system 必须在最前且唯一"
    assert "[本次检索范围]" in system
    assert library.name in system
    assert library.code in system
