"""文件编号/名称优先于语义相似度，并贯穿候选截断、重排与反馈。"""
from types import SimpleNamespace
import json

import httpx
import pytest

from apps.knowledge import feedback, filename_search, pipeline
from apps.knowledge.models import DocumentChunk, ParseStatus
from apps.knowledge.vectorindex import SearchCandidate


@pytest.mark.parametrize("name,query,rank", [
    ("zy2025194-8 海南迎宾馆.pdf", "zy2025194-8", 2),
    ("zy2025194-80 海南迎宾馆.pdf", "zy2025194-8", 1),
    ("zy2025194-81.pdf", "zy2025194-8", 1),
    ("xzy2025194-8.pdf", "zy2025194-8", 1),
    ("ZY2025194－8.pdf", "zy2025194-8", 3),
    ("出差报告.docx", "出差报告.docx", 4),
    ("出差报告.docx", "出差报告", 3),
    ("某项目出差报告.docx", "出差报告", 2),
    ("其他.pdf", "zy2025194-8", 0),
    ("其他.pdf", ".pdf", 0),
    ("其他.pdf", "", 0),
])
def test_filename_matching_boundaries(name, query, rank):
    assert filename_search.match_rank(name, query) == rank


def candidate(record, text, ordinal=1):
    return SearchCandidate(point_id=f"vec:{record.pk}:{ordinal}", score=0.99,
                           collection="synthetic", payload={
        "file_id": record.pk, "library_id": record.library_id,
        "content_sha256": record.content_sha256, "ordinal": ordinal,
        "original_name": record.original_name, "text": text,
    })


@pytest.fixture
def search_without_external_models(library):
    # 无 collection 时只使用真实数据库召回；client 占位不会建立网络连接。
    library.vector_collection = ""

    def run(query="zy2025194-8", **kwargs):
        return pipeline.search(query=query, libraries=[library], permission_codes=None,
                               client=object(), use_rerank=kwargs.pop("use_rerank", False), **kwargs)
    return run


@pytest.mark.django_db
def test_filename_survives_fusion_cutoff_and_rerank_top_n(
    library, make_file, monkeypatch, search_without_external_models,
):
    wrong = [make_file(library, name=f"其他项目-{index}.pdf") for index in range(35)]
    exact = make_file(library, name="zy2025194-8 海南迎宾馆.pdf")
    DocumentChunk.objects.create(file=exact, ordinal=1, text="正文没有合同编号", page=1)
    # 35 条正文候选先占满融合名额；重排还故意只返回一个无关项目。
    monkeypatch.setattr(pipeline, "_db_keyword_search", lambda **kw: [
        candidate(row, f"相似正文 {index}") for index, row in enumerate(wrong)
    ])
    calls = []

    class Reranker:
        profile = SimpleNamespace(model="synthetic-reranker")

        def rerank(self, query, documents, top_n):
            calls.extend(documents)
            return [SimpleNamespace(index=0, score=0.999)]

    result = search_without_external_models(use_rerank=True, rerank_client=Reranker())
    assert result["report"]["rerank"]["applied"]
    assert any("文件名：" + exact.original_name in text for text in calls)
    assert result["results"][0]["file_id"] == exact.pk
    assert result["results"][0]["filename_match"] == "filename_token"
    assert len(result["results"]) == 2


@pytest.mark.django_db
def test_recall_does_not_truncate_exact_match_behind_many_similar_names(library, make_file):
    for index in range(40):
        make_file(library, name=f"zy2025194-8{index} 相近编号.pdf")
    exact = make_file(library, name="zy2025194-8 项目合同.pdf")
    hits = filename_search.recall(query="zy2025194-8", library_ids=[library.pk], limit=10)
    assert hits[0].payload["file_id"] == exact.pk
    # 未解析的文件仍可按名称找到，但不能伪造正文段号/页号。
    assert hits[0].payload["ordinal"] is None
    assert hits[0].payload["page"] is None
    assert hits[0].payload["text"] == ""


@pytest.mark.django_db
def test_filename_recall_respects_library_and_structured_scope(
    library, make_library, make_file, search_without_external_models,
):
    other = make_library(code="private")
    inactive = make_library(code="inactive", active=False)
    allowed = make_file(library, name="zy2025194-8 正确合同.pdf")
    excluded = make_file(library, name="zy2025194-8 其他部门.pdf")
    make_file(other, name="zy2025194-8.pdf")
    make_file(inactive, name="zy2025194-8.pdf")
    make_file(library, name="zy2025194-8.pdf", status=ParseStatus.ARCHIVED)
    result = search_without_external_models(eligible_file_ids={allowed.pk})
    assert [row["file_id"] for row in result["results"]] == [allowed.pk]
    hits = filename_search.recall(query="zy2025194-8", library_ids=[library.pk, inactive.pk], limit=10)
    assert {row.payload["file_id"] for row in hits} == {allowed.pk, excluded.pk}
    assert search_without_external_models(eligible_file_ids=set())["results"] == []


@pytest.mark.django_db
def test_number_keyword_keeps_single_digit_suffix(library, make_file):
    wrong = make_file(library, name="a.pdf")
    exact = make_file(library, name="b.pdf")
    DocumentChunk.objects.create(file=wrong, ordinal=1, text="编号 zy2025194-9")
    DocumentChunk.objects.create(file=exact, ordinal=1, text="编号 zy2025194-8")
    hits = pipeline._db_keyword_search(query="zy2025194-8", library_ids=[library.pk], limit=10)
    assert [row.payload["file_id"] for row in hits] == [exact.pk]


@pytest.mark.django_db
def test_filename_promotes_only_one_chunk_without_losing_other_paragraphs(library, make_file):
    record = make_file(library, name="出差报告.pdf")
    other = make_file(library, name="其他.pdf")
    values = [candidate(other, "其他正文"), candidate(record, "实际行程", 2),
              candidate(record, "报销明细", 3)]
    ordered = filename_search.promote(values, "出差报告")
    assert [(row.payload["file_id"], row.payload["ordinal"]) for row in ordered] == [
        (record.pk, 2), (other.pk, 1), (record.pk, 3),
    ]
    assert sum(bool(row.payload.get("filename_match_rank")) for row in ordered) == 1


def test_feedback_cannot_override_filename_priority(monkeypatch):
    monkeypatch.setattr(feedback, "feedback_weights", lambda ids: {1: -0.15, 2: 0.15})
    monkeypatch.setattr(feedback, "feedback_counts", lambda ids: {})
    result = feedback.apply_feedback_weight([
        {"file_id": 1, "filename_match_rank": 2, "score": 0.015},
        {"file_id": 2, "filename_match_rank": 0, "score": 0.999},
    ])
    assert [row["file_id"] for row in result] == [1, 2]


@pytest.mark.django_db
def test_reranker_windows_long_documents_and_maps_best_score_to_original():
    from apps.knowledge.rerank import RerankClient, RerankProfile
    from apps.knowledge.resilience import BREAKERS
    BREAKERS.clear()
    query = "末尾条款"
    long_document = "文件名：项目合同.pdf\n" + "".join(
        f"第{index:03d}项正文内容甲乙丙😀\n" for index in range(100)
    ) + "末尾条款应当保留"
    captured = {}

    def handler(request):
        payload = json.loads(request.content)
        captured.update(payload)
        assert all(len(query.encode()) + len(text.encode()) <= 480 for text in payload["documents"])
        assert payload["top_n"] == len(payload["documents"])
        return httpx.Response(200, json={"results": [
            {"index": index, "relevance_score": 0.99 if "末尾条款应当保留" in text else
             0.7 if text == "其他段落" else 0.2}
            for index, text in enumerate(payload["documents"])
        ]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        client = RerankClient(RerankProfile(base_url="http://rerank.test/v1", model="bge-reranker-large"), client=transport)
        hits = client.rerank(query, [long_document, "其他段落"], top_n=2)
    assert [hit.index for hit in hits] == [0, 1]
    assert [hit.score for hit in hits] == [0.99, 0.7]
    windows = captured["documents"][:-1]
    assert windows[0].startswith("文件名：项目合同.pdf")
    assert windows[-1].endswith("末尾条款应当保留")
    covered = 0
    for window in windows:
        start = long_document.find(window, max(0, covered - 32))
        assert 0 <= start <= covered
        covered = start + len(window)
    assert covered == len(long_document)
    BREAKERS.clear()


def test_reranker_overlong_query_is_explicit_instead_of_silently_truncated():
    from apps.knowledge.rerank import RerankClient, RerankError, RerankProfile
    client = RerankClient(RerankProfile(base_url="http://rerank.test/v1", model="bge-reranker-large"))
    with pytest.raises(RerankError, match="查询超过"):
        client.rerank("查询" * 100, ["正文"])
