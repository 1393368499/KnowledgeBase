"""设置页两处修复的回归：探测目标绑定 + 文案。

真实缺陷（用户实测）：
  * **四个「测试连通」都返回同一个模型**：前端用"显示名 → 探测目标"的映射表，
    表里写的是「向量模型/重排模型/OCR 引擎」，而行的真实名字是
    「向量服务/重排服务/OCR 服务」——三个都匹配不上，全部落到
    `|| "embedding"` 兜底，于是点谁都只探 BGE。
  * 文案「本地模型」按用户要求改为「模型」。
"""

from __future__ import annotations

from pathlib import Path

WEBUI = Path("apps/webui/static/webui")


def _read(name: str) -> str:
    return (WEBUI / name).read_text(encoding="utf-8")


# ------------------------------------------------- 探测目标必须逐行绑定 ----


def test_each_row_declares_its_own_probe_target():
    pages = _read("kb-pages.js")
    for target in ('target: "embedding"', 'target: "rerank"', 'target: "ocr"', 'target: "llm"'):
        assert target in pages, f"缺少行级探测目标：{target}"


def test_probe_button_carries_target_and_handler_reads_it():
    pages = _read("kb-pages.js")
    assert "data-test-target=" in pages, "按钮没有挂目标（只能靠文案猜）"
    assert 'button.getAttribute("data-test-target")' in pages, "处理器未读取按钮上的目标"
    # 白名单校验（后端只认这四个键；不合法不允许任意透传）
    assert 'ALLOWED_TARGETS = ["embedding", "rerank", "ocr", "llm"]' in pages
    assert "ALLOWED_TARGETS.indexOf(rawTarget) >= 0" in pages


def test_no_display_name_to_target_map_left():
    """**防回退**：不允许再用"显示名 → 目标"的脆弱映射。"""
    pages = _read("kb-pages.js")
    assert "targetMap" not in pages, "又出现了按显示名映射目标的实现"
    # 具体那几个错键也不该再出现
    for stale in ('"向量模型": "embedding"', '"重排模型": "rerank"', '"OCR 引擎": "ocr"'):
        assert stale not in pages, f"残留会兜底成 embedding 的旧映射：{stale}"


def test_probe_result_fills_only_clicked_row():
    pages = _read("kb-pages.js")
    assert 'cell.getAttribute("data-latency-row") !== name' in pages, (
        "结果未按被点击的行过滤，会串行填到别的行"
    )
    # 非嵌入目标必须标注"仅连通性检查"，不冒充能力已验证
    assert "connectivity_only" in pages
    assert "未接入" in pages, "llm 未接入探测时必须如实显示"


# ------------------------------------------------------------- 文案 ----


def test_local_model_copy_is_renamed():
    """按用户要求：`本地模型` → `模型`（可见文案里不再出现这四个字）。"""
    for name in ("kb-pages.js", "kb-assistant.js"):
        source = _read(name)
        assert "本地模型" not in source, f"{name} 仍残留「本地模型」"
    pages = _read("kb-pages.js")
    assert '<div class="card-title">模型</div>' in pages, "卡片标题未改成「模型」"
