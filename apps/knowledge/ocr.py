"""AS711 OCR 客户端（PaddleOCR-VL-1.5）。

实测结论（见 ``docs/architecture-review.md`` §3）：AS711 的 OCR 容器是 vLLM
部署，走 OpenAI 兼容的 ``/v1/chat/completions``，图片用 ``image_url`` 传 base64。
因此这里**不装本地 OCR 引擎**，只做 HTTP 调用。

可用性判定是"探测过才算可用"：任何一次成功调用都会把该 profile 标记为 ready，
没有成功记录时 ``available`` 为 False，解析层会明确报错而不是把扫描件当空文件。
"""

from __future__ import annotations

import base64
import io
import threading
from dataclasses import dataclass

import httpx

from django.conf import settings

from .httpclient import build_http_client
from .resilience import ServiceUnavailable, breaker_for, post_json

#: OCR 提示词：要求只回文字，不要客套话，便于直接入分段
OCR_INSTRUCTION = (
    "你是文档识别引擎。请逐行识别图片中的所有文字，保持原文顺序与数字精确性，"
    "表格用「列名=值」的形式逐行输出。只输出识别结果，不要任何解释、不要 Markdown 代码块。"
)


class OcrError(RuntimeError):
    """OCR 调用失败。"""


class OcrOutputTruncated(OcrError):
    """识别没有正常结束，原始响应不能入库。"""


@dataclass(frozen=True)
class OcrProfile:
    base_url: str
    model: str
    api_key: str = ""
    timeout_seconds: float = 120.0
    max_tokens: int = 4096


def default_profile() -> OcrProfile:
    services = getattr(settings, "MODEL_SERVICES", {})
    ocr = services.get("ocr", {})
    return OcrProfile(
        base_url=str(ocr.get("base_url", "")).rstrip("/"),
        model=str(ocr.get("model", "PaddleOCR-VL-1.5")),
        api_key=str(getattr(settings, "MODEL_API_KEY", "") or ""),
        timeout_seconds=float(getattr(settings, "KB_OCR_TIMEOUT_SECONDS", 120)),
        max_tokens=int(getattr(settings, "KB_OCR_MAX_TOKENS", 4096)),
    )


class OcrClient:
    """极薄的 OCR 客户端：一次调用只做一件事。"""

    def __init__(self, profile: OcrProfile | None = None, *, client: httpx.Client | None = None) -> None:
        self.profile = profile or default_profile()
        self._client = client
        self._probe_lock = threading.Lock()
        self._last_error = ""

    # ------------------------------------------------------------ 可用性 ----
    @property
    def service_name(self) -> str:
        """熔断器/日志用的稳定服务标识。

        ``OcrProfile`` **没有** ``name`` 字段——之前写成 ``self.profile.name``
        会在真实调用时抛 AttributeError，让 OCR 整条路挂在"看起来只是识别失败"上。
        用 endpoint+model 拼，既稳定又能一眼看出打到哪个服务。
        """
        return f"ocr:{self.profile.base_url}|{self.profile.model}"

    @property
    def configured(self) -> bool:
        return bool(self.profile.base_url and self.profile.model)

    @property
    def available(self) -> bool:
        """是否**可以尝试**调用。

        口径修正（审查方指出）：早先这里是"必须成功过一次才算可用"，
        结果生产默认路径（``process_file`` 新建一个 ``OcrClient``）永远拿到
        ``available=False``，解析层据此判定"OCR 不可用"而拒绝扫描件——
        等于"配置齐全但永远不调用"。

        现在分两层：

        - ``configured``（端点+模型名配好）→ 允许发起**首次真实调用**；
        - 是否真的可用由调用结果决定：成功即 :meth:`mark_proven`，
          失败记录 ``last_error`` 并抛出，由上层决定失败或重试。

        所以 ``available`` 只回答"值不值得试"，不替服务端下结论。
        """
        return self.configured or self.proven

    @property
    def proven(self) -> bool:
        """是否**已经实测成功过**（用于如实报告可用性，不用于拦截首次调用）。"""
        return bool(getattr(self, "_proven", False))

    @property
    def last_error(self) -> str:
        return self._last_error

    def mark_proven(self) -> None:
        with self._probe_lock:
            self._proven = True

    # ------------------------------------------------------------ 调用 ----
    def recognize_image(self, image_bytes: bytes, *, hint: str = "") -> str:
        """识别一张图片，返回纯文本。"""
        return self._recognize_complete(image_bytes, hint=hint, depth=0)

    def _recognize_complete(self, image_bytes: bytes, *, hint: str, depth: int) -> str:
        try:
            return self._recognize_image_once(image_bytes, hint=hint)
        except OcrOutputTruncated:
            if depth >= 2:
                raise
            # 整页盖章、手写或复杂版面可能使 VLM 持续生成。保留全部页面，
            # 仅在截断时按上下区域重识别；每个区域也必须正常结束。
            from PIL import Image
            try:
                with Image.open(io.BytesIO(image_bytes)) as source:
                    width, height = source.size
                    if height < 256:
                        raise ValueError("图片太小，无法继续分区")
                    middle = height // 2
                    overlap = min(48, height // 8)
                    pieces = []
                    for top, bottom in ((0, middle + overlap), (middle - overlap, height)):
                        with source.crop((0, top, width, bottom)) as region:
                            buffer = io.BytesIO()
                            region.save(buffer, format="PNG")
                            pieces.append(buffer.getvalue())
            except Exception as exc:
                raise OcrOutputTruncated("OCR 输出达到长度上限，拒绝保存不完整页；页面无法分区重识别") from exc
            texts = [self._recognize_complete(piece, hint=hint, depth=depth + 1) for piece in pieces]
            first, second = [text.splitlines() for text in texts]
            # 只消除两个区域边界处完全相同的行，保留其他所有识别结果。
            for count in range(min(len(first), len(second)), 0, -1):
                if [line.strip() for line in first[-count:]] == [line.strip() for line in second[:count]]:
                    second = second[count:]
                    break
            return "\n".join(first + second)

    def _recognize_image_once(self, image_bytes: bytes, *, hint: str = "") -> str:
        if not self.configured:
            raise OcrError("未配置 OCR 服务端点（AS711_OCR_BASE_URL）")
        if not image_bytes:
            raise OcrError("图片字节为空")
        payload = {
            "model": self.profile.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"{OCR_INSTRUCTION}\n文件提示：{hint or '(未提供)'}"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64,"
                                + base64.b64encode(image_bytes).decode("ascii")
                            },
                        },
                    ],
                }
            ],
            "temperature": 0,
            "max_tokens": self.profile.max_tokens,
        }
        if "paddleocr-vl" in self.profile.model.lower():
            # Paddle 官方模型使用固定任务提示，不使用通用聊天指令或文件名。
            # https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.5
            content = payload["messages"][0]["content"]
            content[0]["text"] = "OCR:"
            content.reverse()
            # 实际签字页在默认解码下反复生成同一栏直到截断；轻度重复惩罚
            # 让模型正常结束，不靠扩大输出上限继续生成重复内容。
            payload["repetition_penalty"] = float(getattr(settings, "KB_OCR_REPETITION_PENALTY", 1.1))
        headers = {"Content-Type": "application/json"}
        if self.profile.api_key:
            headers["Authorization"] = f"Bearer {self.profile.api_key}"

        from .resilience import disabled_service, model_calls_disabled

        if model_calls_disabled():
            raise OcrError(str(disabled_service(self.service_name)))
        client = self._client or build_http_client(timeout=self.profile.timeout_seconds)
        close_after = self._client is None
        try:
            # 走统一外呼入口：超时 + 重试上限 + 熔断。
            # 此前这里是裸 httpx 调用，AS711 掉线会把原始网络异常冒到解析流程，
            # 现在统一包装成 OcrError，并让熔断器在下游请求前就快速失败。
            body = post_json(
                http=client,
                url=f"{self.profile.base_url}/chat/completions",
                payload=payload,
                headers=headers,
                service=self.service_name,
                max_attempts=int(getattr(settings, "KB_OCR_RETRIES", 1)) + 1,
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
            self._last_error = f"OCR 服务不可用：{exc.reason}"
            raise OcrError(self._last_error) from exc
        except Exception as exc:  # noqa: BLE001 - 统一包装成 OCR 失败
            self._last_error = f"OCR 请求失败：{exc}"
            raise OcrError(self._last_error) from exc
        finally:
            if close_after:
                client.close()

        choices = body.get("choices") if isinstance(body, dict) else None
        if (isinstance(choices, list) and choices and isinstance(choices[0], dict)
                and choices[0].get("finish_reason") == "length"):
            raise OcrOutputTruncated("OCR 输出达到长度上限，拒绝保存不完整页；分区重识别仍未完整结束")
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            self._last_error = f"OCR 响应结构异常：{str(body)[:300]}"
            raise OcrError(self._last_error) from exc
        self.mark_proven()
        return str(content or "")
