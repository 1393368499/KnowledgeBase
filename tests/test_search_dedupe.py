"""搜索重复的合成负例（独立复现：每份资料返回两条一模一样的正文）。

两个独立机制都要钉住：

1. **身份对齐**（`vectorindex.fusion_identity`）：稠密路的 payload 带 `generation`，
   DB 关键词兜底路**没有**这个字段 —— 曾经把 generation 写进身份，于是同一分段
   被算成两个身份（`…:<sha>:2` 与 `…:<sha>:-`），融合不去重 → 结果重复。
2. **同文件同正文兜底去重**（`pipeline._dedupe_same_text`）：任何召回路径字段缺失
   时的最后一道防线，且**不得**把同一份资料的不同真实段落合并掉。

用例全部用**合成**数据，不碰真实资料库。
"""

from __future__ import annotations

import pytest

from apps.knowledge import pipeline, vectorindex
from apps.knowledge.vectorindex import SearchCandidate


def _candidate(*, file_id: int, ordinal: int, text: str, sha: str = "sha-x",
               generation: int | None = 2, collection: str = "kb-1-g2",
               point_id: str = "p", score: float = 1.0) -> SearchCandidate:
    payload = {
        "file_id": file_id,
        "library_id": 1,
        "ordinal": ordinal,
        "text": text,
        "content_sha256": sha,
    }
    if generation is not None:
        payload["generation"] = generation
    return SearchCandidate(
        point_id=point_id, score=score, collection=collection, payload=payload
    )


# ------------------------------------------------------- 1) 身份对齐（融合层） ----


def test_dense_and_db_candidates_for_same_chunk_fuse_into_one():
    """同一分段：稠密路带 generation、DB 路没有 → 必须融合成**一条**。"""
    dense = [_candidate(file_id=7, ordinal=0, text="质保期为 12 个月", point_id="vec-1")]
    db = [
        _candidate(
            file_id=7, ordinal=0, text="质保期为 12 个月", generation=None,
            collection="db-keyword", point_id="db:11", score=0.9,
        )
    ]
    fused = vectorindex.rrf_fuse([dense, db], limit=10)
    assert len(fused) == 1, [c.payload.get("text") for c in fused]


def test_two_real_paragraphs_of_the_same_file_are_kept():
    """同一份资料的两个**不同**段落必须都保留（不能被当成重复）。"""
    dense = [
        _candidate(file_id=7, ordinal=0, text="质保期为 12 个月", point_id="vec-1"),
        _candidate(file_id=7, ordinal=1, text="付款方式：预付 30%", point_id="vec-2"),
    ]
    fused = vectorindex.rrf_fuse([dense], limit=10)
    assert len(fused) == 2


def test_identical_text_in_different_files_is_not_merged():
    """不同资料即使正文相同也不算重复（各自的召回都要保留）。"""
    dense = [
        _candidate(file_id=7, ordinal=0, text="质保期为 12 个月", point_id="vec-1"),
        _candidate(file_id=8, ordinal=0, text="质保期为 12 个月", point_id="vec-2"),
    ]
    fused = vectorindex.rrf_fuse([dense], limit=10)
    assert len(fused) == 2


# ------------------------------------------- 2) 同文件同正文兜底去重（管线层） ----


def test_text_dedupe_drops_identical_body_of_same_file():
    """字段缺失导致身份没对齐时，兜底去重仍要消掉"同文件同正文"的第二条。"""
    report: dict = {}
    # 两条同 file 同正文：一条没有 sha（身份会退回 collection:point_id）
    first = _candidate(file_id=7, ordinal=0, text="质保期为 12 个月", point_id="vec-1")
    second = _candidate(
        file_id=7, ordinal=0, text="质保期为 12 个月", sha="", generation=None,
        collection="db-keyword", point_id="db:11",
    )
    kept = pipeline._dedupe_same_text([first, second], report)
    assert len(kept) == 1
    assert report["text_dedupe_dropped"] == 1


def test_text_dedupe_keeps_different_paragraphs_and_files():
    report: dict = {}
    candidates = [
        _candidate(file_id=7, ordinal=0, text="质保期为 12 个月", point_id="a"),
        _candidate(file_id=7, ordinal=1, text="付款方式：预付 30%", point_id="b"),
        _candidate(file_id=8, ordinal=0, text="质保期为 12 个月", point_id="c"),
    ]
    kept = pipeline._dedupe_same_text(candidates, report)
    assert len(kept) == 3, "把真实段落或别的资料误当重复删掉了"
    assert "text_dedupe_dropped" not in report


def test_text_dedupe_ignores_whitespace_differences():
    """正文只差空白/换行/连续空格也算同一条（OCR 与分段常产生这种差异）。"""
    report: dict = {}
    a = _candidate(file_id=7, ordinal=0, text="质保期为 12 个月", point_id="a")
    b = _candidate(file_id=7, ordinal=0, text="质保期为  12\n个月", point_id="b")
    kept = pipeline._dedupe_same_text([a, b], report)
    assert len(kept) == 1


def test_text_dedupe_keeps_different_text_differing_only_in_word_boundary():
    """**只差词界的两段不同正文绝不能被折叠成同一条**（独立审查指出的误删）。

    反例：`"AB C"` 与 `"A BC"` —— 若按"删掉所有空白"比较，两者都会变成 `"ABC"`，
    于是第二段被当成重复删掉。正确做法是只折叠**连续**空白、保留词界。
    """
    report: dict = {}
    first = _candidate(file_id=7, ordinal=0, text="AB C", point_id="a")
    second = _candidate(file_id=7, ordinal=1, text="A BC", point_id="b")
    kept = pipeline._dedupe_same_text([first, second], report)
    assert len(kept) == 2, "只差词界的不同正文被误当成重复删掉了"
    assert "text_dedupe_dropped" not in report


# --------------------------------------------------- 3) 端到端（合成召回多路） ----


@pytest.mark.django_db
def test_search_returns_one_entry_per_document_when_all_routes_hit(
    monkeypatch, library, make_file
):
    """**独立复现的合成负例**：3 份资料、每份 1 段，稠密+稀疏+DB 兜底都命中 →
    每份资料只允许出现**一条**结果（不是两条）。

    这里直接喂三路召回结果（不连 Qdrant/模型），验证融合之后的结果条数。
    """
    files = [make_file(library=library, name=f"合成-{index}.md") for index in range(3)]
    text = "质保期为验收合格之日起 12 个月。"
    dense = [
        _candidate(file_id=row.pk, ordinal=0, text=text, sha=f"sha-{row.pk}",
                   point_id=f"vec-{row.pk}")
        for row in files
    ]
    sparse = [
        _candidate(file_id=row.pk, ordinal=0, text=text, sha=f"sha-{row.pk}",
                   point_id=f"vec-{row.pk}", generation=2)
        for row in files
    ]
    db_hits = [
        _candidate(file_id=row.pk, ordinal=0, text=text, sha=f"sha-{row.pk}",
                   generation=None, collection="db-keyword", point_id=f"db:{row.pk}")
        for row in files
    ]
    fused = vectorindex.rrf_fuse([dense, sparse, db_hits], limit=30)
    report: dict = {}
    fused = pipeline._dedupe_same_text(fused, report)
    assert len(fused) == len(files), [
        (c.payload.get("file_id"), c.payload.get("ordinal")) for c in fused
    ]
    # 每份资料一条，且都不是同文件重复
    per_file: dict[int, int] = {}
    for candidate in fused:
        per_file[candidate.payload["file_id"]] = per_file.get(candidate.payload["file_id"], 0) + 1
    assert set(per_file.values()) == {1}, per_file
