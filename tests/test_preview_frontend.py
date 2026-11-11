"""前端预览统一入口的回归（源码级断言，与 `test_webui.py` 同风格）。

用户复现的缺陷：三个入口（资料列表 / 检索结果 / 任务列表）各自把 `chunks.text`
拼成 `<p>` —— PDF 原页/表格/水印全丢，水印混进正文。修复后三处必须**统一**到
`KB.previewFile`，且预览实现必须按格式真实呈现原件、并遵守有界与诚实标注。
"""

from __future__ import annotations

from pathlib import Path

import pytest

WEBUI = Path("apps/webui/static/webui")

ENTRIES = {
    "files": ("kb-files.js", "openPreview"),
    "search": ("kb-search.js", "previewFile"),
    "pages": ("kb-pages.js", "openTaskPreview"),
}


def _read(name: str) -> str:
    return (WEBUI / name).read_text(encoding="utf-8")


def test_shared_entry_exists():
    common = _read("kb-common.js")
    assert "KB.previewFile = async function" in common, "统一入口未定义"
    # 预览只读本地原件并回给已授权用户：不得调用任何模型接口
    for forbidden in ("/api/v1/planner/llm", "chat/completions", "api.openai.com"):
        assert forbidden not in common, f"预览里出现了模型调用：{forbidden}"


@pytest.mark.parametrize("key", sorted(ENTRIES))
def test_three_entries_delegate_to_shared_preview(key):
    """三个入口都必须**委托**给 KB.previewFile，而不是各自拼索引文字。"""
    filename, func = ENTRIES[key]
    source = _read(filename)
    assert "KB.previewFile(" in source, f"{filename} 未接入统一预览入口"
    assert "chunks.map" not in source, f"{filename} 仍在把索引分段当预览渲染"


def test_pdf_is_rendered_by_page_with_bounded_navigation():
    common = _read("kb-common.js")
    # 按页取图 + 页码有界
    assert "meta.page_url.replace" in common, "未使用按页渲染端点"
    assert "Math.max(1, Math.min(meta.page_count, page))" in common, "页码未做边界约束"
    # **真实尺寸缩放**：宽度按百分比设置（放大后横向滚动），而不是被 max-width 压回容器宽
    assert "img.style.width = Math.round(displayZoom * 100)" in common, "缩放未改变显示尺寸"
    assert "displayZoom = 1" in common, "默认不是适合宽度"
    assert 'data-zoom="fit"' in common, "缺少适合宽度复位"
    assert "displayZoom <= ZOOM_MIN" in common and "displayZoom >= ZOOM_MAX" in common, "缩放上下限未禁用"
    # 加载中 / 失败态：不静默破图
    assert "正在加载" in common and "页面加载失败" in common and "data-retry" in common
    assert 'addEventListener("error"' in common, "缺少图片加载失败处理"
    # 原件缺失时不得仍提供会 404 的下载入口
    assert "原件缺失，无法下载" in common
    # 结构化内容超过上限要明确提示
    assert "blocks_truncated" in common and "已按上限截断显示" in common


def test_text_and_structured_rendering_rules():
    common = _read("kb-common.js")
    # 文本：保留换行 + 转义（转义函数必须真的被用上）
    assert "preview-text" in common and "KB.esc(meta.text" in common
    css = _read("kb-preview.css")
    assert "white-space: pre-wrap" in css, "文本预览未保留换行"
    # DOCX/XLSX：结构化渲染，且把后端的"非原始版式"说明显示出来
    assert "meta.blocks" in common and "blockHtml" in common
    assert "KB.esc(meta.note" in common, "未展示后端给出的口径说明（如非原始版式）"


def test_missing_original_is_labelled_as_index_text_only():
    common = _read("kb-common.js")
    assert "原件缺失或不可访问" in common
    assert "索引文字（非原件）" in common, "索引文字未明确标注，容易被误当成原件"
    # 索引文字只在原始分支缺失/不支持时使用
    assert common.count("kbIndexText(fileId)") >= 2


def test_preview_css_is_loaded_without_touching_prototype_css():
    base = Path("apps/webui/templates/webui/base.html").read_text(encoding="utf-8")
    assert "kb-preview.css" in base, "预览样式未加载（滚动/换行会失效）"
    # 无引用的原型样式表已删除：不允许复活（复活即重新引入旧世界视觉）
    assert not Path("apps/webui/static/webui/kb-proto.css").exists(), (
        "kb-proto.css 是无引用的死文件，已删除，不能复活"
    )
