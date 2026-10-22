"""``GET /api/v1/knowledge/search-feedback/``：只列自己的反馈 + 真实净分（且不越权）。

背景：Agent 控制台的「检索反馈（批注）」表此前是**写死的空态**（假占位：
端点其实早就有 POST/GET，界面却显示"还没有检索反馈"）。接上真实端点后必须证明
三件事：

1. 只返回**自己**的反馈 —— 别人的批注不外泄（该页对所有登录用户可见）；
2. 带上资料名与判定中文名，且保持 DRF 分页形状（表头 5 列要有东西可填）；
3. ``net`` 只对**当前可读**的资料给出，并且与「重扫未处理反馈」**同一口径**
   （跨用户聚合的净负分才会被覆盖重扫）；不可读的资料一律 ``None``。
"""

from __future__ import annotations

import pytest

from apps.knowledge.models import MemberRole, SearchFeedback
from apps.knowledge.integrity import net_negative_feedback_rows

pytestmark = pytest.mark.django_db

LIST_URL = "/api/v1/knowledge/search-feedback/"


def _feedback(user, record, verdict, *, query="北极星质量要点", ordinal=0, note=""):
    return SearchFeedback.objects.create(
        user=user,
        query=query,
        file=record,
        chunk_ordinal=ordinal,
        verdict=verdict,
        note=note,
    )


def test_get_lists_only_my_own_feedback(
    auth_api, user, other_user, library, make_file, grant_member
):
    grant_member(library, user, role=MemberRole.READER)
    record = make_file(library, "北极星质量要点.md")
    _feedback(
        user,
        record,
        SearchFeedback.Verdict.INACCURATE,
        note="金额与合同不一致",
    )
    _feedback(other_user, record, SearchFeedback.Verdict.ACCURATE, query="别人的查询")

    body = auth_api(user).get(LIST_URL).json()

    assert body["count"] == 1
    assert [row["query"] for row in body["results"]] == ["北极星质量要点"]
    assert body["results"][0]["note"] == "金额与合同不一致"


def test_item_carries_file_name_and_verdict_label(
    auth_api, user, library, make_file, grant_member
):
    grant_member(library, user, role=MemberRole.READER)
    record = make_file(library, "北极星质量要点 084928.pdf")
    _feedback(user, record, SearchFeedback.Verdict.ACCURATE)

    row = auth_api(user).get(LIST_URL).json()["results"][0]

    assert row["file_id"] == record.pk
    assert row["file_name"] == "北极星质量要点 084928.pdf"
    assert row["verdict"] == "accurate"
    assert row["verdict_label"] == "准确"


def test_net_uses_the_same_rule_as_rescan(
    auth_api, user, other_user, library, make_file, grant_member
):
    """净分必须跨用户聚合（与「重扫未处理反馈」完全同一条查询）。"""
    grant_member(library, user, role=MemberRole.READER)
    record = make_file(library, "北极星质量要点.md")
    _feedback(user, record, SearchFeedback.Verdict.INACCURATE, ordinal=0)
    _feedback(other_user, record, SearchFeedback.Verdict.INACCURATE, ordinal=1)
    _feedback(user, record, SearchFeedback.Verdict.ACCURATE, ordinal=2)

    rows = auth_api(user).get(LIST_URL).json()["results"]
    nets = {row["file_id"]: row["net"] for row in rows}
    expected = {row["file_id"]: row["net"] for row in net_negative_feedback_rows()}

    # 2 条不准确 - 1 条准确 = -1（别人的不准确也算数，这正是重扫口径）
    assert nets[record.pk] == -1
    assert nets[record.pk] == expected[record.pk]


def test_net_is_hidden_for_unreadable_file(auth_api, user, make_file, make_library):
    """撤权后不得再借净分读到"这份资料别人怎么说"——只给自己的行，净分留空。"""
    secret = make_library(code="secret-lib", name="涉密资料库")
    record = make_file(secret, "不该看到的资料.md")
    _feedback(user, record, SearchFeedback.Verdict.INACCURATE)

    body = auth_api(user).get(LIST_URL).json()

    assert body["count"] == 1
    assert body["results"][0]["file_id"] == record.pk
    assert body["results"][0]["net"] is None


def test_feedback_without_file_is_listed_with_empty_name(auth_api, user):
    SearchFeedback.objects.create(
        user=user,
        query="整次检索都没有有用结果",
        file=None,
        chunk_ordinal=0,
        verdict=SearchFeedback.Verdict.INACCURATE,
        note="一条都没命中",
    )

    row = auth_api(user).get(LIST_URL).json()["results"][0]

    assert row["file_id"] is None
    assert row["file_name"] == ""
    assert row["net"] is None


def test_response_keeps_pagination_shape_and_states_the_rule(
    auth_api, user, library, make_file, grant_member
):
    grant_member(library, user, role=MemberRole.READER)
    record = make_file(library, "北极星质量要点.md")
    _feedback(user, record, SearchFeedback.Verdict.INACCURATE)

    body = auth_api(user).get(LIST_URL).json()

    # 表头 5 列依赖这些键（分页形状不能变）
    assert {"count", "results", "note"} <= set(body)
    assert "我自己" in body["note"]
    assert "同一口径" in body["note"]


def test_newest_feedback_comes_first(auth_api, user, library, make_file, grant_member):
    grant_member(library, user, role=MemberRole.READER)
    first = make_file(library, "先反馈的资料.md")
    second = make_file(library, "后反馈的资料.md")
    _feedback(user, first, SearchFeedback.Verdict.ACCURATE, query="第一条")
    _feedback(user, second, SearchFeedback.Verdict.ACCURATE, query="第二条")

    queries = [row["query"] for row in auth_api(user).get(LIST_URL).json()["results"]]

    # ordering=["-created_at","-id"]：最新的在前（Auto_now_add 可能同秒，靠 -id 兜底）
    assert queries[0] == "第二条"


def test_anonymous_is_rejected(api):
    response = api.get(LIST_URL)

    assert response.status_code in (401, 403)
