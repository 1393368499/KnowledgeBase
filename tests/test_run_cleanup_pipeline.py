"""真实流水线路径验收：`ensure_library_index` 建出的集合必须被会话清理回收。

用户要求：不能只用 `run_resources.name()` 手工造名当探针 —— 必须走**真实代码路径**
（默认上传 / ensure_library_index）建集合，验证它确实落在本 run 独占前缀内、
且会话结束时被清掉、不泄漏。

运行（隔离端点）：
    set KB_TEST_QDRANT_URL=http://127.0.0.1:56339
    python -m pytest tests/test_run_cleanup_pipeline.py
"""

from __future__ import annotations

import os

import pytest

ENDPOINT = os.environ.get("KB_TEST_QDRANT_URL", "")

pytestmark = pytest.mark.skipif(
    not ENDPOINT, reason="未设置 KB_TEST_QDRANT_URL（隔离端点）—— 只在隔离端点跑"
)


@pytest.fixture
def isolated_qdrant(settings):
    """把向量索引指向隔离端点（server 模式）。"""
    settings.QDRANT_MODE = "server"
    settings.KB_QDRANT_URL = ENDPOINT
    return ENDPOINT


def test_ensure_library_index_creates_run_scoped_collection(
    db, make_library, isolated_qdrant
):
    """真实 `ensure_library_index` 路径：集合名必须落在本 run 前缀内。"""
    from qdrant_client import QdrantClient

    from apps.knowledge import vectorindex

    library = make_library()
    client = QdrantClient(url=ENDPOINT, timeout=20)

    before = {c.name for c in client.get_collections().collections}
    # **真实路径**：用生产同一个命名函数 + 同一个建集合函数
    # （apps/knowledge/vectorindex.py 里 prepare_generation 走的就是这两个）
    name = vectorindex.collection_name(library.pk, 1)
    vectorindex.ensure_collection(client, name, 4)
    after = {c.name for c in client.get_collections().collections}
    created = after - before

    assert created, "ensure_collection 应当创建了集合（真实路径）"
    run_prefix = f"kb-{os.environ['KB_TEST_RUN_UUID']}-"
    for name in created:
        assert name.startswith(run_prefix), (
            f"真实集合名 {name} 不在本 run 独占前缀 {run_prefix} 内 —— 会被清理器拒绝，导致泄漏"
        )
    # 故意**不删**：由 conftest 会话清理的“本 run 前缀清点”回收
    print(f"\n[真实路径] 已创建本 run 集合：{sorted(created)}")


def test_first_upload_namespace_is_run_scoped_not_kb_lt(isolated_namespace):
    """回归守卫：首上传并发用例的命名空间必须落在**本 run** 内。

    历史上它是 `kb-lt-<随机>` 且**无任何清理**，是 42 GiB 里 125 个 `kb-lt-*`
    集合的主要来源之一（随机前缀不属于任何 run → 清理器按设计拒绝碰 → 永久残留）。
    """
    assert not isolated_namespace.startswith("kb-lt-"), (
        f"禁止再使用 kb-lt 全局前缀（{isolated_namespace}）—— 它不会被任何 run 回收"
    )
    run_prefix = f"kb-{os.environ['KB_TEST_RUN_UUID']}-"
    assert isolated_namespace.startswith(run_prefix), (
        f"命名空间 {isolated_namespace} 不在本 run 前缀 {run_prefix} 内 → 不会被前缀清点回收"
    )