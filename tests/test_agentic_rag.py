"""Research behavior against real isolated SQL sources and a scripted model protocol.

These tests verify orchestration and source integrity, not real model intelligence.
"""
import json
import httpx
import pytest
from django.test import override_settings
from django.urls import reverse
from apps.knowledge.models import DocumentChunk
from apps.knowledge.request_budget import deadline, remaining
from apps.knowledge.resilience import CircuitBreaker, ServiceUnavailable, post_json
from apps.planner import services
from apps.planner.models import Conversation, Message, OperationRecord
from apps.planner.rag.contracts import Evidence
from apps.planner.rag.reader import read_passages, enumerate_files
from apps.planner.rag.retrieval import attach_evidence
from apps.planner.rag.calculator import EvidenceCalculator
from apps.planner.rag.modules import module
from apps.planner.rag.runtime import ResearchRun, choose_mode
from apps.planner.rag.verifier import EvidenceVerifier
from tests.test_reply_tool_loop_and_card_lifecycle import _fake_llm, _response, _call_payload, _text_payload

pytestmark = pytest.mark.django_db


@pytest.fixture
def source(user, library, grant_member, make_file):
    grant_member(library, user)
    record = make_file(library, name="示例合同.md")
    chunks = [DocumentChunk.objects.create(file=record, ordinal=i, page=i + 1,
              text=text, permission_code=f"library:{library.pk}") for i, text in enumerate([
                  "合同编号 TEST-001。合同总额 100.10 万元。", "付款期限为验收后 30 天。",
                  "补充条款：服务期限延长 12 月。"])]
    return record, chunks


def _retrieval(monkeypatch, chunks):
    calls = []
    def search(**kwargs):
        calls.append(kwargs)
        return {"results": [{"file_id": c.file_id, "ordinal": c.ordinal, "page": c.page,
                 "text": c.text, "original_name": c.file.original_name} for c in chunks], "report": {}}
    monkeypatch.setattr(services, "knowledge_search", search)
    return calls


def test_reader_continuation_does_not_lose_tail(user, source):
    record, chunks = source
    first = read_passages(user, file_id=record.pk, limit=1)
    assert first["has_more"] and first["next_cursor"]["start_ordinal"] == 1
    second = read_passages(user, file_id=record.pk, **first["next_cursor"])
    assert not second["has_more"]
    assert [x["ordinal"] for x in first["items"] + second["items"]] == [0, 1, 2]
    assert first["items"][0]["version"] == record.content_sha256


def test_long_paragraph_can_be_read_without_silent_truncation(user, library, grant_member, make_file):
    grant_member(library, user)
    file = make_file(library)
    text = "首" * 1500 + "尾部关键事实"
    DocumentChunk.objects.create(file=file, ordinal=0, text=text)
    with override_settings(KB_RAG_READ_CHARS=1024):
        a = read_passages(user, file_id=file.pk)
        b = read_passages(user, file_id=file.pk, **a["next_cursor"])
    assert a["items"][0]["truncated"]
    assert a["items"][0]["text"] + b["items"][0]["text"] == text
    assert not b["has_more"]


def test_reader_checks_scope_permission_and_version(user, source, make_library):
    file, _ = source
    other = make_library(code="outside")
    with pytest.raises(services.PlannerError):
        read_passages(user, file_id=file.pk, scope=[other.code])
    with pytest.raises(services.PlannerError, match="版本"):
        read_passages(user, file_id=file.pk, expected_version="wrong")


def test_manifest_reports_pagination_and_complete_count(user, source, make_file, library):
    make_file(library, name="补充协议.md")
    a = enumerate_files(user, limit=1)
    b = enumerate_files(user, limit=1, after_id=a["next_after_id"])
    assert a["count"] == b["count"] == 2
    assert a["has_more"] and not b["has_more"]
    assert a["items"][0]["id"] != b["items"][0]["id"]


def test_stale_vector_text_cannot_become_a_citation(user, source):
    file, chunks = source
    assert attach_evidence(user, [{"file_id": file.pk, "ordinal": 0, "text": "旧的错误正文"}]) == []
    good = attach_evidence(user, [{"file_id": file.pk, "ordinal": 0, "text": chunks[0].text}])
    assert good[0]["file_id"] == file.pk


def test_calculation_is_exact_and_requires_adjacent_matching_unit(user, source):
    file, chunks = source
    item = {"file_id": file.pk, "ordinal": 0, "version": file.content_sha256,
            "value": "100.10", "quote": chunks[0].text}
    result = EvidenceCalculator().calculate(user, operation="sum", items=json.dumps([item]), unit="万元")
    assert result["value"] == "100.10"
    assert result["coverage"] == "provided_operands_only"
    with pytest.raises(services.PlannerError, match="相邻"):
        EvidenceCalculator().calculate(user, operation="sum", items=json.dumps([item]), unit="元")
    with pytest.raises(services.PlannerError, match="重复"):
        EvidenceCalculator().calculate(user, operation="sum", items=json.dumps([item, item]), unit="万元")


def test_calculation_does_not_execute_expressions(user, source):
    file, chunks = source
    item = {"file_id": file.pk, "ordinal": 0, "version": file.content_sha256,
            "value": "__import__('os')", "quote": chunks[0].text}
    with pytest.raises(services.PlannerError, match="表达式"):
        EvidenceCalculator().calculate(user, operation="sum", items=json.dumps([item]), unit="万元")


def test_verifier_rejects_fabricated_source_and_renders_only_server_url(user, source):
    _, chunks = source
    e = Evidence.from_chunk(chunks[0]).to_dict()
    verifier = EvidenceVerifier()
    result = verifier.validate(user, "错误来源 [E" + "f" * 20 + "]", [e])
    assert not result["valid"] and result["unknown"]
    text = verifier.render(f"金额 [{e['evidence_id']}](https://evil.example)", [e])
    assert "evil.example" not in text
    assert e["url"] in text


def test_source_view_checks_auth_version_and_escapes_text(api, user, source):
    file, chunks = source
    e = Evidence.from_chunk(chunks[0]).to_dict()
    assert api.get(e["url"]).status_code in {401, 403}
    api.force_authenticate(user=user)
    response = api.get(e["url"])
    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]
    assert api.get(e["url"].replace(file.content_sha256, "bad")).status_code == 409


def test_research_reads_original_then_returns_persistent_citation(user, source, monkeypatch):
    file, chunks = source
    _retrieval(monkeypatch, chunks[:1])
    key = Evidence.from_chunk(chunks[1]).evidence_id
    seen = []
    def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        if len(seen) == 1:
            return _response(_call_payload("read_passages", {"file_id": file.pk, "start_ordinal": 1, "limit": 1}))
        assert "付款期限" in payload["messages"][-1]["content"]
        return _response(_text_payload(f"付款期限为验收后30天。[{key}]"))
    with override_settings(KB_RAG_VERIFY_SEMANTIC=False):
        result = services.reply_to_message(user, Conversation.objects.create(user=user),
            "合同付款期限是什么？", client=_fake_llm(handler))
    assert "/api/v1/planner/evidence/" in result["content"]
    assert result["research"]["verification"]["valid"]
    assert result["research"]["coverage"]["read_file_ids"] == [file.pk]
    message = Message.objects.get(pk=result["assistant_message_id"])
    assert message.payload["research"] == result["research"]


def test_missing_citation_triggers_repair_not_unchecked_publication(user, source, monkeypatch):
    _, chunks = source
    _retrieval(monkeypatch, chunks[:1])
    key = Evidence.from_chunk(chunks[0]).evidence_id
    seen = []
    def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        if len(seen) == 1:
            return _response(_text_payload("合同总额100.10万元。"))
        assert "核验" in payload["messages"][-1]["content"]
        return _response(_text_payload(f"合同总额100.10万元。[{key}]"))
    with override_settings(KB_RAG_VERIFY_SEMANTIC=False):
        result = services.reply_to_message(user, Conversation.objects.create(user=user),
                 "合同总额？", client=_fake_llm(handler))
    assert len(seen) == 2 and result["research"]["verification"]["valid"]


def test_semantic_failure_is_repaired_and_checked_again(user, source, monkeypatch):
    _, chunks = source
    _retrieval(monkeypatch, chunks[:1])
    key = Evidence.from_chunk(chunks[0]).evidence_id
    responses = [f"合同总额200万元。[{key}]", json.dumps({"supported": False, "issues": ["金额不符"], "missing": []}),
                 f"合同总额100.10万元。[{key}]", json.dumps({"supported": True, "issues": [], "missing": []})]
    def handler(request):
        return _response(_text_payload(responses.pop(0)))
    result = services.reply_to_message(user, Conversation.objects.create(user=user),
             "合同总额？", client=_fake_llm(handler))
    assert not responses
    assert result["research"]["verification"]["semantic"] == "supported"
    assert "200万元" not in result["content"]


def test_persistent_bad_semantics_are_not_published(user, source, monkeypatch):
    _, chunks = source
    _retrieval(monkeypatch, chunks[:1])
    key = Evidence.from_chunk(chunks[0]).evidence_id
    def handler(request):
        payload = json.loads(request.content)
        if "tools" in payload:
            return _response(_text_payload(f"总额200万元。[{key}]"))
        return _response(_text_payload(json.dumps({"supported": False, "issues": ["金额不符"], "missing": []})))
    result = services.reply_to_message(user, Conversation.objects.create(user=user), "合同总额？", client=_fake_llm(handler))
    assert result["research"]["stop_reason"] == "unsupported_answer"
    assert "200万元" not in result["content"]


def test_tool_budget_blocks_extra_operations(user, source, monkeypatch):
    file, chunks = source
    _retrieval(monkeypatch, [])
    seen = []
    def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        if "tools" in payload:
            return _response(_call_payload("read_passages", {"file_id": file.pk, "start_ordinal": len(seen) - 1, "limit": 1}))
        return _response(_text_payload("无法确认全部条款，资料不足。"))
    with override_settings(KB_RAG_MAX_TOOL_CALLS=1, KB_RAG_VERIFY_SEMANTIC=False):
        result = services.reply_to_message(user, Conversation.objects.create(user=user), "比较合同条款", client=_fake_llm(handler))
    assert OperationRecord.objects.filter(intent="read_passages").count() == 1
    assert result["research"]["budget"]["tool_calls"] == 1


def test_statistics_cannot_treat_last_page_as_full_manifest(user, source):
    run = ResearchRun(user, "统计全部合同")
    run.observe("list_files", {"filters": {}, "count": 5, "after_id": 50, "items": [{"id": 51}], "has_more": False})
    assert not run.receipt()["coverage"]["manifests"][0]["complete"]


def test_query_cannot_invoke_unoffered_delete(user, source, monkeypatch):
    file, _ = source
    _retrieval(monkeypatch, [])
    def handler(request):
        return _response(_call_payload("delete_file", {"file_id": file.pk}))
    result = services.reply_to_message(user, Conversation.objects.create(user=user), "合同付款期限？", client=_fake_llm(handler))
    assert result["proposals"][0]["rejected"]
    assert not OperationRecord.objects.filter(intent="delete_file").exists()


def test_modules_can_be_swapped_without_changing_services(settings):
    settings.KB_RAG_MODULES = {"reader": "tests.test_agentic_rag.FakeReader"}
    assert module("reader").read(None) == {"adapter": "replacement"}


class FakeReader:
    def read(self, user):
        return {"adapter": "replacement"}


@pytest.mark.parametrize("question,expected", [
    ("付款期限是多少？", "fast"), ("对比去年合同", "deep"), ("统计全部合同金额", "statistics")])
def test_routing(question, expected):
    assert choose_mode(question) == expected


def test_http_deadline_is_request_local_and_stops_before_network():
    import time
    token = deadline.set(time.monotonic() - 1)
    calls = []
    try:
        with httpx.Client(transport=httpx.MockTransport(lambda r: calls.append(r))) as client:
            with pytest.raises(ServiceUnavailable, match="预算"):
                post_json(http=client, url="http://llm.test", payload={}, service="test",
                          breaker=CircuitBreaker("test"), max_attempts=1)
        assert not calls
    finally:
        deadline.reset(token)
    assert remaining() is None


def test_api_rejects_invalid_mode(api, user):
    api.force_authenticate(user=user)
    response = api.post(reverse("planner:chat"), {"message": "你好", "research_mode": []}, format="json")
    assert response.status_code == 400


def test_manifest_count_has_its_own_audited_citation(user, source, api):
    proposal = services.propose_operation(user, intent="list_files", params={})
    evidence = proposal["result"]["evidence"][-1]
    assert evidence["evidence_id"].startswith("T")
    check = EvidenceVerifier().validate(user, f"共1份。[{evidence['evidence_id']}]", [evidence])
    assert check["valid"]
    api.force_authenticate(user=user)
    assert api.get(evidence["url"]).status_code == 200


def test_manifest_count_checks_revoked_access_beyond_current_page(
    user, source, api, make_library, make_file, grant_member,
):
    first, _ = source
    private = make_library(code="private-manifest")
    membership = grant_member(private, user)
    make_file(private)
    proposal = services.propose_operation(user, intent="list_files", params={"limit": 1})
    operation_id = proposal["operation_id"]
    result = proposal["result"]
    assert result["count"] == 2
    assert [row["id"] for row in result["items"]] == [first.pk]
    url = result["evidence"][-1]["url"]
    api.force_authenticate(user=user)
    assert api.get(url).status_code == 200
    operations_url = "/api/v1/planner/operations/"
    progress_url = f"{operations_url}progress/?ids={operation_id}"
    assert api.get(operations_url).data["results"][0]["result"]["count"] == 2
    assert api.get(progress_url).data["results"][0]["result"]["count"] == 2
    membership.delete()
    assert api.get(url).status_code == 404
    assert api.get(operations_url).data["results"][0]["result"] == {}
    assert api.get(progress_url).data["results"][0]["result"] == {}


def test_unverified_index_text_is_not_sent_to_agent(user, source, monkeypatch):
    file, _ = source
    monkeypatch.setattr(services, "knowledge_search", lambda **kw: {"results": [
        {"file_id": file.pk, "ordinal": 0, "text": "不应该送达的旧正文"}], "report": {}})
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        return _response(_text_payload("没有找到足够依据。"))
    services.reply_to_message(user, Conversation.objects.create(user=user), "合同条款？", client=_fake_llm(handler))
    assert "不应该送达的旧正文" not in json.dumps(seen, ensure_ascii=False)


def test_full_content_statistics_require_reading_manifest_files(user, source):
    file, _ = source
    run = ResearchRun(user, "统计全部合同的总额")
    run.observe("list_files", {"filters": {}, "count": 1, "after_id": 0, "items": [{"id": file.pk}], "has_more": False})
    run.assess(None, "没有找到足够依据。")
    assert run.coverage_issues
    assert "暂不发布全量结论" in run.finish("合计100万元。")


def test_file_count_can_render_authoritative_tool_fact(user, source):
    proposal = services.propose_operation(user, intent="list_files", params={})
    run = ResearchRun(user, "统计文件数量")
    run.observe("list_files", proposal["result"])
    with override_settings(KB_RAG_VERIFY_SEMANTIC=False):
        run.assess(None, "文件数量是错误的50份。")
        answer = run.finish("文件数量是错误的50份。")
    assert "共 1 份资料" in answer and "50份" not in answer
    assert "/research-record/" in answer
