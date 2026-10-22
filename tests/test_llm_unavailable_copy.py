"""模型不可用时的**文案**回归（用户选定：只改文案，不动 token 预算）。

背景（真实反馈）：用户看到徽标/提示以为"模型挂了"。实际原因是
**思考型模型把 token 预算用在推理上**，撞上限后 `content` 为空，
后端已能讲清原因，但**前端把 detail 丢掉了**，只剩笼统的"不可用"。

本文件锁死：
  * 后端空答复文案必须**区分**"预算被推理吃光"与"模型真的没说话"，
    并给出可操作建议；
  * 写文档的 503 提示必须**带出服务端 detail**（不再写死笼统文案）；
  * 顶部徽标必须区分"**已配置·未探测**"与"**未配置**"，
    不能让用户把"未探测"读成"不可用"。
"""

from __future__ import annotations

from pathlib import Path

from apps.planner.llm import LlmReply, _empty_reply_reason

WEBUI = Path("apps/webui/static/webui")


def _read(name: str) -> str:
    return (WEBUI / name).read_text(encoding="utf-8")


# --------------------------------------------------------------- 后端文案 ----


def test_empty_reply_reason_points_at_reasoning_budget():
    """空答复 + finish_reason=length → 必须点明"推理占用预算"并给建议。"""
    reply = LlmReply(
        content="",
        finish_reason="length",
        reasoning_tokens=922,
        usage={"completion_tokens": 4096},
    )
    reason = _empty_reply_reason(reply)
    assert "空答复" in reason
    assert "finish_reason=length" in reason
    assert "推理" in reason, reason
    assert "KB_LLM_MAX_TOKENS" in reason, "没有给出可操作建议"
    assert "922" in reason, "未带上推理 token 数（诊断信息）"


def test_empty_reply_reason_without_length_stays_generic():
    """模型真的什么都没说（finish_reason=stop）时，不应误报"被截断"。"""
    reply = LlmReply(content="", finish_reason="stop")
    reason = _empty_reply_reason(reply)
    assert "空答复" in reason
    assert "token 预算" not in reason, "stop 却说预算被占用，属于误报"


# --------------------------------------------------------------- 前端文案 ----


def test_document_503_surfaces_server_detail():
    """写文档失败时必须带出服务端 detail（旧实现把它丢了）。"""
    source = _read("kb-assistant.js")
    assert "原因：" in source, "未带出失败原因"
    assert '(result.body && result.body.detail) || "未提供原因"' in source, (
        "503 分支仍未读取服务端 detail"
    )
    # 聊天路径同样要带 detail（这条本来就对，锁住防回退）
    assert 'const detail = (result.body && result.body.detail) || "智能助手当前不可用"' in source


def test_env_badge_distinguishes_configured_from_unconfigured():
    """徽标必须区分"已配置·未探测" / "未配置" / "已禁用"，不暗示"不可用"。"""
    nav = _read("kb-nav.js")
    assert "模型已配置 · 未探测" in nav, "已配置却只显示「未检测」，容易被读成不可用"
    assert "模型未配置" in nav
    assert "模型调用已禁用" in nav
    # 旧文案不能再用作"已配置"态的**代码值**（注释里提到它属于说明，不算）
    assert 'parts.push("模型服务未检测")' not in nav
    assert "模型服务未检测" not in nav.split("*/")[0].split("//")[0], (
        "仍以「模型服务未检测」作为已配置态文案"
    )
    # tooltip 要说明"本页不探测"并提供实测入口
    assert "本页不主动探测" in nav and "测试连通" in nav
