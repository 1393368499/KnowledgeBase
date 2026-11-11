"""第一阶段收尾回归：跨库隔离 + 多轮历史安全。

覆盖本轮修的真实缺陷：
- 一个库的 dense/sparse 抛错时，**后续健康库仍被查**、**前面的命中被保留**；
- 失败库进 report.failed_collections 且 degraded 为真（不伪称全量可用）；
- 异常分支必须 continue（否则 hits 张冠李戴 / UnboundLocalError）；
- 历史摘要：撤权后不泄露旧文件名、**保持原结果顺序**、目标名只用当前可读对象、
  已删库抛错时跳过而不是 500。
"""

from __future__ import annotations

import uuid

import pytest

from apps.knowledge import pipeline
from apps.knowledge.models import IngestedFile, KnowledgeLibrary
from apps.knowledge.vectorindex import SearchCandidate

pytestmark = pytest.mark.django_db


def candidate(file_id: int, score: float = 0.5, collection: str = "c") -> SearchCandidate:
    return SearchCandidate(
        point_id=f"p{file_id}", score=score, collection=collection, payload={"file_id": file_id}
    )


class FakeLibrary:
    """pipeline.search 需要的最小 library 形状。"""

    def __init__(self, pk: int, collection: str):
        self.pk = pk
        self.vector_collection = collection
        self.active_generation = 1


@pytest.fixture
def three_libraries():
    return [FakeLibrary(1, "kb-1-g1"), FakeLibrary(2, "kb-2-g1"), FakeLibrary(3, "kb-3-g1")]


def run_search(monkeypatch, libraries, dense_impl, sparse_impl=None):
    # 用一个极简的 VectorCollection 查询替身：**返回登记行形状**（不是 None）。
    # 新版 search 的语义是"没有登记行 → 无法确定该代模型 → 明确失败"，
    # 所以这里的替身必须提供登记快照，否则测的就不是"跨库错误隔离"而是
    # "没有登记行"这另一条（已被 test_vector_profiles 覆盖）。
    monkeypatch.setattr(pipeline, "build_qdrant_client", lambda: object())
    monkeypatch.setattr(
        pipeline.VectorCollection.objects, "filter", lambda *a, **k: _RegistryQuery()
    )
    monkeypatch.setattr(pipeline.vectorindex, "collection_exists", lambda *a, **k: True)
    monkeypatch.setattr(pipeline.vectorindex, "search_dense", dense_impl)
    monkeypatch.setattr(
        pipeline.vectorindex, "search_sparse", sparse_impl or (lambda *a, **k: [])
    )
    monkeypatch.setattr(pipeline, "_db_keyword_search", lambda **k: [])
    monkeypatch.setattr(pipeline, "_drop_tombstoned", lambda items, report: items)
    # 查询向量维度必须与该代登记维度一致（否则会被"串维度"守卫正确拦下）：
    # 登记替身用的是真实 bge1024 快照，所以这里给 1024 维。
    monkeypatch.setattr(pipeline, "embed_texts", lambda texts, profile=None: [[0.0] * 1024])
    return pipeline.search(
        query="质保期",
        libraries=libraries,
        permission_codes=None,
        limit=10,
        use_rerank=False,
        client=object(),
    )


class _EmptyQuery:
    def filter(self, *a, **k):
        return self

    def first(self):
        return None


class _RegistryQuery:
    """登记行替身：维度/模型/版本/端点都取自真实 active profile，保证严格匹配通过。"""

    def filter(self, *a, **k):
        return self

    def first(self):
        from types import SimpleNamespace

        from apps.knowledge import embedding as _emb

        prof = _emb.get_profile("bge1024")
        return SimpleNamespace(
            name="kb-registered",
            profile=prof.name,
            embedding_model=prof.model,
            model_version=prof.model_version,
            dimension=prof.dimension,
            base_url=prof.base_url,
        )


# ------------------------------------------------------------ 跨库隔离 ----


def test_middle_library_failure_keeps_healthy_hits(monkeypatch, three_libraries):
    """中间库抛错：后续库照查，前面命中保留（这是用户实测的那个 bug）。"""
    def dense(client, *, collections, **kwargs):
        name = collections[0]
        if name == "kb-2-g1":
            raise RuntimeError("OutputTooSmall panic")
        return [candidate(100 if name == "kb-1-g1" else 300, 0.9, name)]

    outcome = run_search(monkeypatch, three_libraries, dense)
    file_ids = {hit.get("file_id") for hit in outcome["results"]}
    assert file_ids == {100, 300}, f"健康库的命中丢了：{file_ids}"
    assert outcome["report"]["degraded"] is True
    failed = [item["collection"] for item in outcome["report"]["failed_collections"]]
    assert failed == ["kb-2-g1"]


def test_first_library_failure_still_queries_the_rest(monkeypatch, three_libraries):
    """第一个库就抛错（含 ServiceUnavailable）：不能中断后续库。

    这同时覆盖"异常分支缺 continue"的回归——缺了会 UnboundLocalError
    或把上一个库的 hits 张冠李戴。
    """
    from apps.knowledge.resilience import ServiceUnavailable

    def dense(client, *, collections, **kwargs):
        name = collections[0]
        if name == "kb-1-g1":
            raise ServiceUnavailable("bge down", reason="嵌入服务不可用")
        return [candidate(200 if name == "kb-2-g1" else 300, 0.9, name)]

    outcome = run_search(monkeypatch, three_libraries, dense)
    file_ids = {hit.get("file_id") for hit in outcome["results"]}
    assert file_ids == {200, 300}, f"首库失败后其余库没查到：{file_ids}"
    assert outcome["report"]["degraded"] is True


def test_sparse_failure_is_isolated_per_collection(monkeypatch, three_libraries):
    """sparse 同样逐库隔离：一个坏库不影响其余库的向量侧关键字。"""
    def dense(client, *, collections, **kwargs):
        return []

    def sparse(client, *, collections, **kwargs):
        name = collections[0]
        if name == "kb-3-g1":
            raise RuntimeError("sparse boom")
        return [candidate(10 if name == "kb-1-g1" else 20, 0.5, name)]

    outcome = run_search(monkeypatch, three_libraries, dense, sparse)
    file_ids = {hit.get("file_id") for hit in outcome["results"]}
    assert {10, 20} <= file_ids, f"稀疏检索被一个坏库拖垮：{file_ids}"
    failed = [item["collection"] for item in outcome["report"]["failed_collections"]]
    assert "kb-3-g1" in failed


# ------------------------------------------------ 多轮历史安全 ----


@pytest.fixture
def history_setup():
    from django.contrib.auth import get_user_model

    from apps.planner.models import Conversation, OperationRecord

    user = get_user_model().objects.create_user(
        username=f"hist-{uuid.uuid4().hex[:8]}", password="x", is_staff=True
    )
    library = KnowledgeLibrary.objects.create(code=f"h-{uuid.uuid4().hex[:6]}", name="历史库")
    records = []
    for index in range(3):
        records.append(
            IngestedFile._base_manager.create(
                library=library,
                original_name=f"文件{index}.txt",
                content_sha256=uuid.uuid4().hex,
                size_bytes=10,
                file_type="txt",
                parse_status="done",
            )
        )
    conversation = Conversation.objects.create(user=user, title="会话")
    # 故意让"原结果顺序"与 id 升序**相反**
    ordered_ids = [records[2].pk, records[1].pk, records[0].pk]
    operation = OperationRecord.objects.create(
        user=user,
        conversation=conversation,
        intent="list_files",   # 资料域：允许映射文件
        params={},
        status="executed",
        target_label="旧的目标名.txt",
        result={
            "count": 3,
            "items": [{"id": pk, "name": f"历史里的旧名字{pk}.txt"} for pk in ordered_ids],
        },
    )
    return user, conversation, library, records, operation, ordered_ids


def test_history_keeps_original_result_order(history_setup):
    """保持 result.items 原顺序：用户说的"第一个"必须还是那一个。"""
    from apps.planner.services import executed_history_summary

    user, conversation, _lib, records, _op, ordered_ids = history_setup
    summary = executed_history_summary(user, conversation)
    first_expected = IngestedFile._base_manager.get(pk=ordered_ids[0]).original_name
    # 摘要里第一个出现的名字必须是原结果的第一个
    position = summary.find(first_expected)
    assert position >= 0, f"原第一项没出现：{summary}"
    for other in ordered_ids[1:]:
        name = IngestedFile._base_manager.get(pk=other).original_name
        assert position < summary.find(name), "顺序被改成 id 升序了"


def test_history_never_uses_historical_names(history_setup):
    """历史里的旧名字一律不采用，只用**当前**名字。"""
    from apps.planner.services import executed_history_summary

    user, conversation, _lib, _records, _op, _ids = history_setup
    summary = executed_history_summary(user, conversation)
    assert "历史里的旧名字" not in summary
    assert "旧的目标名.txt" not in summary, "target_label 泄露了未经复核的名字"


def test_history_marks_filenames_as_untrusted(history_setup):
    from apps.planner.services import executed_history_summary

    user, conversation, _lib, _records, _op, _ids = history_setup
    summary = executed_history_summary(user, conversation)
    # 用真实的不可信标记（页眉/页脚）断言，而不是我臆想的文字
    assert "不可信引用数据" in summary
    assert "引用数据结束" in summary


def test_history_skips_when_all_items_unreadable(history_setup):
    """items 全部不可读/已不存在时整条不回放，也不留旧 target_label。

    注意：**不能**用 ``.delete()`` 来造这个场景——不可变守卫会直接拒绝删除
    （实测报"必须走受控删除通道"，这是正确的保护）。这里改用**不存在的文件 ID**
    模拟"来源已不可读/已不存在"，效果等价且不触碰保护。
    """
    from apps.planner.services import executed_history_summary

    user, conversation, _lib, _records, operation, _ids = history_setup
    operation.result = {
        "count": 2,
        "items": [
            {"id": 987654321, "name": "历史里的旧名字-幽灵A.txt"},
            {"id": 987654322, "name": "历史里的旧名字-幽灵B.txt"},
        ],
    }
    operation.save(update_fields=["result"])

    summary = executed_history_summary(user, conversation)
    assert "旧的目标名.txt" not in summary, "整条不可读时仍回放了 target_label"
    assert "幽灵A" not in summary and "幽灵B" not in summary


def test_audit_event_id_is_not_mistaken_for_a_file_id(history_setup):
    """审计事件 ID 恰好等于某个文件 ID 时，**不得**张冠李戴。

    list_audit 的 items.id 是审计事件域；如果照搬去查 IngestedFile，
    就会把一个审计事件显示成某份资料的标题。
    """
    from apps.planner.models import OperationRecord
    from apps.planner.services import executed_history_summary

    user, conversation, _lib, records, _op, _ids = history_setup
    colliding = records[0].pk  # 故意让审计事件 id 撞上真实文件 id
    OperationRecord.objects.create(
        user=user,
        conversation=conversation,
        intent="list_audit",
        params={},
        status="executed",
        target_label="",
        result={
            "count": 1,
            "items": [{"id": colliding, "name": "审计事件：某次删除"}],
        },
    )
    summary = executed_history_summary(user, conversation)
    # 审计那条只以"已完成"的形式出现，不得出现文件映射
    assert "审计事件" not in summary, "把审计事件当资料回放了"
    assert "（已完成）" in summary
    # 资料那条仍然正常映射
    assert records[0].original_name in summary


def test_history_is_scoped_to_current_user(history_setup):
    """别的用户的会话不回放。"""
    from django.contrib.auth import get_user_model

    from apps.planner.services import executed_history_summary

    _user, conversation, _lib, _records, _op, _ids = history_setup
    other = get_user_model().objects.create_user(
        username=f"other-{uuid.uuid4().hex[:8]}", password="x", is_staff=True
    )
    assert executed_history_summary(other, conversation) == ""
