"""``POST /api/v1/knowledge/search-feedback/<pk>/handle/``：反馈分诊（原型「标记已处理」）。

原型里那列「标记已处理」原来在真实界面上是一个**空表头 + 空单元格**（肉眼可见的未完成），
现在补上，但语义**刻意收窄**（这是本次实现的核心约束，测试要把边界钉死）：

1. **只留痕**：写 ``handled_at`` / ``handled_by``，并落一条 ``search_feedback_handled``
   审计事件 —— 不改变 ``verdict``；
2. **不改检索加权**：加权的输入仍然只有 ``verdict`` 的净分（``net_negative_feedback_rows``
   与 ``feedback_pending_count`` 的口径一个字都不变）；
3. **越权与幂等**：只能处理**自己**的反馈，系统管理员可处理全部，其他人一律 404
   （不泄露存在性）；重复点击保留**第一次**留痕，不刷新处理人/时间；
4. **列表要有东西可填**：``GET`` 行里出现 ``handled`` / ``handled_at`` / ``handled_by``。
"""

from __future__ import annotations

import pytest

from apps.audit.models import AuditEvent
from apps.knowledge.integrity import feedback_pending_count, net_negative_feedback_rows
from apps.knowledge.models import MemberRole, SearchFeedback

pytestmark = pytest.mark.django_db

LIST_URL = "/api/v1/knowledge/search-feedback/"


def _handle_url(pk: int) -> str:
    return f"/api/v1/knowledge/search-feedback/{pk}/handle/"


def _feedback(user, record, verdict, *, query="北极星质量要点", ordinal=0, note=""):
    return SearchFeedback.objects.create(
        user=user,
        query=query,
        file=record,
        chunk_ordinal=ordinal,
        verdict=verdict,
        note=note,
    )


def test_owner_can_handle_own_feedback_and_leaves_audit_trail(
    auth_api, user, library, make_file, grant_member
):
    grant_member(library, user, role=MemberRole.READER)
    record = make_file(library, "北极星质量要点.md")
    feedback = _feedback(
        user, record, SearchFeedback.Verdict.INACCURATE, note="金额与合同不一致"
    )

    response = auth_api(user).post(_handle_url(feedback.pk))

    assert response.status_code == 200
    body = response.json()
    assert body["handled"] is True
    assert body["already_handled"] is False
    assert body["handled_by"] == user.username
    assert body["handled_at"]

    feedback.refresh_from_db()
    assert feedback.handled_at is not None
    assert feedback.handled_by_id == user.pk
    # 只留痕：判定本身不许被改写
    assert feedback.verdict == SearchFeedback.Verdict.INACCURATE

    event = AuditEvent.objects.filter(action="search_feedback_handled").first()
    assert event is not None
    assert event.resource_key == str(feedback.pk)
    assert event.payload["verdict"] == SearchFeedback.Verdict.INACCURATE


def test_handling_is_idempotent_and_keeps_first_trail(
    auth_api, user, library, make_file, grant_member
):
    grant_member(library, user, role=MemberRole.READER)
    record = make_file(library, "北极星质量要点.md")
    feedback = _feedback(user, record, SearchFeedback.Verdict.ACCURATE)

    first = auth_api(user).post(_handle_url(feedback.pk)).json()
    second = auth_api(user).post(_handle_url(feedback.pk)).json()

    assert first["already_handled"] is False
    assert second["already_handled"] is True
    assert second["handled_at"] == first["handled_at"]
    assert second["handled_by"] == user.username
    # 第二次点击不重复落审计事件（同一条留痕只记一次）
    assert AuditEvent.objects.filter(action="search_feedback_handled").count() == 1


def test_other_user_gets_404_and_cannot_touch_the_row(
    auth_api, user, other_user, library, make_file, grant_member
):
    grant_member(library, user, role=MemberRole.READER)
    grant_member(library, other_user, role=MemberRole.READER)
    record = make_file(library, "北极星质量要点.md")
    feedback = _feedback(user, record, SearchFeedback.Verdict.INACCURATE)

    response = auth_api(other_user).post(_handle_url(feedback.pk))

    assert response.status_code == 404
    feedback.refresh_from_db()
    assert feedback.handled_at is None
    assert feedback.handled_by_id is None
    assert not AuditEvent.objects.filter(action="search_feedback_handled").exists()


def test_system_admin_can_handle_someone_elses_feedback(
    auth_api, admin_user, user, library, make_file, grant_member
):
    grant_member(library, user, role=MemberRole.READER)
    record = make_file(library, "北极星质量要点.md")
    feedback = _feedback(user, record, SearchFeedback.Verdict.INACCURATE)

    response = auth_api(admin_user).post(_handle_url(feedback.pk))

    assert response.status_code == 200
    body = response.json()
    assert body["handled_by"] == admin_user.username
    feedback.refresh_from_db()
    assert feedback.handled_by_id == admin_user.pk


def test_unknown_feedback_returns_404(auth_api, user):
    assert auth_api(user).post(_handle_url(999_999)).status_code == 404


def test_triage_does_not_change_weighting_or_rescan_scope(
    auth_api, user, library, make_file, grant_member
):
    """把边界钉死：分诊不是「忽略该条反馈」，净分/待处理/重扫口径必须一字不变。"""

    grant_member(library, user, role=MemberRole.READER)
    record = make_file(library, "北极星质量要点.md")
    feedback = _feedback(user, record, SearchFeedback.Verdict.INACCURATE)

    before_rows = net_negative_feedback_rows()
    before_pending = feedback_pending_count()
    assert before_pending == 1
    assert [row["file_id"] for row in before_rows] == [record.pk]

    assert auth_api(user).post(_handle_url(feedback.pk)).status_code == 200

    after_rows = net_negative_feedback_rows()
    after_pending = feedback_pending_count()
    assert after_pending == before_pending == 1
    assert [row["file_id"] for row in after_rows] == [record.pk]
    assert after_rows[0]["net"] == before_rows[0]["net"] == -1

    row = auth_api(user).get(LIST_URL).json()["results"][0]
    assert row["net"] == -1
    assert row["verdict"] == SearchFeedback.Verdict.INACCURATE


def test_list_exposes_handled_state(auth_api, user, library, make_file, grant_member):
    grant_member(library, user, role=MemberRole.READER)
    record = make_file(library, "北极星质量要点.md")
    feedback = _feedback(user, record, SearchFeedback.Verdict.ACCURATE)

    untouched = auth_api(user).get(LIST_URL).json()["results"][0]
    assert untouched["handled"] is False
    assert untouched["handled_at"] is None
    assert untouched["handled_by"] == ""

    auth_api(user).post(_handle_url(feedback.pk))

    handled = auth_api(user).get(LIST_URL).json()["results"][0]
    assert handled["handled"] is True
    assert handled["handled_at"] is not None
    assert handled["handled_by"] == user.username
