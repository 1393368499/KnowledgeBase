"""RRF 融合去重（真实浏览器缺陷的定向回归）。

**已复现的缺陷**：搜索"沧海716"时，上传的 id30 ordinal 0 这一片段
在结果第 1/2 条**重复出现**（向量路与 DB 关键词路各命中一次，score 都是 0.992）。
根因：融合 key 用的是 `collection:point_id` —— 向量路是 `<collection>:<chunk>`、
DB 关键词路是 `db:<chunk_pk>`，同一来源被算成两条。

修复口径：按**可信来源身份**（library_id + file_id + ordinal + content_sha256 [+ generation]）
对齐多路召回；缺身份字段时**保持原 key**（保守，不误合并）。
另外每一召回列表**内部先去重**，避免同一来源被重复加分。
"""

from __future__ import annotations

from apps.knowledge.vectorindex import SearchCandidate, fusion_identity, rrf_fuse

SHA = "a" * 64


def _vector_hit(**over):
    payload = {
        "file_id": 30,
        "library_id": 9,
        "ordinal": 0,
        "content_sha256": SHA,
        "generation": 1,
        "text": "沧海716",
    }
    payload.update(over)
    return SearchCandidate(
        point_id="kb-9-g1:chunk-1", score=0.992, collection="kb-9-g1", payload=payload
    )


def _db_hit(**over):
    payload = {
        "file_id": 30,
        "library_id": 9,
        "ordinal": 0,
        "content_sha256": SHA,
        "generation": 1,
        "text": "沧海716",
    }
    payload.update(over)
    return SearchCandidate(
        point_id="db:555", score=1.0, collection="db-keyword", payload=payload
    )


def test_same_source_from_two_recalls_is_merged():
    """**核心回归**：向量路 + DB 关键词路命中同一片段 → 融合后只出现一次。"""
    fused = rrf_fuse([[_vector_hit()], [_db_hit()]])
    assert len(fused) == 1, f"同一来源被算成两条：{[f.point_id for f in fused]}"
    # 两路名次都是 1 → 分数是两次 1/(k+1) 之和（融合是"合并"，不是"丢弃"）
    assert fused[0].score > 1.0 / 61, fused[0].score


def test_identity_falls_back_when_fields_missing():
    """缺身份字段 → **保持原 key**，不做激进合并。"""
    a = SearchCandidate(point_id="db:1", score=0.1, collection="db-keyword", payload={})
    b = SearchCandidate(point_id="db:2", score=0.1, collection="db-keyword", payload={"text": "x"})
    assert fusion_identity(a) == "db-keyword:db:1"
    assert fusion_identity(b) == "db-keyword:db:2"
    assert len(rrf_fuse([[a, b]])) == 2


def test_different_library_is_not_merged():
    """不同库的相同 file_id/ordinal **不得**被合并。"""
    fused = rrf_fuse([[_vector_hit(library_id=9)], [_db_hit(library_id=10)]])
    assert len(fused) == 2, "不同库被错误合并"


def test_different_file_is_not_merged():
    fused = rrf_fuse([[_vector_hit(file_id=30)], [_db_hit(file_id=31)]])
    assert len(fused) == 2, "不同文件被错误合并"


def test_different_ordinal_is_not_merged():
    fused = rrf_fuse([[_vector_hit(ordinal=0)], [_db_hit(ordinal=1)]])
    assert len(fused) == 2, "同一文件的不同分段被错误合并"


def test_different_content_hash_is_not_merged():
    fused = rrf_fuse([[_vector_hit(content_sha256="a" * 64)], [_db_hit(content_sha256="b" * 64)]])
    assert len(fused) == 2, "内容不同却被合并"


def test_duplicate_inside_one_recall_list_adds_score_once():
    """**路内去重**：同一列表里的重复项不得重复加分、也不得占用名次。"""
    dup = [_vector_hit(), _vector_hit(), _db_hit(file_id=99, ordinal=5)]
    fused = rrf_fuse([dup], k=60)
    by_file = {f.payload.get("file_id"): f for f in fused}
    # 重复项只计一次 → 分数等于"名次 1"的那一份，而不是两份相加
    assert abs(by_file[30].score - 1.0 / 61) < 1e-9, by_file[30].score
    # 去重后名次顺延：第二项应是 file_id=99（名次 2），而不是被重复项挤到名次 3
    assert abs(by_file[99].score - 1.0 / 62) < 1e-9, by_file[99].score


def test_no_duplicate_output_for_multi_recall_real_shape():
    """贴近真实形状：两路各含同一片段 + 各含独有片段 → 输出无重复来源。"""
    dense = [_vector_hit(), _vector_hit(file_id=31, ordinal=2)]
    keyword = [_db_hit(), _db_hit(file_id=32, ordinal=0)]
    fused = rrf_fuse([dense, keyword])
    identities = [
        (f.payload["library_id"], f.payload["file_id"], f.payload["ordinal"]) for f in fused
    ]
    assert len(identities) == len(set(identities)), f"输出仍有重复：{identities}"
    assert len(fused) == 3, identities  # 30/0 合并成一条，31/2 与 32/0 各一条
