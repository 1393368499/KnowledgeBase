"""检索反馈持久化的验收用例（目标 ③ 第一项）。

覆盖：
  * 新建 201 / **改判** 200 且不堆重复记录（唯一约束 + update_or_create）；
  * 非法输入 400（verdict / query / chunk_ordinal）；
  * 不存在的资料 id → 404（不泄露存在性）；
  * GET 只列**自己**的反馈；
  * **源资料不被修改**（硬约束：反馈只影响排序，不碰源文档）；
  * 写审计事件。
"""

from __future__ import annotations

import pytest

from apps.knowledge.models import IngestedFile, SearchFeedback

URL = "/api/v1/knowledge/search-feedback/"


@pytest.fixture
def readable_file(make_library, make_file, grant_member, user):
    """user **可读**的一份资料（必须授成员关系，否则端点正确返回 404）。"""
    library = make_library(code="fb-lib", name="反馈库")
    grant_member(library, user)
    return make_file(library=library, name="反馈用资料.txt")


def _post(client, **payload):
    body = {"query": "质保期", "verdict": "accurate"}
    body.update(payload)
    return client.post(URL, body, format="json")


def test_create_feedback(auth_api, user, readable_file):
    client = auth_api(user)
    resp = _post(client, file_id=readable_file.pk, chunk_ordinal=2)
    assert resp.status_code == 201, resp.content
    assert resp.json()["verdict"] == "accurate"

    row = SearchFeedback.objects.get(user=user)
    assert (row.verdict, row.query, row.file_id, row.chunk_ordinal) == (
        "accurate", "质保期", readable_file.pk, 2
    )


def test_resubmit_is_update_not_duplicate(auth_api, user, readable_file):
    """同一个人对同一命中重复提交 = **改判**，不是堆两条互相矛盾的记录。"""
    client = auth_api(user)
    assert _post(client, file_id=readable_file.pk, chunk_ordinal=1).status_code == 201
    second = _post(client, file_id=readable_file.pk, chunk_ordinal=1, verdict="inaccurate")
    assert second.status_code == 200, second.content
    assert second.json()["created"] is False

    rows = SearchFeedback.objects.filter(user=user)
    assert rows.count() == 1, "重复提交不应产生第二条记录"
    assert rows.first().verdict == "inaccurate", "应当改判"


@pytest.mark.parametrize(
    "payload,field",
    [
        ({"verdict": "maybe"}, "verdict"),
        ({"query": "   "}, "query"),
        ({"chunk_ordinal": -1}, "chunk_ordinal"),
        ({"chunk_ordinal": "abc"}, "chunk_ordinal"),
    ],
)
def test_invalid_payload_is_rejected(auth_api, user, readable_file, payload, field):
    client = auth_api(user)
    resp = _post(client, file_id=readable_file.pk, **payload)
    assert resp.status_code == 400, resp.content
    assert field in resp.json(), f"应指出字段 {field}：{resp.json()}"


def test_missing_file_is_404(auth_api, user):
    client = auth_api(user)
    assert _post(client, file_id=999999).status_code == 404


def test_feedback_does_not_modify_source_material(auth_api, user, readable_file):
    """**硬约束**：反馈绝不能修改源资料。"""
    before = IngestedFile.objects.get(pk=readable_file.pk)
    # 注意：IngestedFile **没有** updated_at（只有 created_at/parsed_at），
    # 所以用"内容相关字段 + 创建时间"一起判"源资料未被改动"。
    before_created, before_status, before_parsed = (
        before.created_at, before.parse_status, before.parsed_at
    )

    client = auth_api(user)
    assert _post(client, file_id=readable_file.pk, verdict="inaccurate").status_code == 201

    after = IngestedFile.objects.get(pk=readable_file.pk)
    assert after.created_at == before_created, "源资料创建时间被改动"
    assert after.parse_status == before_status, "源资料解析状态被改动"
    assert after.parsed_at == before_parsed, "源资料解析时间被改动"
    # 反馈确实挂到了这份资料上（是"记录"而不是"改动"）
    assert after.search_feedback.filter(verdict="inaccurate").exists()


def test_list_only_own_feedback(auth_api, other_user, user):
    client = auth_api(user)
    SearchFeedback.objects.create(user=user, query="我的查询", verdict="accurate")
    SearchFeedback.objects.create(user=other_user, query="别人的查询", verdict="accurate")

    resp = client.get(URL)
    assert resp.status_code == 200, resp.content
    body = resp.json()
    items = body.get("results", body.get("data", body))
    queries = [item["query"] for item in items]
    assert "我的查询" in queries
    assert "别人的查询" not in queries, "不得看到别人的反馈"


def test_feedback_writes_audit_event(auth_api, user, readable_file):
    from apps.audit.models import AuditEvent

    client = auth_api(user)
    assert _post(client, file_id=readable_file.pk).status_code == 201
    assert AuditEvent.objects.filter(action="search_feedback").exists(), "反馈必须留审计"
