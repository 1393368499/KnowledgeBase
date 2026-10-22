"""AS711 重排客户端（bge-reranker-large）。

实测（``docs/architecture-review.md`` §3）：``POST {base}/rerank``，
请求体 ``{"model": ..., "query": ..., "documents": [...], "top_n": N}``，
返回 ``results: [{"index": i, "relevance_score": s}, ...]``。

设计约定：

1. **分数不猜**：只接受服务返回的 ``relevance_score``；缺失就抛错，不自己算一个假的。
2. **可关闭**：`KB_SEARCH_USE_RERANK=0` 时检索只用 RRF 融合，不调用重排——
   没有部署重排服务时系统仍可用，但文档里会写明"未启用重排"。
3. **失败可降级但留痕**：调用失败由检索服务决定是降级还是报错，客户端只负责如实抛。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import httpx

from django.conf import settings

from .httpclient import build_http_client
from .resilience import ServiceUnavailable, breaker_for, post_json


class RerankError(RuntimeError):
    """重排调用失败。"""


@dataclass(frozen=True)
class RerankProfile:
    base_url: str
    model: str
    api_key: str = ""
    timeout_seconds: float = 30.0


def default_profile() -> RerankProfile:
    services = getattr(settings, "MODEL_SERVICES", {})
    rerank = services.get("rerank", {})
    return RerankProfile(
        base_url=str(rerank.get("base_url", "")).rstrip("/"),
        model=str(rerank.get("model", "bge-reranker-large")),
        api_key=str(getattr(settings, "MODEL_API_KEY", "") or ""),
        timeout_seconds=float(getattr(settings, "KB_RERANK_TIMEOUT_SECONDS", 30)),
    )


@dataclass(frozen=True)
class RerankHit:
    """一条重排结果：原始序号 + 相关性分数。"""

    index: int
    score: float


def _document_windows(text: str, budget: int) -> list[str]:
    """按 UTF-8 字节上界分窗，预留重叠，保留全部正文且不切断汉字。"""
    windows, current, size = [], "", 0
    overlap = min(32, budget // 4)
    for char in text:
        width = len(char.encode("utf-8"))
        if size + width > budget:
            windows.append(current)
            current = current.encode("utf-8")[-overlap:].decode("utf-8", errors="ignore")
            size = len(current.encode("utf-8"))
        current += char
        size += width
    if current or not windows:
        windows.append(current)
    return windows


class RerankClient:
    def __init__(self, profile: RerankProfile | None = None, *, client: httpx.Client | None = None) -> None:
        self.profile = profile or default_profile()
        self._client = client
        self._lock = threading.Lock()
        self._proven = False
        self._last_error = ""

    @property
    def service_name(self) -> str:
        """熔断器/日志用的稳定服务标识。

        **不要用 ``self.profile.name``**：``RerankProfile`` 根本没有 ``name`` 字段，
        之前这么写在真实调用时会抛 `AttributeError`，把重排整条路弄挂
        （而且异常被上层兜住 → 看起来只是"重排没生效"，很难发现）。
        用 endpoint+model 拼出来，既稳定又能一眼看出打到哪个服务。
        """
        return f"rerank:{self.profile.base_url}|{self.profile.model}"

    @property
    def configured(self) -> bool:
        return bool(self.profile.base_url and self.profile.model)

    @property
    def available(self) -> bool:
        """是否有过成功调用（没实测过就不算可用）。"""
        return self._proven

    @property
    def last_error(self) -> str:
        return self._last_error

    def rerank(self, query: str, documents: list[str], *, top_n: int | None = None) -> list[RerankHit]:
        """对候选文档重排，返回按分数降序的结果。"""
        if not documents:
            return []
        if not self.configured:
            raise RerankError("未配置重排服务端点（AS711_RERANK_BASE_URL）")
        documents_to_send = documents
        window_sources = list(range(len(documents)))
        # 当前 BGE 服务的 query+document 上限为 512 tokens。使用保守的字节
        # 上界留出特殊 token 余量；每窗保留真实模型分数，以最佳窗口代表原段。
        # 不能直接截断：合同的命中条款可能恰好位于长段落末尾。
        if self.profile.model.lower().startswith("bge-reranker"):
            budget = 480 - len(query.encode("utf-8"))
            if budget < 32:
                raise RerankError("查询超过重排模型的长度范围，已保留完整查询供其他检索使用")
            documents_to_send, window_sources = [], []
            for index, document in enumerate(documents):
                windows = _document_windows(document, budget)
                documents_to_send.extend(windows)
                window_sources.extend([index] * len(windows))
        windowed = len(documents_to_send) != len(documents)
        payload = {
            "model": self.profile.model,
            "query": query,
            "documents": documents_to_send,
        }
        if windowed:
            # 服务端必须返回所有窗口，再按原文段聚合，避免同一段占满 top_n。
            payload["top_n"] = len(documents_to_send)
        elif top_n:
            payload["top_n"] = int(top_n)
        headers = {"Content-Type": "application/json"}
        if self.profile.api_key:
            headers["Authorization"] = f"Bearer {self.profile.api_key}"

        from .resilience import disabled_service, model_calls_disabled

        if model_calls_disabled():
            raise RerankError(str(disabled_service(self.service_name)))
        client = self._client or build_http_client(timeout=self.profile.timeout_seconds)
        close_after = self._client is None
        try:
            # 走统一的外呼入口：超时 + 重试上限 + 熔断（此前这里是裸 httpx 调用，
            # AS711 掉线时会把原始网络异常直接冒到检索接口）。
            body = post_json(
                http=client,
                url=f"{self.profile.base_url}/rerank",
                payload=payload,
                headers=headers,
                service=self.service_name,
                max_attempts=int(getattr(settings, "KB_RERANK_RETRIES", 1)) + 1,
                breaker=breaker_for(
                    self.service_name,
                    failure_threshold=int(
                        getattr(settings, "KB_SERVICE_FAILURE_THRESHOLD", 3)
                    ),
                    cooldown_seconds=float(
                        getattr(settings, "KB_SERVICE_COOLDOWN_SECONDS", 60)
                    ),
                ),
            )
        except ServiceUnavailable as exc:
            # 不可用要如实往外说，调用方据此在界面上标"重排不可用"
            self._last_error = exc.reason
            raise RerankError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - 统一包装成重排失败
            self._last_error = f"重排请求失败：{exc}"
            raise RerankError(self._last_error) from exc
        finally:
            if close_after:
                client.close()

        try:
            raw_results = body["results"]
        except (KeyError, TypeError) as exc:
            self._last_error = f"重排响应结构异常：{str(body)[:300]}"
            raise RerankError(self._last_error) from exc

        hits: list[RerankHit] = []
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            index = item.get("index")
            score = item.get("relevance_score", item.get("score"))
            if index is None or score is None:
                raise RerankError(f"重排结果缺少 index/relevance_score：{item!r}")
            index = int(index)
            if windowed:
                if not 0 <= index < len(window_sources):
                    raise RerankError(f"重排返回无效窗口序号：{index}")
                index = window_sources[index]
            hits.append(RerankHit(index=index, score=float(score)))
        with self._lock:
            self._proven = True
        hits.sort(key=lambda hit: hit.score, reverse=True)
        if windowed:
            best, seen = [], set()
            for hit in hits:
                if hit.index not in seen:
                    seen.add(hit.index)
                    best.append(hit)
            hits = best[:top_n] if top_n else best
        return hits
