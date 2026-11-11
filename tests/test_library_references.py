import json
import pytest

from apps.planner import services
from apps.planner.library_refs import resolve_read_library, LibraryReferenceError, visible_libraries
from apps.planner.models import Conversation, OperationRecord, Message
from tests.test_reply_tool_loop_and_card_lifecycle import _fake_llm, _response, _call_payload, _text_payload

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("ref", ["合同资料库", "合同档案库", "合同库", "合同", " contract-archive "])
def test_contract_alias_lists_only_actual_archive(user, make_library, make_file, grant_member, ref):
    archive = make_library(code="contract-archive", name="合同档案库")
    other = make_library(code="contract-test", name="测试库")
    grant_member(archive, user)
    grant_member(other, user)
    file = make_file(archive)
    make_file(other)
    result = services.propose_operation(user, intent="list_files", params={"library": ref},
        conversation=Conversation.objects.create(user=user))
    assert result["params"]["library"] == archive.code
    assert result["result"]["count"] == 1
    assert [r["id"] for r in result["result"]["items"]] == [file.pk]


def test_ambiguous_business_names_require_selection(user, make_library, grant_member):
    for code, name in [("purchase", "采购合同档案库"), ("sales", "销售合同资料库")]:
        grant_member(make_library(code=code, name=name), user)
    with pytest.raises(LibraryReferenceError, match="名称不明确") as error:
        resolve_read_library(user, "合同资料库")
    assert not error.value.retryable
    assert "采购合同档案库" in str(error.value) and "销售合同资料库" in str(error.value)


def test_alias_catalog_cannot_escape_scope_or_disclose_hidden_names(user, make_library, grant_member):
    own = make_library(code="contracts", name="合同档案库")
    hidden = make_library(code="secret", name="隐秘合同资料库")
    outside = make_library(code="outside", name="结算资料库")
    grant_member(own, user)
    grant_member(outside, user)
    assert list(visible_libraries(user, [own.code])) == [own]
    assert resolve_read_library(user, "合同资料库", [own.code]) == own.code
    with pytest.raises(LibraryReferenceError) as error:
        resolve_read_library(user, "隐秘合同资料库", [own.code])
    assert hidden.code not in str(error.value)
    assert outside.code not in str(error.value)
    with pytest.raises(LibraryReferenceError):
        resolve_read_library(user, "合同资料库", [outside.code])


def test_read_alias_does_not_redirect_deletion(user, make_library, grant_member):
    archive = make_library(code="contract-archive", name="合同档案库")
    grant_member(archive, user, "admin")
    with pytest.raises(services.PlannerError):
        services.propose_operation(user, intent="delete_library", params={"library": "合同资料库"},
            conversation=Conversation.objects.create(user=user))
    assert not OperationRecord.objects.exists()


def test_exact_name_outside_scope_is_not_redirected_to_a_similar_inside_name(user, make_library, grant_member):
    outside = make_library(code="outside", name="合同档案库")
    inside = make_library(code="inside", name="合同资料库")
    for row in [inside, outside]:
        grant_member(row, user)
    with pytest.raises(services.PlannerError, match="不在本次检索范围"):
        services.propose_operation(user, intent="list_files", params={"library": outside.name},
            conversation=Conversation.objects.create(user=user), library_scope=[inside.code])


def test_wrong_asr_library_can_be_corrected_using_real_directory(user, make_library, make_file, grant_member, monkeypatch, settings):
    settings.KB_RAG_VERIFY_SEMANTIC = False
    archive = make_library(code="contract-archive", name="合同档案库")
    grant_member(archive, user)
    make_file(archive)
    monkeypatch.setattr(services, "build_context_report", lambda *a, **k: {
        "context": "", "degraded": False, "degraded_reason": "", "searched_libraries": [], "failed_collections": []})
    seen = []
    def handler(request):
        data = json.loads(request.content)
        seen.append(data)
        if len(seen) == 1:
            assert archive.code in data["messages"][0]["content"]
            return _response(_call_payload("list_files", {"library": "文达"}, "bad"))
        if len(seen) == 2:
            assert data["messages"][-1]["role"] == "tool"
            assert archive.code in data["messages"][-1]["content"]
            return _response(_call_payload("list_files", {"library": "合同资料库"}, "fixed"))
        import re
        citation = re.findall(r'T[a-f0-9]{20}', str(data["messages"]))[-1]
        return _response(_text_payload(f"已找到合同档案库，共 1 份资料。[{citation}]"))
    result = services.reply_to_message(user, Conversation.objects.create(user=user),
        "合同资料库一共有多少份资料？", client=_fake_llm(handler))
    assert len(seen) == 3
    assert OperationRecord.objects.get().params["library"] == archive.code
    assert Message.objects.get(pk=result["assistant_message_id"]).payload["rejected_proposals"] == []
