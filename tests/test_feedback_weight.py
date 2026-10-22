"""反馈加权的验收用例（目标 ③ "基于反馈的学习规则"）。

锁死四条边界：
  * 权重**有界**（绝不超过 ±MAX_ADJUST）—— 反馈是偏好，不是硬过滤；
  * 被判不准确的资料**不会消失**（不做删除/过滤）；
  * 每条命中都带 `feedback_weight` / `feedback_counts`（**可审计**）；
  * 不改源资料（加权只发生在内存里的排序）。
"""

from __future__ import annotations

import pytest

from apps.knowledge import feedback as fb
from apps.knowledge.models import IngestedFile, SearchFeedback


@pytest.fixture
def two_files(make_library, make_file, grant_member, user):
    library = make_library(code="fbw", name="加权库")
    grant_member(library, user)
    good = make_file(library=library, name="多次判准确.txt")
    bad = make_file(library=library, name="多次判不准确.txt")
    return good, bad


def _verdicts(user, target, verdict, times):
    for index in range(times):
        SearchFeedback.objects.create(
            user=user, query=f"q{index}", verdict=verdict, file=target, chunk_ordinal=index
        )


def test_weight_is_positive_for_accurate_and_negative_for_inaccurate(db, user, two_files):
    good, bad = two_files
    _verdicts(user, good, "accurate", 3)
    _verdicts(user, bad, "inaccurate", 3)

    weights = fb.feedback_weights([good.pk, bad.pk])
    assert weights[good.pk] > 0, "多次判准确应为正权重"
    assert weights[bad.pk] < 0, "多次判不准确应为负权重"


def test_weight_is_bounded(db, user, two_files):
    """**核心**：无论多少条反馈，权重都不超过 ±MAX_ADJUST（不能盖过相关性）。"""
    good, _bad = two_files
    _verdicts(user, good, "accurate", 50)
    weights = fb.feedback_weights([good.pk])
    assert abs(weights[good.pk]) <= fb.MAX_ADJUST + 1e-9, weights


def test_listing_is_always_returned_even_when_penalised(db, user, two_files):
    """被判不准确的资料**不会消失** —— 反馈只调整次序，不做过滤/删除。"""
    good, bad = two_files
    _verdicts(user, bad, "inaccurate", 5)

    items = [
        {"file_id": good.pk, "score": 0.9},
        {"file_id": bad.pk, "score": 0.8},
    ]
    ordered = fb.apply_feedback_weight(items)

    assert {item["file_id"] for item in ordered} == {good.pk, bad.pk}, "命中不得被反馈过滤掉"
    assert ordered[0]["file_id"] == bad.pk or ordered[0]["file_id"] == good.pk  # 次序可调但都在


def test_every_hit_carries_audit_fields(db, user, two_files):
    good, bad = two_files
    _verdicts(user, good, "accurate", 2)
    _verdicts(user, bad, "inaccurate", 1)

    ordered = fb.apply_feedback_weight([
        {"file_id": good.pk, "score": 0.5},
        {"file_id": bad.pk, "score": 0.5},
    ])
    for item in ordered:
        assert "feedback_weight" in item, "每条命中都必须暴露权重（可审计）"
        assert "feedback_counts" in item, "每条命中都必须暴露计数（可复盘）"
    by_id = {item["file_id"]: item for item in ordered}
    assert by_id[good.pk]["feedback_counts"]["accurate"] == 2
    assert by_id[bad.pk]["feedback_counts"]["inaccurate"] == 1
    assert by_id[good.pk]["score"] > by_id[bad.pk]["score"], "同原始分下准确者应排前"


def test_weighting_does_not_modify_source_material(db, user, two_files):
    """加权只发生在内存里：源资料字段不得改变。"""
    good, _bad = two_files
    _verdicts(user, good, "accurate", 2)
    before = IngestedFile.objects.get(pk=good.pk)
    before_state = (before.created_at, before.parse_status, before.parsed_at)

    fb.apply_feedback_weight([{"file_id": good.pk, "score": 0.5}])

    after = IngestedFile.objects.get(pk=good.pk)
    assert (after.created_at, after.parse_status, after.parsed_at) == before_state


def test_no_feedback_means_untouched_order(db, user, two_files):
    good, bad = two_files
    items = [{"file_id": bad.pk, "score": 1.0}, {"file_id": good.pk, "score": 0.1}]
    ordered = fb.apply_feedback_weight(items)
    assert [item["file_id"] for item in ordered] == [bad.pk, good.pk], "无反馈时不得改次序"
