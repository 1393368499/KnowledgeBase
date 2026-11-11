"""中文连续输入（无空格）的关键词召回回归。

修的是 ``evals/BASELINE.md`` 量出来的真实缺陷（表 A vs 表 C）：

* 同一套 50 条题，把 query 里的空格去掉（真实用户输入形态）后，离线词法
  Recall@5 从 88% 掉到 12%；8 条纯中文问句 0 命中，仅 6 条带 ASCII 编号的侥幸命中。
* 根因：``re.split(r"[\\s\\W_]+", query)`` 对中文只切出**一个整串词**，
  必须整句原样出现在 chunk 里才算命中；DB 兜底因此长期空转，召回全靠稠密向量。

本文件钉住三件事：
1. 切词：连续中文必须切成二元组（不是整串一个词）；
2. 打分：覆盖查询词越多的片段排得越前，且"一个词都没覆盖"的噪声被丢掉；
3. 重排：分数饱和（模板重复合同 0.9844 vs 0.9922）时必须保留融合顺序。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.knowledge import pipeline, textquery


# ---------------------------------------------------------------- 1) 切词 ----


def test_continuous_chinese_is_split_into_bigrams():
    """去掉空格的连续中文不能只切出一个整串词。"""
    terms = textquery.terms("预付款比例为百分之三十")
    assert "预付" in terms and "付款" in terms and "比例" in terms
    # 整串不再是唯一的（也不是最长的）匹配单元
    assert terms != ["预付款比例为百分之三十"]
    assert len(terms) >= 5


def test_latin_identifiers_are_kept_whole():
    """型号/编号这类拉丁词必须整个保留（旧实现也这么干）。"""
    terms = textquery.terms("GB-T-19001 质保期")
    assert "gb-t-19001" in terms
    assert "质保" in terms and "保期" in terms


def test_single_chinese_char_is_not_a_query_term():
    """单字噪音太大，不单独作为查询词。"""
    assert "付" not in textquery.terms("付")


# ---------------------------------------------------------------- 2) 打分 ----


def test_coverage_rewards_the_chunk_that_covers_more_terms():
    terms = textquery.terms("预付款比例")
    full = textquery.coverage("预付款比例为合同总价的百分之三十", terms)
    weak = textquery.coverage("付款方式另行约定", terms)
    assert full > weak > 0


class _FakeQuerySet:
    """忽略过滤条件、只按构造顺序吐 chunk 的 queryset 替身。

    这里只验证"切词 + 覆盖率打分 + 排序"，不验证 SQL；所以过滤条件被忽略是刻意的
    （否则要在测试里复刻 icontains 的行为，反而测不出本函数的逻辑）。
    """

    def __init__(self, chunks):
        self._chunks = chunks

    def filter(self, *args, **kwargs):
        return self

    def select_related(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def __getitem__(self, item):
        return self._chunks[item]


def _chunk(pk: int, text: str) -> SimpleNamespace:
    library = SimpleNamespace(pk=1, code="lib-1")
    record = SimpleNamespace(
        pk=pk,
        library_id=1,
        library=library,
        original_name=f"doc-{pk}.docx",
        directory_path="/",
        classification="contract",
        customer="测试客户",
        department="法务",
        content_sha256=f"sha-{pk}",
    )
    return SimpleNamespace(
        pk=pk,
        file_id=pk,
        file=record,
        ordinal=0,
        page=1,
        text=text,
        permission_code="p-1",
    )


def test_db_keyword_search_matches_space_stripped_chinese(monkeypatch):
    """去掉空格的中文查询必须能召回：旧实现这里会 0 命中。"""
    chunks = [
        _chunk(1, "本合同付款方式另行约定，逾期不计利息。"),
        _chunk(2, "预付款比例为合同总价的百分之三十，验收后支付尾款。"),
    ]
    monkeypatch.setattr(
        pipeline.DocumentChunk, "objects", SimpleNamespace(filter=lambda **k: _FakeQuerySet(chunks))
    )

    hits = pipeline._db_keyword_search(
        query="预付款比例为百分之三十", library_ids=[1], limit=5
    )
    assert hits, "连续中文查询不应再是 0 命中"
    assert hits[0].payload["file_id"] == 2, "覆盖查询词最多的片段必须排第一"
    assert hits[0].payload["source_layer"] == "db_keyword"
    assert hits[0].payload["keyword_hits"] >= 3
    # 一个查询词都没覆盖的片段不该占名额
    assert all(item.payload["keyword_hits"] > 0 for item in hits)


# ---------------------------------------------------------------- 3) 重排 ----


def test_rerank_spread_detects_saturated_scores():
    """0.9844 vs 0.9922（只差 0.008）是饱和噪声，区分度低于阈值。"""
    spread = pipeline._rerank_spread([0.9844, 0.9922, 0.9911])
    assert 0 < spread < 0.05


def test_rerank_spread_is_healthy_for_separated_scores():
    assert pipeline._rerank_spread([0.91, 0.42, 0.11]) > 0.05


@pytest.mark.parametrize("scores", [[], [0.5]])
def test_rerank_spread_of_degenerate_input_is_zero(scores):
    assert pipeline._rerank_spread(list(scores)) == 0.0


def test_saturated_rerank_is_not_applied():
    """饱和（差 0.008）时拒绝采用重排顺序 —— 这是 Recall 0.90→0.50 的直接原因。"""
    apply, spread = pipeline._should_apply_rerank([0.9844, 0.9922], 0.05)
    assert apply is False
    assert spread == pytest.approx(0.0078, abs=1e-9)


def test_separated_rerank_is_applied():
    apply, spread = pipeline._should_apply_rerank([0.91, 0.42], 0.05)
    assert apply is True
    assert spread > 0.05


def test_rerank_threshold_zero_restores_old_behaviour():
    """``KB_RERANK_MIN_SPREAD=0`` = 永远采用重排（旧行为，留给需要的人）。"""
    apply, _ = pipeline._should_apply_rerank([0.9844, 0.9922], 0.0)
    assert apply is True
