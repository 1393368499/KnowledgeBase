"""服务端 Markdown 渲染（与前端 ``kb-markdown.js`` **同一套规则**）。

为什么需要它：导出 HTML 时如果只是把 Markdown 原文塞进 ``<pre>``，
导出的文件与界面右侧预览**长得不一样**——用户会以为导出坏了。
所以这里按与前端一致的口径做真实渲染：

- 标题 ``#``~``####`` → ``h2``~``h5``（页面语义下 h1 留给文档标题）；
- **粗体** / *斜体* / ``行内代码`` / ``[文字](链接)``（协议白名单）；
- 围栏代码块、无序/有序列表、引用、表格、分隔线。

安全口径与前端一致：**先 HTML 转义，再只生成白名单标签**；
链接只允许 ``http(s)://`` 与站内相对路径，其它协议（如 ``javascript:``）
只保留文字、不生成 ``<a>``。这样导出文件不会被文档内容注入脚本。

与前端保持"规则一致"而不是"共享代码"：前端是 JS、后端是 Python，
无法真正共用一份实现；因此两边都以此处列出的规则为准，改动需同步两处。
"""

from __future__ import annotations

import re
from html import escape

_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_HEADING = re.compile(r"^(#{1,4})\s+(.*)$")
_RULE = re.compile(r"^\s*([-*_])\1{2,}\s*$")
_FENCE = re.compile(r"^\s*```")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_DIVIDER = re.compile(r"^:?-{2,}:?$")
_BLOCKQUOTE = re.compile(r"^\s*>\s?(.*)$")

_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC = re.compile(r"(^|[^*])\*([^*]+)\*")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def _inline(text: str) -> str:
    """行内元素：先转义，再只替换白名单语法。"""
    out = escape(text, quote=True)
    out = _INLINE_CODE.sub(lambda m: f"<code>{m.group(1)}</code>", out)
    out = _BOLD.sub(lambda m: f"<strong>{m.group(1)}</strong>", out)
    out = _ITALIC.sub(lambda m: f"{m.group(1)}<em>{m.group(2)}</em>", out)

    def _link(match: re.Match[str]) -> str:
        label, href = match.group(1), match.group(2)
        # 注意：href 已在转义后的文本里（& 变成 &amp;），协议判断用原文更可靠，
        # 这里用转义结果判断足够——白名单前缀不受转义影响。
        if not re.match(r"^(https?://|/)", href, flags=re.IGNORECASE):
            return label
        return f'<a href="{href}" rel="noopener noreferrer">{label}</a>'

    return _LINK.sub(_link, out)


def render_markdown(markdown: str) -> str:
    """Markdown → 安全 HTML（与前端渲染规则一致）。"""
    lines = str(markdown or "").splitlines()
    html: list[str] = []
    paragraph: list[str] = []
    in_code = False
    list_type = ""
    in_table = False

    def flush_paragraph() -> None:
        if paragraph:
            html.append("<p>" + _inline(" ".join(paragraph)) + "</p>")
            paragraph.clear()

    def close_list() -> None:
        nonlocal list_type
        if list_type:
            html.append(f"</{list_type}>")
            list_type = ""

    def close_table() -> None:
        nonlocal in_table
        if in_table:
            html.append("</tbody></table>")
            in_table = False

    for raw in lines:
        line = raw.rstrip()

        if _FENCE.match(line):
            flush_paragraph()
            close_list()
            close_table()
            if in_code:
                html.append("</code></pre>")
                in_code = False
            else:
                html.append("<pre><code>")
                in_code = True
            continue
        if in_code:
            html.append(escape(raw, quote=False))
            continue

        if not line.strip():
            flush_paragraph()
            close_list()
            close_table()
            continue

        if _RULE.match(line):
            flush_paragraph()
            close_list()
            close_table()
            html.append("<hr>")
            continue

        heading = _HEADING.match(line)
        if heading:
            flush_paragraph()
            close_list()
            close_table()
            level = min(len(heading.group(1)) + 1, 5)
            html.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            continue

        quote = _BLOCKQUOTE.match(line)
        if quote:
            flush_paragraph()
            close_list()
            close_table()
            html.append(f"<blockquote>{_inline(quote.group(1))}</blockquote>")
            continue

        if _TABLE_ROW.match(line):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if not in_table:
                flush_paragraph()
                close_list()
                html.append("<table><thead><tr>")
                html.extend(f"<th>{_inline(cell)}</th>" for cell in cells)
                html.append("</tr></thead><tbody>")
                in_table = True
                continue
            if all(_TABLE_DIVIDER.match(cell) for cell in cells):
                continue  # 分隔行
            html.append("<tr>")
            html.extend(f"<td>{_inline(cell)}</td>" for cell in cells)
            html.append("</tr>")
            continue
        close_table()

        bullet = _BULLET.match(line)
        ordered = _ORDERED.match(line)
        match = bullet or ordered
        if match is not None:
            flush_paragraph()
            want = "ul" if bullet else "ol"
            if list_type != want:
                close_list()
                html.append(f"<{want}>")
                list_type = want
            html.append(f"<li>{_inline(match.group(1))}</li>")
            continue
        close_list()

        paragraph.append(line.strip())

    flush_paragraph()
    close_list()
    close_table()
    if in_code:
        html.append("</code></pre>")
    return "\n".join(html)


__all__ = ["render_markdown"]
