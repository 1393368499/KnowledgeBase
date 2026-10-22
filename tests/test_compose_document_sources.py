"""参考文件选择：支持全选、持久化、授权校验，至少选一份才能确认或执行。"""

from __future__ import annotations

import pytest

from apps.knowledge.models import ParseStatus
from apps.planner import services
from apps.planner.models import OperationRecord


def _propose(user, *, scope=None):
    payload = services.propose_operation(
        user,
        intent="compose_document",
        params={"title": "送货说明", "topic": "质保"},
        library_scope=scope,
    )
    return payload, OperationRecord.objects.get(pk=payload["operation_id"])


def _card_row(api, user, operation_id):
    api.force_authenticate(user=user)
    listing = api.get("/api/v1/planner/operations/")
    assert listing.status_code == 200, listing.content[:300]
    return next(
        item for item in listing.json()["results"] if item["id"] == operation_id
    )


def test_card_offers_candidates_only_from_the_current_library(
    api, user, library, make_library, make_file, grant_member
):
    """候选列表只来自**当前所在库**，范围外的资料不出现。"""
    other = make_library(code="other-lib", name="别的库")
    grant_member(library, user)
    grant_member(other, user)
    wanted = make_file(library, "出差申请.pdf")
    make_file(other, "不该出现的资料.pdf")

    _payload, operation = _propose(user, scope=[library.code])
    row = _card_row(api, user, operation.pk)

    assert row["source_file_ids"] == []
    names = [item["name"] for item in row["source_candidates"]]
    assert "出差申请.pdf" in names
    assert "不该出现的资料.pdf" not in names
    # 候选里带上库名，卡片上才说得清"这份资料来自哪个库"
    matched = next(item for item in row["source_candidates"] if item["id"] == wanted.pk)
    assert matched["library"] == library.name


def test_candidates_skip_unprocessed_files(api, user, library, make_file, grant_member):
    grant_member(library, user)
    make_file(library, "还没处理完.pdf", status=ParseStatus.PENDING)
    make_file(library, "已处理完.pdf")

    _payload, operation = _propose(user, scope=[library.code])
    row = _card_row(api, user, operation.pk)
    names = [item["name"] for item in row["source_candidates"]]
    assert names == ["已处理完.pdf"]


def test_set_sources_persists_and_cannot_be_cleared(api, user, library, make_file, grant_member):
    grant_member(library, user)
    first = make_file(library, "第一份.pdf")
    second = make_file(library, "第二份.pdf")
    _payload, operation = _propose(user, scope=[library.code])

    api.force_authenticate(user=user)
    picked = api.post(
        f"/api/v1/planner/operations/{operation.pk}/sources/",
        {"file_ids": [second.pk, first.pk]},
        content_type="application/json",
    )
    assert picked.status_code == 200, picked.content[:300]
    body = picked.json()
    assert body["source_file_ids"] == [second.pk, first.pk]  # 保持用户勾选顺序
    assert "已选参考文件" in body["message"]
    assert [item["name"] for item in body["sources"]] == ["第二份.pdf", "第一份.pdf"]

    operation.refresh_from_db()
    # 参数校验器把来源统一规范成字符串（与模型调用时同一口径），顺序保持
    assert operation.params["sources"] == [str(second.pk), str(first.pk)]

    # 卡片回显：勾了哪几份要能看出来（刷新/翻历史都还在）
    row = _card_row(api, user, operation.pk)
    assert row["source_file_ids"] == [second.pk, first.pk]

    cleared = api.post(
        f"/api/v1/planner/operations/{operation.pk}/sources/",
        {"file_ids": []},
        content_type="application/json",
    )
    assert cleared.status_code == 400
    assert "至少选择一份" in cleared.json()["detail"]
    operation.refresh_from_db()
    assert operation.params["sources"] == [str(second.pk), str(first.pk)]
    assert operation.status == OperationRecord.Status.PROPOSED


def test_set_sources_rejects_files_outside_the_scope(
    api, user, library, make_library, make_file, grant_member
):
    other = make_library(code="other-lib", name="别的库")
    grant_member(library, user)
    grant_member(other, user)
    outside = make_file(other, "范围外.pdf")
    _payload, operation = _propose(user, scope=[library.code])

    api.force_authenticate(user=user)
    response = api.post(
        f"/api/v1/planner/operations/{operation.pk}/sources/",
        {"file_ids": [outside.pk]},
        content_type="application/json",
    )
    assert response.status_code == 400, response.content[:300]
    assert "不在本次检索范围内" in response.json()["detail"]
    operation.refresh_from_db()
    assert "sources" not in operation.params


def test_set_sources_rejects_unprocessed_file(api, user, library, make_file, grant_member):
    grant_member(library, user)
    pending = make_file(library, "未完成.pdf", status=ParseStatus.PENDING)
    _payload, operation = _propose(user, scope=[library.code])

    api.force_authenticate(user=user)
    response = api.post(
        f"/api/v1/planner/operations/{operation.pk}/sources/",
        {"file_ids": [pending.pk]},
        content_type="application/json",
    )
    assert response.status_code == 400, response.content[:300]
    assert "还没有处理完成" in response.json()["detail"]


def test_set_sources_rejects_unknown_and_non_integer(api, user, library, make_file, grant_member):
    grant_member(library, user)
    make_file(library, "参考.pdf")
    _payload, operation = _propose(user, scope=[library.code])

    api.force_authenticate(user=user)
    unknown = api.post(
        f"/api/v1/planner/operations/{operation.pk}/sources/",
        {"file_ids": [987654]},
        content_type="application/json",
    )
    assert unknown.status_code == 404
    assert "参考文件不存在" in unknown.json()["detail"]

    non_integer = api.post(
        f"/api/v1/planner/operations/{operation.pk}/sources/",
        {"file_ids": ["abc"]},
        content_type="application/json",
    )
    assert non_integer.status_code == 400
    assert "资料 id" in non_integer.json()["detail"]


def test_set_sources_locked_once_armed(api, user, library, make_file, grant_member):
    """已经确认进入执行窗口的待办不能再改参考文件：改了也不会被执行，那才是骗人。"""
    grant_member(library, user)
    source = make_file(library, "参考.pdf")
    proposal, operation = _propose(user, scope=[library.code])
    services.set_operation_sources(user, operation, [source.pk])
    services.confirm_operation(user, operation, token=proposal["confirm_token"])

    api.force_authenticate(user=user)
    response = api.post(
        f"/api/v1/planner/operations/{operation.pk}/sources/",
        {"file_ids": [source.pk]},
        content_type="application/json",
    )
    assert response.status_code == 409, response.content[:300]
    assert "不能再改参考文件" in response.json()["detail"]


def test_set_sources_rejects_other_intents(user, library, make_file, grant_member):
    grant_member(library, user)
    source = make_file(library, "参考.pdf")
    payload = services.propose_operation(
        user,
        intent="list_files",
        params={"library": library.code},
    )
    operation = OperationRecord.objects.get(pk=payload["operation_id"])
    with pytest.raises(services.PlannerError, match="只有「编写新文档」"):
        services.set_operation_sources(user, operation, [source.pk])


def test_execute_uses_the_selected_reference_files(
    monkeypatch, user, library, make_file, grant_member
):
    """勾了参考文件 → 执行时**只**用这些资料（不再按主题全库检索）。"""
    grant_member(library, user)
    source = make_file(library, "出差申请.pdf")
    proposal, operation = _propose(user, scope=[library.code])
    services.set_operation_sources(user, operation, [source.pk])
    services.confirm_operation(user, operation, token=proposal["confirm_token"])

    captured: dict = {}

    def _fake_compose(*args, **kwargs):
        captured.update(kwargs)
        return {"document_id": 1, "source_file_ids": list(kwargs.get("source_file_ids") or [])}

    monkeypatch.setattr(services, "compose_document", _fake_compose)
    result = services.execute_operation(user, operation)

    assert result["status"] == OperationRecord.Status.EXECUTED
    assert captured["source_file_ids"] == [source.pk]
    # 勾了库时库范围也一并带上（两者是"与"关系：库必须对，资料必须对）
    assert captured["library_codes"] == [library.code]


def test_empty_selection_blocks_confirmation_without_consuming_ticket(
    monkeypatch, user, library, make_file, grant_member
):
    """空选不能确认，也不能调用撰写；选完后原确认凭证仍可正常使用。"""
    grant_member(library, user)
    source = make_file(library, "参考.pdf")
    proposal, operation = _propose(user)  # 没有勾库 = 范围不限
    with pytest.raises(services.PlannerError, match="至少选择一份"):
        services.confirm_operation(user, operation, token=proposal["confirm_token"])
    operation.refresh_from_db()
    assert operation.status == OperationRecord.Status.PROPOSED
    assert operation.confirmations == 0

    captured: dict = {}

    def _fake_compose(*args, **kwargs):
        captured.update(kwargs)
        return {"document_id": 2}

    monkeypatch.setattr(services, "compose_document", _fake_compose)
    services.set_operation_sources(user, operation, [source.pk])
    services.confirm_operation(user, operation, token=proposal["confirm_token"])
    result = services.execute_operation(user, operation)

    assert result["status"] == OperationRecord.Status.EXECUTED
    assert captured["source_file_ids"] == [source.pk]
    assert captured["library_codes"] is None


# ---------------------------------------------------------------------------
# 6. 模型把"没选"写成空串时，不能因此枪毙整张待确认卡
# ---------------------------------------------------------------------------


def test_blank_optional_params_are_treated_as_absent(user):
    """生产实测（2026-10-19 11:1x）：模型提案 `{"template_id": ""}` 被 int 校验拒掉，
    用户侧什么卡都看不到。空串/纯空白对可选参数等价于"没传"，应被摘掉。"""
    payload = services.propose_operation(
        user,
        intent="compose_document",
        params={
            "title": "出差情况说明",
            "topic": "出差",
            "sources": [],
            "template_id": "",
        },
    )
    operation = OperationRecord.objects.get(pk=payload["operation_id"])
    assert payload["status"] == OperationRecord.Status.PROPOSED
    assert "template_id" not in operation.params
    assert operation.params["title"] == "出差情况说明"

    # 纯空白同样处理
    whitespace = services.propose_operation(
        user,
        intent="compose_document",
        params={"title": "出差情况说明", "topic": "出差", "template_id": "   "},
    )
    assert "template_id" not in whitespace["params"]

    # 真正的非法值仍然拒绝：不能借这条放宽把"随便什么"都放过去
    with pytest.raises(services.PlannerError, match="必须是整数"):
        services.propose_operation(
            user,
            intent="compose_document",
            params={"title": "出差情况说明", "topic": "出差", "template_id": "abc"},
        )


@pytest.mark.parametrize('selection', [[], None, '', (), set()])
def test_empty_source_update_is_rejected_without_changing_saved_selection(user, library, make_file, grant_member, selection):
    grant_member(library, user)
    source = make_file(library, '参考.pdf')
    _, operation = _propose(user)
    services.set_operation_sources(user, operation, [source.pk])
    with pytest.raises(services.PlannerError, match='至少选择一份'):
        services.set_operation_sources(user, operation, selection)
    operation.refresh_from_db()
    assert operation.params['sources'] == [str(source.pk)]


def test_legacy_armed_empty_operation_cannot_execute(user, monkeypatch):
    from django.utils import timezone
    _, operation = _propose(user)
    OperationRecord.objects.filter(pk=operation.pk).update(status='armed', confirmations=1,
        armed_expires_at=timezone.now()+timezone.timedelta(minutes=5))
    operation.refresh_from_db()
    calls = []
    monkeypatch.setattr(services, '_dispatch', lambda *args: calls.append(True))
    with pytest.raises(services.PlannerError, match='至少选择一份'):
        services.execute_operation(user, operation)
    operation.refresh_from_db()
    assert operation.status == 'armed' and not calls


def test_select_all_accepts_more_than_generic_50_references(user, library, make_file, grant_member):
    grant_member(library, user)
    files = [make_file(library, f'参考-{i}.pdf') for i in range(60)]
    _, operation = _propose(user, scope=[library.code])
    ids = [row.pk for row in files]
    response = services.set_operation_sources(user, operation, ids)
    assert response['source_file_ids'] == ids
    operation.refresh_from_db()
    assert operation.params['sources'] == list(map(str, ids))


def test_empty_reply_wording_no_longer_says_based_on_whole_library():
    """助手无正文时的兜底文案要说和卡片一致的名字，不能还留着"基于全库"。

    生产实测（2026-10-19 11:21）：卡上已经是「编写新文档（可选参考文件）」，
    但气泡里仍写"只发起了操作：基于全库编写新文档"，用户会以为还是老行为。
    """
    from pathlib import Path

    js = (
        Path(__file__).resolve().parents[1]
        / "apps/webui/static/webui/kb-assistant.js"
    ).read_text(encoding="utf-8")
    assert 'compose_document: "编写新文档（需选参考文件）"' in js
    assert "基于全库编写新文档" not in js
