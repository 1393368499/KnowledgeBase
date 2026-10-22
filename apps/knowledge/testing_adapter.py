"""显式离线测试适配器：**只在明确打开时**提供确定性假向量。

用户 2026-10-13 的硬约束
------------------------
> 离线测试用显式 test/mock 适配器，不可把模拟结果当真实模型结果，
> 不可用假向量污染正式索引。

所以本模块做了三层防误用：

1. **默认关闭**：``KB_TEST_ADAPTER`` 为空时，:func:`enabled` 恒为 False，
   适配器根本不会被构造；生产路径拿不到任何假向量。
2. **名字必须带标记**：适配器的 :attr:`OfflineAdapter.tag` 是 ``offline-test``。
   任何要写向量的调用都必须显式 ``allow_test_adapter=True``，
   且目标 collection **必须**以 :data:`TEST_COLLECTION_SUFFIX` 结尾，
   否则抛 :class:`TestAdapterForbidden`。
3. **结果自带标记**：返回的向量与元数据里带 ``is_test_adapter=True``，
   检索结果里也会带上 ``_test_adapter`` 字段——任何时候都能一眼看出
   "这条结果是假向量算出来的"，不会被当成真实模型结果汇报。

它生产什么
----------
- ``embed(texts)``：用文本的 SHA-256 派生的确定性伪随机向量（同文本永远同向量），
  维度必须与 active profile 一致；**不是**语义向量，所以它只能验证
  "管道通不通、过滤对不对、维度管不管得住"，不能验证检索质量。
- ``rerank(query, docs)``：按"查询词覆盖率"打分，同样确定性。
- ``ocr(image)``：返回固定占位文本，用来验证扫描件的分支会走到 OCR。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from django.conf import settings

#: 假向量 collection 必须带这个后缀，否则拒绝写入
TEST_COLLECTION_SUFFIX = "__offline_test"

TAG = "offline-test"


class TestAdapterDisabled(RuntimeError):
    """没有显式打开适配器，却想用它。"""

    #: 告诉 pytest 别把这个类当测试用例收集（名字以 Test 开头）
    __test__ = False


class TestAdapterForbidden(RuntimeError):
    """想用假向量写正式索引——明确禁止。"""

    __test__ = False


def enabled() -> bool:
    return str(getattr(settings, "KB_TEST_ADAPTER", "")).strip().lower() == "offline"


def require_enabled() -> None:
    if not enabled():
        raise TestAdapterDisabled(
            "离线测试适配器未启用。请在 .env 里显式设置 KB_TEST_ADAPTER=offline 后重试。"
        )


def assert_test_collection(name: str) -> None:
    """假向量只准写测试 collection。"""
    if not name.endswith(TEST_COLLECTION_SUFFIX):
        raise TestAdapterForbidden(
            f"拒绝用假向量写入 {name!r}：测试适配器只允许写以 "
            f"{TEST_COLLECTION_SUFFIX!r} 结尾的 collection，避免污染正式索引。"
        )


def test_collection_name(library_id: int, generation: int) -> str:
    """测试专用 collection 名：带 ``__offline_test`` 后缀，一眼能认出来。"""
    from .vectorindex import collection_name

    return f"{collection_name(library_id, generation)}{TEST_COLLECTION_SUFFIX}"


@dataclass(frozen=True)
class DeterministicEmbedding:
    vector: list[float]
    is_test_adapter: bool = True
    tag: str = TAG


class OfflineAdapter:
    """确定性假模型。**不是**语义模型，不能用来评估检索质量。"""

    tag = TAG

    def __init__(self, *, dimension: int | None = None) -> None:
        require_enabled()
        self.dimension = int(
            dimension or getattr(settings, "KB_TEST_ADAPTER_DIMENSION", 1024)
        )

    # ------------------------------------------------------------ 嵌入 ----
    def embed(self, texts: list[str], *, collection: str = "") -> list[list[float]]:
        """按文本哈希生成确定性向量（同文本 → 同向量）。"""
        if collection:
            assert_test_collection(collection)
        return [self._vector_for(text) for text in texts]

    def _vector_for(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        # 反复扩展摘要，凑够维度；再映射到 [-1, 1]
        raw = bytearray()
        counter = 0
        while len(raw) < self.dimension * 2:
            raw.extend(hashlib.sha256(digest + counter.to_bytes(4, "big")).digest())
            counter += 1
        vector = [
            (int.from_bytes(raw[i * 2 : i * 2 + 2], "big") / 65535.0) * 2.0 - 1.0
            for i in range(self.dimension)
        ]
        # 归一化，和余弦距离口径一致
        norm = sum(value * value for value in vector) ** 0.5 or 1.0
        return [value / norm for value in vector]

    # ------------------------------------------------------------ 重排 ----
    def rerank(self, query: str, documents: list[str]) -> list[tuple[int, float]]:
        """按查询词覆盖率打分：确定性、可解释，但不代表语义相关性。"""
        terms = {term for term in re.split(r"\W+", query) if len(term) > 1}
        scored: list[tuple[int, float]] = []
        for index, document in enumerate(documents):
            if not terms:
                scored.append((index, 0.0))
                continue
            hit = sum(1 for term in terms if term in document)
            scored.append((index, hit / len(terms)))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored

    # --------------------------------------------------------------- OCR ----
    def ocr(self, image_bytes: bytes, *, hint: str = "") -> str:
        digest = hashlib.sha256(image_bytes).hexdigest()[:12]
        return f"[离线测试适配器 OCR 占位文本 {digest}] {hint}".strip()


def mark(payload: dict) -> dict:
    """给向量 payload 打上"这是测试数据"的标记。"""
    marked = dict(payload)
    marked["is_test_adapter"] = True
    marked["adapter_tag"] = TAG
    return marked
