"""「搜索知识库」不再要用户点确认（2026-10-19 用户要求）。

原行为：模型提议 ``search_knowledge`` → 界面弹一张"待确认卡" → 用户点「继续确认」
→ 再点「执行」→ 才看到结果。纯只读检索走这套纯属摩擦，用户明确说：
**搜索知识库不需要用户确认**。

现在：``OperationSpec.auto_execute`` 只对纯只读意图打开（目前只有搜索）。
提案即执行 —— 但仍然完整记账：

1. ``OperationRecord`` 照旧落库，状态走到 ``executed``，结果写在 ``result``；
2. 审计事件、权限复核、一次性凭证核销全部沿用原流程（"不用人点"≠"不记账"）;
3. 失败（越界/后端不可用/参数问题）如实落成 ``failed`` + error 并抛给调用方，
   绝不假装成功；
4. 写操作与删除类操作**不受影响**：它们照旧要确认（这里是不能被放松的边界）。
"""

from __future__ import annotations

import json

import httpx
import pytest

from apps.planner import services
from apps.planner.llm import LlmClient, LlmProfile
from apps.planner.models import Conversation, Message, OperationRecord
from apps.planner.operations import all_specs, get as get_spec, is_mutation

pytestmark = pytest.mark.django_db


# ------------------------------------------------------------------ 工具 ----


def _fake_llm(handler) -> LlmClient:
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
                                "function": {"name": name, "arguments": json.dumps(arguments)},
                            }
                        ],
                    }
                }
            ],
            "usage": {"total_tokens": 12},
        },
    )


def _fake_search(**kwargs):
    return {
        "results": [{"file_id": 1, "chunk_ordinal": 0, "text": "出差行程", "score": 0.9}],
        "report": {"hits": 1},
    }


@pytest.fixture
def search(monkeypatch):
    """把向量检索换成假实现：用例测的是"要不要确认"，不是向量库。"""
    monkeypatch.setattr(services, "knowledge_search", _fake_search)


def _search_once(user, *, scope=None, params=None) -> dict:
    return services.propose_operation(
        user,
        intent="search_knowledge",
        params=params or {"query": "出差"},
        conversation=Conversation.objects.create(user=user),
        library_scope=scope,
    )


# ============================================= 1) 登记表：只有只读意图免确认 ===


def test_only_search_knowledge_is_exempt_from_confirmation():
    """免确认是**逐意图**打开的白名单，不能被顺手扩散到写操作。"""
    assert get_spec("search_knowledge").auto_execute is True
    # 其它只读意图仍走确认（用户只要求了"搜索知识库"）
    for intent in ("list_files", "read_file", "get_stats", "runtime_metrics"):
        assert get_spec(intent).auto_execute is False, intent


def test_auto_execute_specs_are_never_mutations_or_destructive():
    """不变量：免确认只能给"不写库、不可破坏"的意图。"""
    for spec in all_specs():
        if not spec.auto_execute:
            continue
        assert spec.requires_destructive_flow is False, spec.intent
        assert not is_mutation(spec.intent), spec.intent
        assert spec.risk == "low", spec.intent


# =============================================== 2) 提案即执行（真链路） ===


def test_propose_search_executes_immediately(user, library, grant_member, search):
    """一次 ``propose_operation`` 就能拿到结果：没有待确认卡这一站。"""
    grant_member(library, user)
    proposal = _search_once(user, scope=[library.code])

    assert proposal["status"] == OperationRecord.Status.EXECUTED
    assert proposal["auto_executed"] is True
    # 凭证已核销：不能再把能用的 token 回给前端
    assert proposal["confirm_token"] == ""
    assert proposal["result"]["count"] >= 0
    assert proposal["result"]["library_codes"] == [library.code]

    operation = OperationRecord.objects.get(pk=proposal["operation_id"])
    assert operation.status == OperationRecord.Status.EXECUTED
    assert operation.confirmations == 1  # 记了账：那一次确认是系统代用户完成的
    assert operation.result == proposal["result"]
    assert operation.error == ""


def test_search_result_is_visible_without_any_confirm_step(user, library, grant_member, search):
    """界面上不应再出现"待确认/待执行"的搜索卡 —— 列表里直接就是已完成。"""
    grant_member(library, user)
    proposal = _search_once(user)
    row = OperationRecord.objects.get(pk=proposal["operation_id"])
    assert row.status not in {
        OperationRecord.Status.PROPOSED,
        OperationRecord.Status.ARMED,
        OperationRecord.Status.CONFIRMED_1,
    }


def test_confirming_an_auto_executed_search_is_refused(user, library, grant_member, search):
    """旧入口（点确认/执行）对已完成的搜索必须明确拒绝，而不是重复检索一次。"""
    grant_member(library, user)
    proposal = _search_once(user)
    operation = OperationRecord.objects.get(pk=proposal["operation_id"])
    with pytest.raises(services.PlannerError) as exc:
        services.confirm_operation(user, operation, token="any-token")
    assert "已结束" in str(exc.value)
    with pytest.raises(services.PlannerError) as exc:
        services.execute_operation(user, operation)
    assert "已结束" in str(exc.value) or "状态" in str(exc.value)


def test_writes_still_require_confirmation(admin_user, library, make_file):
    """边界：写操作照旧停在"待确认"，不会因为这次改动被自动执行。"""
    record = make_file(library=library, name="要归档.md")
    proposal = services.propose_operation(
        admin_user, intent="classify_file", params={"file_id": record.pk, "classification": "合同"}
    )
    assert proposal["status"] == OperationRecord.Status.PROPOSED
    assert proposal["required_confirmations"] == 1
    assert proposal["confirm_token"]


# ============================================= 3) 会话链路（模型发起检索） ===


def test_chat_tool_call_returns_executed_proposal(user, library, grant_member, search):
    """模型这轮就能把结果交给界面：``proposals[0]`` 已是 executed + result。"""
    grant_member(library, user)

    def handler(request: httpx.Request) -> httpx.Response:
        return _tool_call_reply("search_knowledge", {"query": "出差"})

    payload = services.reply_to_message(
        user,
        Conversation.objects.create(user=user),
        "谁出差了?",
        client=_fake_llm(handler),
    )
    proposal = payload["proposals"][0]
    assert proposal.get("rejected") is not True, proposal
    assert proposal["status"] == OperationRecord.Status.EXECUTED
    assert proposal["result"]["library_codes"] == [library.code]
    # 助手消息仍然写（正文为空时界面靠它解释"这一轮发起了什么"）
    assert Message.objects.filter(pk=payload["assistant_message_id"]).exists()
    # 没有留下"待确认"的搜索记录
    assert not OperationRecord.objects.filter(
        intent="search_knowledge", status=OperationRecord.Status.PROPOSED
    ).exists()


def test_chat_search_failure_is_reported_honestly(user, library, grant_member, monkeypatch):
    """检索后端报错 → 记录 failed + 原因，会话里如实显示（不伪造"已完成"）。"""
    grant_member(library, user)

    def broken(**kwargs):
        raise services.PlannerError("向量检索暂不可用")

    monkeypatch.setattr(services, "knowledge_search", broken)

    def handler(request: httpx.Request) -> httpx.Response:
        return _tool_call_reply("search_knowledge", {"query": "出差"})

    payload = services.reply_to_message(
        user,
        Conversation.objects.create(user=user),
        "谁出差了?",
        client=_fake_llm(handler),
    )
    proposal = payload["proposals"][0]
    assert proposal.get("rejected") is True
    assert "向量检索暂不可用" in proposal["reason"]
    failed = OperationRecord.objects.filter(intent="search_knowledge").first()
    assert failed is not None
    assert failed.status == OperationRecord.Status.FAILED
    assert "向量检索暂不可用" in (failed.error or "")
