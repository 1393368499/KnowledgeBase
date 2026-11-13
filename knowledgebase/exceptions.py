"""统一异常处理：未认证语义 + 未预期异常转结构化 500。

**为什么要把"未认证"做成明确的 401 + code**（Codex 追加审核）：
DRF 的 SessionAuthentication 没有 ``WWW-Authenticate`` 头，于是"没登录"会被渲染成
**403**，与"登录了但没权限"的 403 长得一样。前端的 KB.api 因此只敢对 401 跳登录，
会话过期时功能就不完整（页面停在原地、每个请求都 403）。

这里给未认证/认证失败统一成 ``401 + {"code": "not_authenticated"}``：
  * 前端只在 ``code == "not_authenticated"``（或 401）时跳登录；
  * 权限 403（``permission_denied``）与 CSRF 403 保持原样、原地如实报错，
    不会被误当成"会话过期"而跳走、也不会把登录页当成导出文件存下来。
"""

from __future__ import annotations

import logging

from rest_framework import exceptions as drf_exceptions
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger("api")


def api_exception_handler(exc, context):
    # 库级写冻结（重建/换代窗口）：**409 Conflict，不是 500**。
    #
    # 为什么必须显式映射：``WriteFrozen`` 继承 ``RuntimeError``，DRF 不认识它，
    # 会落到"未预期异常"分支变成 500 internal_error。那样调用方（前端/调度器）会把
    # "这个库正在重建，稍后再试"当成服务故障去重试/报警，还会掩盖真实原因。
    # 409 的语义正好是"请求本身没错，但与当前资源状态冲突"，并带上可判断的字段：
    # ``reason``（为什么被拒）、``frozen_by``（谁冻的）、``retryable``（等它结束再试）。
    from apps.knowledge.writegate import WriteFrozen

    if isinstance(exc, WriteFrozen):
        return Response(
            {
                "detail": str(exc) or "信息库正在重建/换代，写入被拒绝",
                "code": "write_frozen",
                "reason": getattr(exc, "reason", "") or "",
                "frozen_by": getattr(exc, "frozen_by", "") or "",
                # 冻结是**临时窗口**：重建自己会解除，所以"稍后重试"是有效建议。
                "retryable": True,
            },
            status=409,
        )

    # 未认证 / 认证失败：强制 401 + 机器可读 code（不依赖本地化文案）
    if isinstance(exc, (drf_exceptions.NotAuthenticated, drf_exceptions.AuthenticationFailed)):
        detail = getattr(exc, "detail", None) or "身份认证信息未提供。"
        return Response(
            {"detail": str(detail), "code": "not_authenticated"},
            status=401,
        )

    response = drf_exception_handler(exc, context)
    if response is not None:
        # 权限/CSRF 等其它 4xx：补一个稳定 code，方便前端区分（文案可能被本地化）
        if response.status_code == 403 and isinstance(response.data, dict):
            response.data.setdefault("code", "permission_denied")
        elif response.status_code == 404 and isinstance(response.data, dict):
            response.data.setdefault("code", "not_found")
        # **每个错误体都要有可显示的 detail**（处置清单 AF-20）：DRF 的字段级校验
        # 返回的是 ``{"file": "…请重新上传同名文件补齐"}``，没有 ``detail`` —— 前端
        # 只取 detail 就会把"唯一可执行的修复动作"丢掉（用户只看到一句通用失败）。
        if isinstance(response.data, dict) and not response.data.get("detail"):
            parts = []
            for field, messages in response.data.items():
                if field in {"code"}:
                    continue
                text = messages if isinstance(messages, str) else "; ".join(
                    str(item) for item in (messages if isinstance(messages, (list, tuple)) else [messages])
                )
                parts.append(("" if field in {"non_field_errors", "detail"} else f"{field}: ") + text)
            if parts:
                response.data["detail"] = " ".join(parts)
        return response

    view = context.get("view") if context else None
    logger.exception("unhandled API error in %s", view.__class__.__name__ if view else "unknown")
    return Response(
        {"detail": "服务内部错误，已记录日志", "code": "internal_error"},
        status=500,
    )
