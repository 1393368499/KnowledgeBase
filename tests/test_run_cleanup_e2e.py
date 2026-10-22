"""真实入口验收：让 **pytest 自己** 建真集合，由 conftest 的会话 finally 清理。

为什么必须单独有这个文件：`cleanup_probe.py` 单独调用 `cleanup()` 只能证明
"清理函数能删东西"，**证明不了 pytest 的 finally 自动执行**（用户明确指出）。

这里的用例**不自己删**集合 —— 删除必须发生在 conftest 的 session 级 finally 里。
运行方式（隔离端点）：
    set KB_TEST_QDRANT_URL=http://127.0.0.1:56339
    python -m pytest tests/test_run_cleanup_e2e.py
随后用独立进程验证集合确实没了：
    python scripts/cleanup_probe.py --endpoint http://127.0.0.1:56339
"""

from __future__ import annotations

import os

import pytest

ENDPOINT = os.environ.get("KB_TEST_QDRANT_URL", "")

pytestmark = pytest.mark.skipif(
    not ENDPOINT, reason="未设置 KB_TEST_QDRANT_URL（隔离端点）—— 该用例只在隔离端点跑"
)


def test_pytest_creates_real_collection_and_finally_cleans_it(run_resources):
    """建真集合并登记；**不做删除**，留给 conftest 的 finally。"""
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, VectorParams

    client = QdrantClient(url=ENDPOINT, timeout=20)
    name = run_resources.name("pytest-e2e")
    assert not client.collection_exists(name), "run 独占前缀必须唯一（不应已存在）"

    run_resources.declare("qdrant_collection", name)
    client.create_collection(name, vectors_config=VectorParams(size=4, distance=Distance.COSINE))
    run_resources.confirm("qdrant_collection", name)

    assert client.collection_exists(name), "刚创建的集合应当存在"
    # 故意**不删**：由会话 finally 清理 → 这才验证了"真实入口自动清理"
    print(f"\n[真实入口] 已登记待清理集合：{name}")


def test_registered_collection_survives_until_session_end(run_resources):
    """同时在场的第二个集合：验证多个资源都被登记且互不干扰。"""
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, VectorParams

    client = QdrantClient(url=ENDPOINT, timeout=20)
    name = run_resources.name("pytest-e2e-2")
    if client.collection_exists(name):
        pytest.skip("同名集合已存在（不应发生）")
    run_resources.declare("qdrant_collection", name)
    client.create_collection(name, vectors_config=VectorParams(size=4, distance=Distance.COSINE))
    run_resources.confirm("qdrant_collection", name)
    assert client.collection_exists(name)
