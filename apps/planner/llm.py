"""首页 AI 会话用的 LLM 客户端（OpenAI 兼容 + 统一韧性层）。

设计口径（用户明确要求）
------------------------
1. **真实模型，不伪造回答**：模型服务不可用时抛
   :class:`~apps.knowledge.resilience.ServiceUnavailable`，由上层如实告诉用户
   "智能助手当前不可用"。**绝不**用模板/假答案顶替，也绝不返回空字符串让界面
   显示"AI 说：无内容"——那等于骗人。
2. 复用 ``apps.knowledge.resilience``：超时 / 重试上限 / 熔断 / 离线开关
   （``KB_MODEL_CALLS_DISABLED=1`` 时立刻失败且**不发起任何网络请求**）。
3. 支持"工具调用"（function calling）：AI 只能**提出**操作，不能自己执行；
   执行必须走权限核验 + 用户确认（见 ``apps/planner/services.py``）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace
from typing import Any

import httpx
from django.conf import settings

from apps.knowledge.httpclient import build_http_client
from apps.knowledge.resilience import (
    ServiceUnavailable,
    breaker_for,
    disabled_service,
    model_calls_disabled,
    post_json,
)

logger = logging.getLogger(__name__)

#: vLLM 判定 greedy 的阈值（源码里的 ``_SAMPLING_EPS``）：``temperature`` 低于它
#: 就按 greedy 算，达到它就是随机采样。
SAMPLING_EPS = 1e-5

#: "已统一压成 greedy"的告警只打一次：按请求打会把日志刷满，反而没人看。
_greedy_downgrade_notified = False


def greedy_only() -> bool:
    """本系统是否强制所有 LLM 请求走 greedy（默认是）。

    默认值及其原因写在 ``config/settings.py`` 的 ``KB_LLM_GREEDY_ONLY`` 旁边：
    AS711 的算能 vLLM-TPU 引擎一旦在同一 batch 里混到 greedy + random，就因
    ``aten::where.self_out`` 未实现而**整个引擎退出**（2026-10-19 实测两次）。
    我们不发 random 请求，就永远不会是"混采"里那一半。
    """
    return bool(getattr(settings, "KB_LLM_GREEDY_ONLY", True))


def effective_temperature(temperature: float) -> float:
    """把 profile 上的 temperature 折算成**实际发出去**的值。

    这是全系统唯一的采样口径出口：``LlmClient.chat`` 构造请求体时也走这里，所以
    即便有人直接 ``LlmProfile(temperature=0.3)`` 或用 ``dataclasses.replace`` 造出
    随机 profile，请求仍会以 greedy 发出。只在 profile 上做约束是不够的——新写的
    调用点很容易绕过 profile 构造，而请求体是绕不过去的。
    """
    global _greedy_downgrade_notified
    value = float(temperature)
    if not greedy_only():
        return value
    if not _greedy_downgrade_notified and value >= SAMPLING_EPS:
        logger.info(
            "采样统一为 greedy：本应发出的 temperature=%s 被压成 0.0"
            "（AS711 算能 vLLM-TPU 引擎混采会整体崩，见 settings.KB_LLM_GREEDY_ONLY）",
            value,
        )
        _greedy_downgrade_notified = True
    return 0.0


class LLMError(RuntimeError):
    """LLM 调用失败（配置错误、响应结构异常等）。"""


def input_budget(profile) -> int:
    return (int(getattr(settings, 'KB_LLM_CONTEXT_TOKENS', 262144)) - profile.max_tokens
            - int(getattr(settings, 'KB_LLM_CONTEXT_RESERVE_TOKENS', 4096)))


def message_size(messages, tools=None) -> int:
    data = {'messages': normalize_messages(messages)}
    if tools:
        data['tools'] = tools
    return len(json.dumps(data, ensure_ascii=False).encode('utf-8'))


@dataclass(frozen=True)
class LlmProfile:
    """LLM 端点配置。默认取 ``MODEL_SERVICES['llm']``。"""

    base_url: str
    model: str
    api_key: str = ""
    timeout_seconds: float = 60.0
    #: 默认预算。实测：Qwen3.6 回答 4 个字就用掉 239 completion_tokens
    #: （推理过程占绝大部分），2048 对"写一份文档"是偏紧的，因此提到 4096。
    #: 真正的上限由 ``KB_LLM_MAX_TOKENS`` 控制。
    max_tokens: int = 4096
    #: 采样温度。**不直接决定发出去什么**：:func:`effective_temperature` 会按
    #: ``KB_LLM_GREEDY_ONLY`` 统一折算（默认为 greedy，原因见该函数）。
    temperature: float = 0.2
    #: 允许的最大工具调用轮次（防止模型来回调用把请求拖死）
    max_tool_rounds: int = 4

    @property
    def configured(self) -> bool:
        return bool(self.base_url.strip() and self.model.strip())


@dataclass
class ToolCall:
    """模型提出的一次工具调用（**只是提案，不是执行**）。"""

    call_id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    raw_arguments: str = ""
    #: 非空表示这次工具调用**协议上无效**（非法 JSON / 非对象 / 无名等）。
    #: 上层必须据此拒绝，不能把 arguments 当成 {} 去提案 ——
    #: 对"参数全可选"的工具来说 {} 是合法的，静默吞掉畸形输入会变成有效提案。
    invalid_reason: str = ""

    @property
    def is_valid(self) -> bool:
        return not self.invalid_reason


@dataclass
class LlmReply:
    """一次模型回复：要么是文本，要么是工具调用提案（也可能两者都有）。"""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    #: 供应商给的结束原因（stop / length / tool_calls …）。
    #: 必须读它：``length`` 表示 token 预算用尽，正文可能是空的。
    finish_reason: str = ""
    #: 推理模型单独计费的推理 token 数（``usage.completion_tokens_details``）。
    #: 它只用于**诊断**，绝不会被当成答复内容。
    reasoning_tokens: int = 0

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    @property
    def is_empty(self) -> bool:
        """既没有正文也没有工具调用 = 这次模型没给出任何可用结果。"""
        return not str(self.content or "").strip() and not self.tool_calls


def document_profile() -> LlmProfile:
    """撰写派生文档用的 profile：正文长，需要比对话更大的预算。

    实测依据同上：推理开销是固定的大头，写一份几百字的文档很容易超过 4096。
    可用 ``KB_LLM_DOC_MAX_TOKENS`` 覆盖。
    """
    base = default_profile()
    return replace(
        base,
        max_tokens=int(getattr(settings, "KB_LLM_DOC_MAX_TOKENS", 8192)),
        timeout_seconds=float(getattr(settings, "KB_LLM_DOC_TIMEOUT_SECONDS", 180)),
    )


def default_profile() -> LlmProfile:
    services = getattr(settings, "MODEL_SERVICES", {}) or {}
    llm = services.get("llm", {}) or {}
    return LlmProfile(
        base_url=str(llm.get("base_url", "")).rstrip("/"),
        model=str(llm.get("model", "")),
        api_key=str(getattr(settings, "MODEL_API_KEY", "") or ""),
        timeout_seconds=float(getattr(settings, "KB_LLM_TIMEOUT_SECONDS", 60)),
        max_tokens=int(getattr(settings, "KB_LLM_MAX_TOKENS", 4096)),
        # 读设置再统一折算：``KB_LLM_GREEDY_ONLY`` 默认打开时这里就是 0.0，
        # 即 profile 本身就如实反映"会发 greedy"，不是等发请求前再偷偷改。
        temperature=effective_temperature(
            float(getattr(settings, "KB_LLM_TEMPERATURE", 0.2))
        ),
    )


def normalize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把**开头连续的 system 消息合并成一条**，保证只有一条 system 且在最前。

    为什么必须做（实测阻塞）：AS711 的服务模板要求 system 只能出现在开头，
    收到两条 system 会直接 **HTTP 400 "System message must be at the beginning."**。
    调用方很容易顺手再追加一条（比如"以下是检索到的资料"），
    所以在客户端做一层规范化，避免同类问题再次以 503 的形式暴露给用户。

    行为：
    - 合并**开头连续**的所有 system（内容按顺序用空行连接）；
    - 中间/末尾出现的 system 会被**降级为 user**（否则仍是非法位置），
      并在内容前加注标记，保持信息不丢；
    - 其余消息顺序不变（不重排，避免破坏工具调用与结果的配对）。
    """
    if not messages:
        return []

    normalized: list[dict[str, Any]] = []
    system_parts: list[str] = []
    index = 0
    while index < len(messages) and messages[index].get("role") == "system":
        system_parts.append(str(messages[index].get("content") or ""))
        index += 1

    if system_parts:
        normalized.append({"role": "system", "content": "\n\n".join(system_parts)})

    for message in messages[index:]:
        if message.get("role") == "system":
            # 非法位置：转成 user，保留原文并注明来源角色
            normalized.append(
                {
                    "role": "user",
                    "content": "[系统补充说明]\n" + str(message.get("content") or ""),
                }
            )
        else:
            normalized.append(message)
    return normalized


def _truncated_reason(reply: LlmReply) -> str:
    """``finish_reason=length``：模型被 token 上限截断。

    为什么**不能**当成完成（用户实测依据）：Qwen3.6 是推理模型，回答 4 个字也要
    239 completion_tokens；一旦撞上限，正文会被**从中间截断**，工具调用的
    ``arguments`` 也可能是半截 JSON。这两种情况都不可用：

    - 截断的正文拿去写文档 → 落一份"看起来成功、实际缺半句"的文档；
    - 截断的 arguments → 参数不完整，执行它等于拿模型的草稿去动真实数据。

    所以一律显式失败，让调用方重试（或提高预算），而不是悄悄放过。
    """
    parts = ["模型输出被 token 上限截断（finish_reason=length），本次结果不可用"]
    if reply.reasoning_tokens:
        parts.append(f"其中推理约 {reply.reasoning_tokens} tokens")
    usage = reply.usage or {}
    if "completion_tokens" in usage:
        parts.append(f"completion_tokens={usage['completion_tokens']}")
    if reply.tool_calls:
        parts.append("工具调用参数可能不完整，已丢弃，不会生成提案")
    parts.append("请提高 KB_LLM_MAX_TOKENS（或缩短输入）后重试")
    return "；".join(parts)


def _empty_reply_reason(reply: LlmReply) -> str:
    """把"空答复"讲清楚：是预算被推理吃掉，还是模型真的什么都没说。"""
    parts = ["模型返回了空答复（content 为空且没有工具调用）"]
    if reply.finish_reason:
        parts.append(f"finish_reason={reply.finish_reason}")
    if reply.finish_reason == "length":
        parts.append(
            "token 预算在用完前被推理过程占用"
            + (f"（推理约 {reply.reasoning_tokens} tokens）" if reply.reasoning_tokens else "")
            + "，因此没有产出最终答复；请提高 KB_LLM_MAX_TOKENS 后重试"
        )
    usage = reply.usage or {}
    if usage:
        parts.append(
            "usage="
            + ", ".join(f"{key}={usage[key]}" for key in ("prompt_tokens", "completion_tokens", "total_tokens") if key in usage)
        )
    return "；".join(parts)


class LlmClient:
    """OpenAI 兼容的 chat/completions 客户端（含工具调用）。"""

    def __init__(
        self,
        profile: LlmProfile | None = None,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.profile = profile or default_profile()
        self._client = client
        self._last_error = ""

    # ------------------------------------------------------------- 元信息 ----

    @property
    def service_name(self) -> str:
        """熔断器/日志用的稳定标识。

        不要用 ``self.profile.name``——``LlmProfile`` 没有该字段
        （同类缺陷在 rerank/ocr 上都出现过，靠真实调用才暴露）。
        """
        return f"llm:{self.profile.base_url}|{self.profile.model}"

    @property
    def configured(self) -> bool:
        return self.profile.configured

    @property
    def available(self) -> bool:
        """**是否可以尝试**调用。

        与 rerank/ocr 同一口径：configured 就允许发起首次调用；
        "是否真的能用"由调用结果决定（并交给熔断器记忆），
        不在这里预先否定——否则第一次永远是"不可用"。
        """
        return self.configured

    @property
    def last_error(self) -> str:
        return self._last_error

    # --------------------------------------------------------------- 调用 ----

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
    ) -> LlmReply:
        """调用模型。失败抛 :class:`ServiceUnavailable`（**不返回假回答**）。"""
        if not self.configured:
            raise LLMError(
                "未配置智能助手端点（MODEL_SERVICES['llm'].base_url / model 为空）"
            )
        if model_calls_disabled():
            # 离线开关：连 http 客户端都不造，也不发任何请求
            raise disabled_service(self.service_name)

        payload: dict[str, Any] = {
            "model": self.profile.model,
            # 规范化后**只有一条 system 且在最前**（AS711 模板要求）
            "messages": normalize_messages(messages),
            # 唯一出口：手搓 profile / replace 造出的随机温度也在这里被压成 greedy
            "temperature": effective_temperature(self.profile.temperature),
            "max_tokens": self.profile.max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice
        if message_size(messages, tools) > input_budget(self.profile):
            raise LLMError("本次资料超出模型的安全上下文预算；请分批整理资料后再生成，未发送超长请求")
        headers = {"Content-Type": "application/json"}
        if self.profile.api_key:
            headers["Authorization"] = f"Bearer {self.profile.api_key}"

        client = self._client or build_http_client(
            timeout=self.profile.timeout_seconds
        )
        close_after = self._client is None
        try:
            body = post_json(
                http=client,
                url=f"{self.profile.base_url}/chat/completions",
                payload=payload,
                headers=headers,
                service=self.service_name,
                max_attempts=int(getattr(settings, "KB_LLM_RETRIES", 1)) + 1,
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
            self._last_error = exc.reason
            raise
        except Exception as exc:  # noqa: BLE001 - 统一包装
            self._last_error = f"智能助手调用失败：{exc}"
            raise LLMError(self._last_error) from exc
        finally:
            if close_after:
                client.close()

        return self._parse_reply(body)

    # ------------------------------------------------------------- 解析 ----

    def _parse_reply(self, body: Any) -> LlmReply:
        if not isinstance(body, dict):
            raise LLMError(f"响应不是 JSON 对象：{str(body)[:200]}")
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMError(f"响应缺少 choices：{str(body)[:200]}")
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") or {}
        if not isinstance(message, dict):
            raise LLMError(f"响应 message 结构异常：{str(first)[:200]}")

        content = message.get("content") or ""
        if isinstance(content, list):
            # 有些实现把 content 分成多个片段
            content = "".join(
                str(part.get("text", "")) if isinstance(part, dict) else str(part)
                for part in content
            )

        tool_calls: list[ToolCall] = []
        raw_calls = message.get("tool_calls")
        if raw_calls is not None and not isinstance(raw_calls, list):
            # 协议错误：tool_calls 必须是数组
            raise LLMError(
                f"工具调用协议错误：tool_calls 应为数组，实际 {type(raw_calls).__name__}"
            )
        for index, raw in enumerate(raw_calls or []):
            call_id = f"call_{index}"
            if not isinstance(raw, dict):
                tool_calls.append(
                    ToolCall(
                        call_id=call_id,
                        name="",
                        invalid_reason=f"工具调用项不是对象（{type(raw).__name__}）",
                    )
                )
                continue
            call_id = str(raw.get("id") or call_id)
            function = raw.get("function")
            if not isinstance(function, dict):
                tool_calls.append(
                    ToolCall(
                        call_id=call_id,
                        name="",
                        invalid_reason="工具调用的 function 不是对象",
                    )
                )
                continue
            name = str(function.get("name") or "").strip()
            if not name:
                tool_calls.append(
                    ToolCall(call_id=call_id, name="", invalid_reason="工具调用缺少函数名")
                )
                continue

            arguments_text = function.get("arguments")
            invalid_reason = ""
            arguments: dict[str, Any] = {}
            if arguments_text is None or arguments_text == "":
                # 无参数是合法的（例如 get_stats）
                arguments = {}
            elif isinstance(arguments_text, dict):
                arguments = dict(arguments_text)
            elif isinstance(arguments_text, str):
                try:
                    parsed = json.loads(arguments_text)
                except ValueError:
                    invalid_reason = "参数不是合法 JSON"
                    logger.warning(
                        "工具调用参数不是合法 JSON：%s", arguments_text[:200]
                    )
                else:
                    if isinstance(parsed, dict):
                        arguments = parsed
                    else:
                        invalid_reason = (
                            f"参数 JSON 不是对象（{type(parsed).__name__}）"
                        )
            else:
                invalid_reason = f"参数类型不支持（{type(arguments_text).__name__}）"

            tool_calls.append(
                ToolCall(
                    call_id=call_id,
                    name=name,
                    arguments=arguments if not invalid_reason else {},
                    raw_arguments=str(arguments_text)[:500],
                    invalid_reason=invalid_reason,
                )
            )

        usage = dict(body.get("usage") or {})
        details = usage.get("completion_tokens_details")
        reasoning_tokens = 0
        if isinstance(details, dict):
            try:
                reasoning_tokens = int(details.get("reasoning_tokens") or 0)
            except (TypeError, ValueError):
                reasoning_tokens = 0

        reply = LlmReply(
            content=str(content or ""),
            tool_calls=tool_calls,
            model=str(body.get("model") or self.profile.model),
            usage=usage,
            finish_reason=str(first.get("finish_reason") or ""),
            # 注意：这里**只**读 message.content。
            # 有些推理模型把思考过程放在 message.reasoning_content 里，
            # 那**不是**答复——绝不能用它兜底当答案，否则用户会看到模型的草稿。
            reasoning_tokens=reasoning_tokens,
        )
        # 顺序很重要：先判"被截断"，再判"空"。
        # 截断但正文非空也必须失败——那半句话不能被当完成。
        if reply.finish_reason == "length":
            raise LLMError(_truncated_reason(reply))
        if reply.is_empty:
            # HTTP 200 但什么都没产出 → 明确失败，不返回"成功但空"。
            raise LLMError(_empty_reply_reason(reply))
        return reply




__all__ = [
    "LLMError",
    "LlmClient",
    "LlmProfile",
    "LlmReply",
    "ToolCall",
    "default_profile",
    "document_profile",
    "normalize_messages",
]
