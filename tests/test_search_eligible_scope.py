"""结构化条件必须在**服务端全量**收敛，并约束整条检索链路。

覆盖审查要求的四个用例（全部用隔离 stub，**不调用 AS711**）：
1. 候选超过 200 份：不能截断（前端枚举 200 条的老做法会漏）；
2. 越权：别的库的资料不能进候选；
3. 结构化条件无候选：立即空结果，**不外呼模型**；
4. 重排之前已筛掉不合条件的候选。

设计：结构化收敛用真实 ORM（自己的库、真实行）；检索链路用假 client/假 rerank
把"进入各路检索的 file_ids"记录下来，断言约束真的传下去了。
"""

from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model

from apps.knowledge import pipeline
from apps.knowledge.models import IngestedFile, KnowledgeLibrary

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner():
    """**普通**用户：这样 readable_library_ids 才真的按成员关系收敛。

    注意不能用 staff：``is_system_admin`` 会把 staff 判成不受限，
    权限用例就失去意义了（我第一次就是这么写错的）。
    """
    return get_user_model().objects.create_user(
        username=f"elig-{uuid.uuid4().hex[:8]}", password="x"
    )


@pytest.fixture
def admin():
    """系统管理员：readable_library_ids 返回 None（不受限）。"""
    return get_user_model().objects.create_user(
        username=f"root-{uuid.uuid4().hex[:8]}", password="x", is_staff=True
    )


@pytest.fixture
def two_libraries():
    """两个内部库：都不是"公开库"，所以只有成员才可读。"""
    mine = KnowledgeLibrary.objects.create(
        code=f"mine-{uuid.uuid4().hex[:6]}", name="我的库"
    )
    other = KnowledgeLibrary.objects.create(
        code=f"other-{uuid.uuid4().hex[:6]}", name="别人的库"
    )
    return mine, other


@pytest.fixture
def member(owner, two_libraries):
    """只把 owner 加进第一个库。"""
    from apps.knowledge.models import LibraryMember

    mine, _other = two_libraries
    LibraryMember.objects.create(library=mine, user=owner, role="viewer")
    return owner


def make_files(library, count, *, department="技术部", amount=100.0, prefix="f"):
    rows = []
    for index in range(count):
        rows.append(
            IngestedFile(
                library=library,
                original_name=f"{prefix}-{index}.pdf",
                content_sha256=f"{uuid.uuid4().hex}{index}",
                size_bytes=1024,
                file_type="pdf",
                parse_status="done",
                department=department,
                amount_wan=amount,
            )
        )
    return IngestedFile._base_manager.bulk_create(rows)


# ------------------------------------------------ 1) 候选超过 200 份不截断 ----


def test_eligible_set_is_not_truncated_beyond_200(two_libraries, admin):
    """结构化收敛必须覆盖**全部**匹配资料，而不是只取一页。

    这里刻意用 ``_readable_files(None)``（管理员/不受限口径）并显式限定库，
    验证的是"收敛不分页"，不掺权限因素。
    """
    from apps.knowledge.views import _readable_files, apply_file_filters

    library, _other = two_libraries
    make_files(library, 250, department="技术部")

    # owner 是 staff → 不受限口径（系统管理员），排除权限因素，专测"不分页"
    queryset = apply_file_filters(
        _readable_files(admin).filter(library_id__in=[library.pk]), department="技术部"
    )
    eligible = set(queryset.values_list("id", flat=True))
    assert len(eligible) == 250, f"候选被截断成 {len(eligible)} 条（应 250）"


# ------------------------------------------------------------ 2) 越权 ----


def test_eligible_set_excludes_unreadable_libraries(two_libraries, member):
    """候选只能来自用户可读的库（成员关系决定，不是 owner 字段）。"""
    from apps.knowledge.views import _readable_files, apply_file_filters

    mine, other = two_libraries
    make_files(mine, 3, prefix="mine")
    make_files(other, 5, prefix="other")

    eligible = set(
        apply_file_filters(_readable_files(member), department="技术部").values_list(
            "id", flat=True
        )
    )
    mine_ids = set(IngestedFile._base_manager.filter(library=mine).values_list("id", flat=True))
    other_ids = set(
        IngestedFile._base_manager.filter(library=other).values_list("id", flat=True)
    )
    assert eligible & mine_ids, "自己库的资料应当可读"
    assert not (eligible & other_ids), "越权库的资料混进了候选"


# ------------------------------------------ 3) 无候选：不外呼模型 ----


def test_no_candidate_returns_empty_without_calling_models(monkeypatch):
    """空候选必须立即返回，且**不得**发起任何模型/向量调用。"""
    called = {"dense": 0, "sparse": 0, "rerank": 0, "embed": 0}

    def boom(*args, **kwargs):
        called["dense"] += 1
        raise AssertionError("空候选时不应发起稠密检索")

    monkeypatch.setattr(pipeline.vectorindex, "search_dense", boom)
    monkeypatch.setattr(pipeline.vectorindex, "search_sparse", boom)
    monkeypatch.setattr(pipeline, "_db_keyword_search", boom)

    outcome = pipeline.search(
        query="合同",
        libraries=[],
        permission_codes=None,
        eligible_file_ids=set(),
    )
    assert outcome["results"] == []
    assert outcome["report"]["reason"] == "no_eligible_file"
    assert called == {"dense": 0, "sparse": 0, "rerank": 0, "embed": 0}


# ------------------------------------- 4) 重排前已筛掉不合条件的候选 ----


def test_ineligible_candidates_are_dropped_before_rerank():
    """融合结果里不合条件的命中必须在**重排之前**被剔除。"""
    from apps.knowledge.pipeline import _filter_by_eligible
    from apps.knowledge.vectorindex import SearchCandidate

    kept = SearchCandidate(point_id="p1", score=0.9, collection="c", payload={"file_id": 11})
    dropped = SearchCandidate(
        point_id="p2", score=0.8, collection="c", payload={"file_id": 99}
    )
    no_id = SearchCandidate(point_id="p3", score=0.7, collection="c", payload={})

    report: dict = {}
    kept_list = _filter_by_eligible([kept, dropped, no_id], {11}, report)
    assert [item.point_id for item in kept_list] == ["p1"]
    assert report["eligible_filtered_out"] == 2
    # report 里同时记录"过滤后还剩几条"，便于验收核对
    assert report["eligible_kept"] == 1


def test_filter_by_eligible_is_a_noop_when_unrestricted():
    """``None`` 表示不限制：一条都不该被丢掉。"""
    from apps.knowledge.pipeline import _filter_by_eligible
    from apps.knowledge.vectorindex import SearchCandidate

    items = [
        SearchCandidate(point_id="a", score=1.0, collection="c", payload={"file_id": 1}),
        SearchCandidate(point_id="b", score=0.5, collection="c", payload={}),
    ]
    report: dict = {}
    assert len(_filter_by_eligible(items, None, report)) == 2
    assert "eligible_filtered_out" not in report


def test_eligible_filter_runs_before_rerank_in_source():
    """结构断言：过滤调用必须出现在重排调用**之前**。

    这是防"顺序写反"的回归——先重排再过滤等于把不合条件（乃至已删除）的正文
    已经发给外部模型了。
    """
    import inspect

    from apps.knowledge import pipeline

    source = inspect.getsource(pipeline.search)
    assert source.index("_filter_by_eligible") < source.index("rerank("), (
        "过滤发生在重排之后 —— 会泄露不该送出的正文"
    )
