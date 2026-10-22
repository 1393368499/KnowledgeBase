"""AI 派生文档的**真实**导出：Markdown / HTML / DOCX / PDF。

这个模块只做一件事：把一份"长得像 ``DerivedDocument``"的对象
（只要有 ``title`` / ``content_markdown`` / ``pk``）变成可以直接写进
``HttpResponse`` 的字节 + 文件名 + MIME，**纯函数式**、不碰 ORM、不碰 request。

设计口径（每一条都由 ``tests/test_document_export.py`` 守住）
----------------------------------------------------------------

* **MD / HTML 沿用已有渲染规则**：HTML 直接复用
  :func:`apps.planner.markdown.render_markdown`（与界面右侧预览同一套规则），
  不另抄一份实现，避免两处规则漂移。中文按 UTF-8 输出，``<meta charset="utf-8">``
  随正文一起走，表格/列表/引用都保留真实标签。

* **DOCX 自己写 OOXML**：``python-docx`` 是否安装**不影响结果**（装了也不用），
  产物是一个合法的 OPC zip 包：

  ==============================  ===============================================
  部件                            作用
  ==============================  ===============================================
  ``[Content_Types].xml``         声明各部件类型（必须是包里第一个部件）
  ``_rels/.rels``                 包级关系 → ``word/document.xml`` / ``docProps``
  ``word/document.xml``          正文；中文以 UTF-8 字面量直接写入
  ``word/styles.xml``             默认字号/行距 + ``Normal`` 样式
  ``word/numbering.xml``          无序/有序列表的编号定义（``numId`` 1 / 2）
  ``word/_rels/document.xml.rels``styles / numbering 关系
  ``docProps/core.xml``           标题等文档属性（PDF 转换后也会带上）
  ==============================  ===============================================

  结构映射：``#``→1 级标题（文档标题单独占最高一级）… ``####``→4 级；
  ``-``/``*``/``+``→无序列表；``1.``→有序列表；``| a | b |``→真实 ``w:tbl`` 表格
  （首行为表头且加粗）；````` ``` `````→等宽代码段；``>``→引用；``---``→分隔线。
  行内 ``**粗体**`` / ``` `行内代码` `` `` 支持。所有 ``w:t`` 都带
  ``xml:space="preserve"``，避免首尾空格被吞。

* **PDF 复用 ``apps.knowledge.office_convert`` 的受限运行器**：先由本模块生成
  DOCX，再交给镜像里的 LibreOffice（``soffice --headless --convert-to pdf``）。
  隔离 user profile、禁宏/禁外链、超时杀**整个进程组**、产物符号链接与体积上限
  这些加固全部来自那套实现 —— 本模块**不复制任何 subprocess 代码**，
  只做"组装 -> 调用 -> 取字节"，并且**没有 LibreOffice 时明确报错**，
  绝不返回假 PDF（真正的 PDF 必须以 ``%PDF-`` 开头）。

* **文件名安全化**：``<安全化后的标题>.<ext>``。去掉路径分隔符与控制字符、
  折叠 ``..``、避开 Windows 保留名（``CON``/``NUL``…）、限长（字符 + UTF-8 字节
  双重上限），**保留中文**；空标题兜底 ``document``。
"""

from __future__ import annotations

import io
import re
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .markdown import render_markdown

__all__ = [
    "SUPPORTED_FORMATS",
    "CONTENT_TYPES",
    "FORMAT_EXTENSIONS",
    "ExportPayload",
    "ExportError",
    "UnsupportedExportFormat",
    "ExportUnavailable",
    "normalize_format",
    "safe_filename_stem",
    "export_filename",
    "render_document_html",
    "markdown_to_docx",
    "docx_to_pdf",
    "find_soffice",
    "soffice_available",
    "build_export",
    "content_disposition",
]

# --------------------------------------------------------------------- 常量 ----

#: 对外承诺支持的导出格式（顺序即文档里的顺序）。
SUPPORTED_FORMATS: tuple[str, ...] = ("md", "html", "docx", "pdf", "pptx")

#: 格式 → MIME。全部带 ``charset=utf-8``（文本两件）；二进制两件不带。
CONTENT_TYPES: dict[str, str] = {
    "md": "text/markdown; charset=utf-8",
    "html": "text/html; charset=utf-8",
    "docx": (
        "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document"
    ),
    "pdf": "application/pdf",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

#: 格式 → 扩展名（HTML 用 ``.html``，与界面/下载端点既有口径一致）。
FORMAT_EXTENSIONS: dict[str, str] = {
    "md": "md",
    "html": "html",
    "docx": "docx",
    "pdf": "pdf",
    "pptx": "pptx",
}

#: 常见别名 → 规范格式名（服务层/视图层此前已经在做同样的归一化）。
FORMAT_ALIASES: dict[str, str] = {
    "markdown": "md",
    "m": "md",
    "text": "md",
    "htm": "html",
    "doc": "docx",  # 老口径里 "doc" 指 Word 文档；这里落到真正能用的 .docx
    "word": "docx",
}

#: 文件名主干的最大**字符**数（中文按 1 个字符计）。
MAX_STEM_LENGTH = 80
#: 文件名主干的最大 **UTF-8 字节**数（多数文件系统上限 255 字节，留足余量）。
MAX_STEM_BYTES = 200

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_CP_NS = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
_DC_NS = "http://purl.org/dc/elements/1.1/"
_DCTERMS_NS = "http://purl.org/dc/terms/"

_XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'

#: XML 1.0 **不允许**的字符（控制字符与 DEL/C1）；文档正文里可能出现，
#: 直接写进 XML 会让 LibreOffice/Word 打不开，因此统一剔除。
_XML_INVALID = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

#: 文件名里**必须**替换掉的字符：路径分隔符、Windows 保留字符、控制字符。
_UNSAFE_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
#: 连续的 ``..``（路径穿越的最小构件）：折叠成 ``_``。
_DOT_RUNS = re.compile(r"\.{2,}")

#: Windows 保留设备名（大小写不敏感，也不允许带扩展名出现）。
_RESERVED_STEMS = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

#: 空标题/纯符号标题的兜底主干。
FALLBACK_STEM = "document"

# --------------------------------------------------------------- Markdown ----

_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^(\s*)\d+[.)]\s+(.*)$")
_HEADING = re.compile(r"^\s*(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*```")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_DIVIDER = re.compile(r"^:?-{2,}:?$")
_BLOCKQUOTE = re.compile(r"^\s*>\s?(.*)$")
_RULE = re.compile(r"^\s*([-*_])\1{2,}\s*$")

#: 行内元素：``**粗体**`` 与 ``` `行内代码` ``（其余保留字面量）。
_INLINE_TOKEN = re.compile(r"\*\*(?P<bold>.+?)\*\*|`(?P<code>[^`]+)`")

#: DOCX 里标题的（字号[半磅], 大纲级别）：文档标题单独占最高一级。
_TITLE_SIZE = 36
_HEADING_SIZES = {1: 32, 2: 28, 3: 26, 4: 24}
_HEADING_OUTLINE = {1: 1, 2: 2, 3: 3, 4: 4}


# --------------------------------------------------------------------- 异常 ----


class ExportError(Exception):
    """导出失败的基类（调用方可以统一捕获它转成 4xx/5xx）。"""


class UnsupportedExportFormat(ExportError, ValueError):
    """未知格式。

    同时继承 :class:`ValueError`：纯函数式的"参数不合法"用 ``ValueError``
    更符合直觉，调用方按哪个捕获都能接住。
    """


class ExportUnavailable(ExportError):
    """环境不具备导出条件（当前只有一种：没有 LibreOffice 转不了 PDF）。

    这是一个**明确的失败**：绝不降级成"返回一个假 PDF"或"返回 Markdown 冒充"。
    """


# ----------------------------------------------------------------- 数据载体 ----


@dataclass(frozen=True)
class ExportPayload:
    """一次导出的全部结果；调用方直接拿去构造 ``HttpResponse``。"""

    content: bytes
    filename: str
    content_type: str
    note: str = ""
    #: 归一化之后的格式名（``md``/``html``/``docx``/``pdf``）。
    #: 调用方请求别名（``markdown``/``m``/``htm``/``doc``）时，用它回写
    #: ``X-Document-Format`` 之类的响应头，避免"说是 htm、实际 html"的不一致。
    format: str = ""

    @property
    def size(self) -> int:
        return len(self.content)


# ------------------------------------------------------------- 格式与文件名 ----


def normalize_format(fmt: str) -> str:
    """把用户输入归一化成 :data:`SUPPORTED_FORMATS` 里的名字。

    未知格式抛 :class:`UnsupportedExportFormat`（同时是 ``ValueError``）。
    """
    key = str(fmt or "").strip().lower().lstrip(".")
    key = FORMAT_ALIASES.get(key, key)
    if key not in SUPPORTED_FORMATS:
        raise UnsupportedExportFormat(
            f"不支持的导出格式：{fmt!r}；支持 {list(SUPPORTED_FORMATS)}"
            "（别名：markdown/m、htm、doc/word）"
        )
    return key


def _truncate_bytes(text: str, limit: int) -> str:
    """按 **UTF-8 字节**限长，且不切碎多字节字符。"""
    if len(text.encode("utf-8")) <= limit:
        return text
    out: list[str] = []
    used = 0
    for char in text:
        width = len(char.encode("utf-8"))
        if used + width > limit:
            break
        out.append(char)
        used += width
    return "".join(out)


def safe_filename_stem(
    title: str,
    *,
    max_length: int = MAX_STEM_LENGTH,
    max_bytes: int = MAX_STEM_BYTES,
    fallback: str = FALLBACK_STEM,
) -> str:
    """把标题压成**单个安全的文件名主干**（保留中文）。

    - 只取路径的最后一段（``../../etc/passwd`` → ``passwd`` 之前先丢掉前缀）；
    - 路径分隔符 ``/ \\``、Windows 保留字符 ``:*?"<>|``、控制字符 → ``_``；
    - 其余 Unicode 控制/格式字符（零宽连接符、方向覆写等）直接删除；
    - 连续 ``..`` 折叠，首尾的 ``.`` 与空格去掉（``".."`` 因此不会成为文件名）；
    - Windows 保留设备名（``CON``/``NUL``/``COM1``…）与空标题 → ``document``；
    - 先按字符数、再按 UTF-8 字节数限长。
    """
    raw = str(title or "")
    raw = unicodedata.normalize("NFKC", raw)
    # 路径成分一律丢掉：Windows 反斜杠也当分隔符。
    raw = raw.replace("\\", "/").rsplit("/", 1)[-1]
    raw = _UNSAFE_FILENAME_CHARS.sub("_", raw)
    # 剩下的控制/格式/未分配字符（Cc/Cf/Cs/Co/Cn）删掉而不是换成 "_"，
    # 否则一个纯零宽标题会变成一堆下划线。
    raw = "".join(char for char in raw if unicodedata.category(char)[0] != "C")
    raw = _DOT_RUNS.sub("_", raw)
    raw = raw.strip(" ._")
    raw = raw[:max_length].strip(" ._")
    stem = _truncate_bytes(raw, max_bytes).strip(" ._")
    # Windows 的保留设备名**带扩展名也照样保留**（``NUL.txt`` 同样不可用），
    # 所以按第一个点之前的部分判断。
    if not stem or stem.split(".")[0].upper() in _RESERVED_STEMS:
        return fallback
    return stem


def export_filename(title: str, fmt: str) -> str:
    """``<安全化后的标题>.<ext>``。格式未知时抛 :class:`UnsupportedExportFormat`。"""
    key = normalize_format(fmt)
    return f"{safe_filename_stem(title)}.{FORMAT_EXTENSIONS[key]}"


def content_disposition(filename: str, *, disposition: str = "attachment") -> str:
    """生成对中文文件名友好的 ``Content-Disposition`` 头。

    同时给 ``filename*``（RFC 5987 / UTF-8，中文原样）与 ``filename``
    （ASCII 兜底，给不认识 ``filename*`` 的老客户端）。ASCII 兜底里
    非 ASCII 字符一律换成 ``_``，**保证响应头本身是全 ASCII 的**
    （把中文直接塞进头里是 Latin-1 编码错误的高发点）。
    """
    from urllib.parse import quote

    raw = Path(str(filename))
    stem = safe_filename_stem(raw.stem)
    suffix = re.sub(r"[^A-Za-z0-9]", "", raw.suffix)[:10]
    name = f"{stem}.{suffix}" if suffix else stem
    ascii_name = name.encode("ascii", "replace").decode("ascii").replace("?", "_")
    return (
        f"{disposition}; filename=\"{ascii_name}\"; "
        f"filename*=UTF-8''{quote(name, safe='')}"
    )


# --------------------------------------------------------------------- HTML ----


def render_document_html(title: str, content_markdown: str) -> str:
    """Markdown → **完整** HTML 文档（UTF-8，中文与表格/列表可读）。

    渲染规则来自 :func:`apps.planner.markdown.render_markdown`（与界面预览同源），
    这里只负责套一层带 ``charset=utf-8`` 与基础排版样式的文档外壳。
    """
    from html import escape

    body = render_markdown(content_markdown or "")
    heading = escape(str(title or ""))
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{heading}</title>"
        "<style>"
        "body{font-family:system-ui,'PingFang SC','Microsoft YaHei',sans-serif;"
        "max-width:820px;margin:40px auto;padding:0 16px;line-height:1.7;color:#171717}"
        "h1,h2,h3,h4{line-height:1.35}"
        "table{border-collapse:collapse;width:100%}"
        "th,td{border:1px solid #e5e5e5;padding:6px 8px;text-align:left}"
        "code{background:#f6f6f6;border:1px solid #e5e5e5;border-radius:2px;padding:0 3px;"
        "font-family:ui-monospace,Consolas,monospace;font-size:.92em}"
        "pre{white-space:pre-wrap;word-break:break-word;background:#fafafa;"
        "border:1px solid #e5e5e5;border-radius:2px;padding:12px}"
        "blockquote{border-left:3px solid #e5e5e5;margin:8px 0;padding:4px 12px;color:#555}"
        "hr{border:none;border-top:1px solid #e5e5e5;margin:16px 0}"
        "a{color:#171717}"
        "</style></head>"
        f"<body><h1>{heading}</h1>\n{body}\n</body></html>\n"
    )


# --------------------------------------------------------------------- DOCX ----

#: 解析出来的块；``kind`` ∈ heading/paragraph/list/table/code/quote/rule。
@dataclass
class _Block:
    kind: str
    text: str = ""
    level: int = 0
    ordered: bool = False
    depth: int = 0
    items: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    #: 段落块的原始行（兜底：``text`` 是把多行用空格拼起来的，
    #: 但公文的"落款单位 / 成文日期"这类换行是有意义的，排版时要按行还原）。
    lines: list[str] = field(default_factory=list)


def _xml_text(text: str) -> str:
    """转义 XML 文本节点，并剔除 XML 1.0 不允许的字符。"""
    cleaned = _XML_INVALID.sub("", str(text))
    # 单独的代理对（来自被截断的字符串）会造成编码失败：用 replace 消化掉。
    cleaned = cleaned.encode("utf-8", "replace").decode("utf-8")
    return cleaned.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _xml_attr(text: str) -> str:
    return _xml_text(text).replace('"', "&quot;")


def parse_markdown_blocks(markdown: str) -> list[_Block]:
    """把 Markdown 拆成块级结构（表格/列表/段落/标题/代码/引用）。

    与 :mod:`apps.planner.markdown` 的判定口径一致，只是输出结构化数据而不是
    HTML 字符串 —— DOCX 需要真实的表格/列表部件，不能靠 ``<br>`` 糊。
    """
    blocks: list[_Block] = []
    lines = str(markdown or "").splitlines()
    paragraph: list[str] = []
    in_code = False
    code_lines: list[str] = []
    table_open = False

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(
                _Block(kind="paragraph", text=" ".join(paragraph), lines=list(paragraph))
            )
            paragraph.clear()

    def flush_code() -> None:
        if code_lines:
            blocks.append(_Block(kind="code", text="\n".join(code_lines)))
            code_lines.clear()

    for raw in lines:
        line = raw.rstrip()

        if _FENCE.match(line):
            flush_paragraph()
            table_open = False
            if in_code:
                flush_code()
                in_code = False
            else:
                in_code = True
            continue
        if in_code:
            code_lines.append(raw)
            continue

        table_line = bool(_TABLE_ROW.match(line))
        if not table_line:
            # 任何非表格行都表示"这张表结束了"（Markdown 表格不允许空行续行）。
            table_open = False

        if not line.strip():
            flush_paragraph()
            continue

        if _RULE.match(line):
            flush_paragraph()
            blocks.append(_Block(kind="rule"))
            continue

        heading = _HEADING.match(line)
        if heading:
            flush_paragraph()
            blocks.append(
                _Block(kind="heading", text=heading.group(2), level=len(heading.group(1)))
            )
            continue

        quote = _BLOCKQUOTE.match(line)
        if quote:
            flush_paragraph()
            blocks.append(_Block(kind="quote", text=quote.group(1)))
            continue

        if table_line:
            flush_paragraph()
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if all(_TABLE_DIVIDER.match(cell) for cell in cells):
                # 分隔行只负责把上一行标成表头：它本身不是数据。
                continue
            if table_open:
                blocks[-1].rows.append(cells)
            else:
                blocks.append(_Block(kind="table", rows=[cells]))
                table_open = True
            continue

        bullet = _BULLET.match(line)
        ordered = _ORDERED.match(line)
        if bullet or ordered:
            flush_paragraph()
            match = bullet or ordered
            indent = len(match.group(1).replace("\t", "  "))
            item = match.group(2)
            is_ordered = ordered is not None
            depth = min(indent // 2, 1)
            if (
                blocks
                and blocks[-1].kind == "list"
                and blocks[-1].ordered == is_ordered
                and blocks[-1].depth == depth
            ):
                blocks[-1].items.append(item)
            else:
                blocks.append(
                    _Block(
                        kind="list",
                        ordered=is_ordered,
                        depth=depth,
                        items=[item],
                    )
                )
            continue

        paragraph.append(line.strip())

    flush_paragraph()
    flush_code()
    return blocks


def _inline_runs(text: str, *, bold: bool = False, size: int | None = None) -> str:
    """行内 Markdown → 一串 ``w:r``。"""
    runs: list[str] = []
    position = 0
    for match in _INLINE_TOKEN.finditer(text):
        if match.start() > position:
            runs.append(
                _run(text[position : match.start()], bold=bold, size=size)
            )
        if match.group("bold") is not None:
            runs.append(_run(match.group("bold"), bold=True, size=size))
        else:
            runs.append(_run(match.group("code"), mono=True, size=size))
        position = match.end()
    if position < len(text):
        runs.append(_run(text[position:], bold=bold, size=size))
    if not runs and text:
        runs.append(_run(text, bold=bold, size=size))
    return "".join(runs)


def _run(
    text: str,
    *,
    bold: bool = False,
    mono: bool = False,
    size: int | None = None,
) -> str:
    """一个 ``w:r``；``w:t`` 恒带 ``xml:space="preserve"``。"""
    if text == "":
        return ""
    props: list[str] = []
    if mono:
        props.append(
            '<w:rFonts w:ascii="DejaVu Sans Mono" w:hAnsi="DejaVu Sans Mono"'
            ' w:cs="DejaVu Sans Mono"/>'
        )
    if bold:
        props.append("<w:b/><w:bCs/>")
    if size:
        props.append(f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>')
    rpr = f"<w:rPr>{''.join(props)}</w:rPr>" if props else ""
    return f'<w:r>{rpr}<w:t xml:space="preserve">{_xml_text(text)}</w:t></w:r>'


def _paragraph(
    runs: str,
    *,
    num_id: int | None = None,
    indent: int | None = None,
    spacing: str | None = None,
    border: str | None = None,
    shading: str | None = None,
    jc: str | None = None,
    outline: int | None = None,
) -> str:
    """一个 ``w:p``。子元素顺序严格按 CT_PPr 的 schema 排列。"""
    props: list[str] = []
    if num_id is not None:
        props.append(
            f'<w:numPr><w:ilvl w:val="0"/><w:numId w:val="{num_id}"/></w:numPr>'
        )
    if border:
        props.append(border)
    if shading:
        props.append(shading)
    if spacing:
        props.append(spacing)
    if indent is not None:
        props.append(f'<w:ind w:left="{indent}"/>')
    if jc:
        props.append(f'<w:jc w:val="{jc}"/>')
    if outline is not None:
        props.append(f'<w:outlineLvl w:val="{outline}"/>')
    ppr = f"<w:pPr>{''.join(props)}</w:pPr>" if props else ""
    return f"<w:p>{ppr}{runs}</w:p>"


#: 正文可用宽度（twips）。Word 默认页面 12240 twips 宽、左右各 1440 margins
#: → 可用 9360。表格必须显式占满这个宽度：早期写 `tblW=auto` 时渲染出来只有
#: 正文一半宽，"12 个月"这类内容会被挤成两行（独立审查的可见排版问题）。
TABLE_WIDTH_TWIPS = 9360
#: 单列最小宽度，避免"列多字少"时被压成一条缝
TABLE_MIN_COLUMN_TWIPS = 1100


def _column_widths(rows: list[list[str]], columns: int) -> list[int]:
    """按每列**最长单元格**的比例分配宽度（等宽会把窄列浪费掉）。

    为什么要按内容分：等宽时"条款/内容"两列各占一半，短列（条款名）空一大片、
    长列（内容）反而挤成两行。按最长单元格加权更接近 Word 的自动排版效果。
    """
    weights: list[int] = []
    for column in range(columns):
        longest = 0
        for row in rows:
            if column < len(row):
                # 中文按 2 个字符宽度计，粗略但足够决定比例
                text = str(row[column] or "")
                longest = max(longest, sum(2 if ord(ch) > 0x2E80 else 1 for ch in text))
        weights.append(max(longest, 4))
    total_weight = sum(weights) or 1
    widths = [max(TABLE_MIN_COLUMN_TWIPS, TABLE_WIDTH_TWIPS * w // total_weight) for w in weights]
    # 分配后的总和可能与可用宽度有偏差（取整 + 下限抬升）→ 差额补到最宽的一列
    delta = TABLE_WIDTH_TWIPS - sum(widths)
    if delta:
        widest = widths.index(max(widths))
        widths[widest] = max(TABLE_MIN_COLUMN_TWIPS, widths[widest] + delta)
    return widths


def _table_xml(rows: list[list[str]]) -> str:
    """``w:tbl``：占满正文宽度、首行表头加粗、单线边框、单元格正常换行。"""
    if not rows:
        return ""
    columns = max(len(row) for row in rows)
    widths = _column_widths(rows, columns)
    borders = (
        "<w:tblBorders>"
        + "".join(
            f'<w:{edge} w:val="single" w:sz="4" w:space="0" w:color="auto"/>'
            for edge in ("top", "left", "bottom", "right", "insideH", "insideV")
        )
        + "</w:tblBorders>"
    )
    grid = "".join(f'<w:gridCol w:w="{width}"/>' for width in widths)
    body: list[str] = []
    for index, row in enumerate(rows):
        cells: list[str] = []
        for column in range(columns):
            value = row[column] if column < len(row) else ""
            runs = _inline_runs(value, bold=index == 0)
            cells.append(
                f'<w:tc><w:tcPr><w:tcW w:w="{widths[column]}" w:type="dxa"/></w:tcPr>'
                f"{_paragraph(runs, spacing='<w:spacing w:after=\"0\"/>')}</w:tc>"
            )
        body.append(f"<w:tr>{''.join(cells)}</w:tr>")
    return (
        "<w:tbl><w:tblPr>"
        f'<w:tblW w:w="{TABLE_WIDTH_TWIPS}" w:type="dxa"/>'
        # fixed：让 Word/LibreOffice 严格按 gridCol 宽度排版（auto 会重新按内容算，
        # 于是又回到"表格只占半幅"的现象）
        '<w:tblLayout w:type="fixed"/>'
        f"{borders}"
        "</w:tblPr>"
        f"<w:tblGrid>{grid}</w:tblGrid>"
        f"{''.join(body)}</w:tbl>"
    )


def _body_xml(title: str, markdown: str) -> str:
    parts: list[str] = []
    title_text = str(title or "").strip()
    if title_text:
        parts.append(
            _paragraph(
                _run(title_text, bold=True, size=_TITLE_SIZE),
                spacing='<w:spacing w:before="0" w:after="240"/>',
                outline=0,
            )
        )
    # **正文里与标题同名的第一个 H1 要去重**：Markdown 常常把标题又写成 `# 标题`，
    # 于是文档一开头把同一个标题印两遍（独立审查在渲染 PDF 时看到的可见问题）。
    # 只去重**紧跟在标题后的第一个同名 H1**，不动其它任何标题。
    deduped_title = False
    for block in parse_markdown_blocks(markdown):
        if (
            not deduped_title
            and title_text
            and block.kind == "heading"
            and int(getattr(block, "level", 0) or 0) == 1
            and str(block.text or "").strip() == title_text
        ):
            deduped_title = True
            continue
        if block.kind == "heading":
            level = min(max(block.level, 1), 4)
            parts.append(
                _paragraph(
                    _inline_runs(block.text, bold=True, size=_HEADING_SIZES[level]),
                    spacing='<w:spacing w:before="240" w:after="120"/>',
                    outline=_HEADING_OUTLINE[level],
                )
            )
        elif block.kind == "paragraph":
            parts.append(_paragraph(_inline_runs(block.text)))
        elif block.kind == "list":
            num_id = 2 if block.ordered else 1
            for item in block.items:
                parts.append(
                    _paragraph(
                        _inline_runs(item),
                        num_id=num_id,
                        spacing='<w:spacing w:after="0"/>',
                    )
                )
        elif block.kind == "table":
            parts.append(_table_xml(block.rows))
            parts.append(_paragraph(""))
        elif block.kind == "code":
            for line in block.text.split("\n"):
                parts.append(
                    _paragraph(
                        _run(line, mono=True, size=20),
                        shading='<w:shd w:val="clear" w:color="auto" w:fill="F5F5F5"/>',
                        spacing='<w:spacing w:after="0"/>',
                        indent=240,
                    )
                )
        elif block.kind == "quote":
            parts.append(
                _paragraph(
                    _inline_runs(block.text),
                    border=(
                        "<w:pBdr>"
                        '<w:left w:val="single" w:sz="18" w:space="8" w:color="CCCCCC"/>'
                        "</w:pBdr>"
                    ),
                    indent=360,
                    spacing='<w:spacing w:before="120" w:after="120"/>',
                )
            )
        elif block.kind == "rule":
            parts.append(
                _paragraph(
                    "",
                    border=(
                        "<w:pBdr>"
                        '<w:bottom w:val="single" w:sz="6" w:space="1" w:color="CCCCCC"/>'
                        "</w:pBdr>"
                    ),
                    spacing='<w:spacing w:before="120" w:after="120"/>',
                )
            )
    # 正文至少要有一个段落，否则 Word/LibreOffice 会抱怨空的 body。
    if not parts:
        parts.append(_paragraph(""))
    sect = (
        "<w:sectPr>"
        '<w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"'
        ' w:header="720" w:footer="720" w:gutter="0"/>'
        "</w:sectPr>"
    )
    return f"{''.join(parts)}{sect}"


def _document_xml(title: str, markdown: str) -> str:
    return (
        _XML_DECL
        + f'<w:document xmlns:w="{_W_NS}" xmlns:r="{_R_NS}"><w:body>'
        + _body_xml(title, markdown)
        + "</w:body></w:document>"
    )


def _content_types_xml() -> str:
    docx_main = (
        "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document.main+xml"
    )
    return (
        _XML_DECL
        + f'<Types xmlns="{_CT_NS}">'
        '<Default Extension="rels"'
        ' ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f'<Override PartName="/word/document.xml" ContentType="{docx_main}"/>'
        '<Override PartName="/word/styles.xml"'
        ' ContentType="application/vnd.openxmlformats-officedocument.'
        'wordprocessingml.styles+xml"/>'
        '<Override PartName="/word/numbering.xml"'
        ' ContentType="application/vnd.openxmlformats-officedocument.'
        'wordprocessingml.numbering+xml"/>'
        '<Override PartName="/docProps/core.xml"'
        ' ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        "</Types>"
    )


def _root_rels_xml() -> str:
    base = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    return (
        _XML_DECL
        + f'<Relationships xmlns="{_REL_NS}">'
        f'<Relationship Id="rId1" Type="{base}/officeDocument"'
        ' Target="word/document.xml"/>'
        '<Relationship Id="rId2"'
        ' Type="http://schemas.openxmlformats.org/package/2006/relationships/'
        'metadata/core-properties" Target="docProps/core.xml"/>'
        "</Relationships>"
    )


def _document_rels_xml() -> str:
    base = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    return (
        _XML_DECL
        + f'<Relationships xmlns="{_REL_NS}">'
        f'<Relationship Id="rId1" Type="{base}/styles" Target="styles.xml"/>'
        f'<Relationship Id="rId2" Type="{base}/numbering" Target="numbering.xml"/>'
        "</Relationships>"
    )


def _styles_xml() -> str:
    """默认字号/行距 + ``Normal``。

    **刻意不指定字体名**：容器里若没有 ``SimSun``/``Calibri`` 这类字体，
    指定了反而会让 LibreOffice 做一次无意义的替换；中文能不能渲染取决于
    镜像里装没装 CJK 字体（见交付报告里的 ``fonts-noto-cjk``）。
    """
    return (
        _XML_DECL
        + f'<w:styles xmlns:w="{_W_NS}">'
        "<w:docDefaults>"
        '<w:rPrDefault><w:rPr><w:sz w:val="22"/><w:szCs w:val="22"/></w:rPr>'
        "</w:rPrDefault>"
        '<w:pPrDefault><w:pPr>'
        '<w:spacing w:after="120" w:line="276" w:lineRule="auto"/>'
        "</w:pPr></w:pPrDefault>"
        "</w:docDefaults>"
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
        '<w:name w:val="Normal"/><w:qFormat/></w:style>'
        "</w:styles>"
    )


def _abstract_num_xml(abstract_id: int, num_format: str, level_text: str) -> str:
    """一个抽象编号（两级足够覆盖本仓库的 Markdown 缩进）。"""
    levels: list[str] = []
    for ilvl in (0, 1):
        text = level_text if num_format == "bullet" else f"%{ilvl + 1}."
        levels.append(
            f'<w:lvl w:ilvl="{ilvl}"><w:start w:val="1"/>'
            f'<w:numFmt w:val="{num_format}"/>'
            f'<w:lvlText w:val="{_xml_attr(text)}"/>'
            '<w:lvlJc w:val="left"/>'
            f'<w:pPr><w:ind w:left="{720 * (ilvl + 1)}" w:hanging="360"/></w:pPr>'
            "</w:lvl>"
        )
    return (
        f'<w:abstractNum w:abstractNumId="{abstract_id}">'
        '<w:multiLevelType w:val="hybridMultilevel"/>'
        + "".join(levels)
        + "</w:abstractNum>"
    )


def _numbering_xml() -> str:
    """numId 1 = 无序（项目符号），numId 2 = 有序（1. 2. 3.）。"""
    return (
        _XML_DECL
        + f'<w:numbering xmlns:w="{_W_NS}">'
        + _abstract_num_xml(0, "bullet", "\u2022")
        + _abstract_num_xml(1, "decimal", "%1.")
        + '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
        + '<w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>'
        + "</w:numbering>"
    )


def _core_xml(title: str, *, created: datetime | None = None) -> str:
    stamp = (created or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return (
        _XML_DECL
        + f'<cp:coreProperties xmlns:cp="{_CP_NS}" xmlns:dc="{_DC_NS}"'
        f' xmlns:dcterms="{_DCTERMS_NS}"'
        ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>{_xml_text(title)}</dc:title>"
        "<dc:creator>KnowledgeBase</dc:creator>"
        "<cp:lastModifiedBy>KnowledgeBase</cp:lastModifiedBy>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{stamp}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{stamp}</dcterms:modified>'
        "</cp:coreProperties>"
    )


def _zip_parts(parts: dict[str, bytes]) -> bytes:
    """把部件打成 zip（``[Content_Types].xml`` 第一个，时间戳固定便于比对）。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, data)
    return buffer.getvalue()


def markdown_to_docx(
    title: str, content_markdown: str, *, created: datetime | None = None
) -> bytes:
    """Markdown → **合法 OOXML** DOCX 字节（不依赖 ``python-docx``）。

    中文以 UTF-8 字面量直接写进 ``word/document.xml``；表格/列表/段落/标题
    使用对应的 OOXML 部件（``w:tbl`` / ``w:numPr`` / ``w:p`` / ``w:outlineLvl``）。
    """
    document = _document_xml(title, content_markdown)
    parts = {
        "[Content_Types].xml": _content_types_xml().encode("utf-8"),
        "_rels/.rels": _root_rels_xml().encode("utf-8"),
        "docProps/core.xml": _core_xml(title, created=created).encode("utf-8"),
        "word/document.xml": document.encode("utf-8"),
        "word/styles.xml": _styles_xml().encode("utf-8"),
        "word/numbering.xml": _numbering_xml().encode("utf-8"),
        "word/_rels/document.xml.rels": _document_rels_xml().encode("utf-8"),
    }
    return _zip_parts(parts)


# ---------------------------------------------------------------------- PDF ----


def find_soffice() -> str | None:
    """定位 LibreOffice（复用 ``apps.knowledge.office_convert`` 的探测）。"""
    from apps.knowledge import office_convert

    return office_convert.find_soffice()


def soffice_available() -> bool:
    return find_soffice() is not None


def docx_to_pdf(docx_bytes: bytes, *, timeout: float | None = None) -> bytes:
    """DOCX 字节 → PDF 字节（由镜像内 LibreOffice 完成）。

    加固完全沿用 :mod:`apps.knowledge.office_convert`：

    * 每次转换一个**隔离的 user profile**（宏/外链/无脚本按 schema 显式关掉，
      并在启动前校验真的落盘）；
    * 超时杀**整个进程组**（``soffice`` 会 fork ``soffice.bin``）；
    * 产物做符号链接/条数/体积校验。

    没有 ``soffice`` → :class:`ExportUnavailable`（**明确报错，不返回假 PDF**）；
    转换失败/没产物/产物不是 PDF → 同样是 :class:`ExportUnavailable`。
    """
    from apps.knowledge import office_convert

    if not docx_bytes:
        raise ExportUnavailable("DOCX 内容为空，无法转换为 PDF")

    binary = find_soffice()
    if not binary:
        raise ExportUnavailable(
            "导出 PDF 需要 LibreOffice（soffice），但当前环境找不到该可执行文件；"
            "容器镜像请安装 libreoffice-writer（或完整 libreoffice），"
            "并确保容器内安装了中文字体（如 fonts-noto-cjk），否则中文会渲染成方框"
        )

    limit = office_convert.conversion_timeout_seconds() if timeout is None else timeout

    with tempfile.TemporaryDirectory(prefix="kb-export-pdf-") as tmp:
        work = Path(tmp)
        profile_dir = work / "profile"
        profile_dir.mkdir()
        # **必须**在拉起 soffice 之前写好并生效（run_conversion 会再校验一次）。
        office_convert.write_profile_config(profile_dir)

        source_dir = work / "src"
        source_dir.mkdir()
        source = source_dir / "document.docx"
        source.write_bytes(docx_bytes)

        output_dir = work / "out"
        output_dir.mkdir()

        try:
            outcome = office_convert.run_conversion(
                binary,
                source=source,
                output_dir=output_dir,
                profile_dir=profile_dir,
                target="pdf",
                timeout=limit,
            )
        except office_convert.ParseError as exc:
            raise ExportUnavailable(f"LibreOffice 转换 PDF 失败：{exc}") from exc

        if not outcome.ok:
            detail = f"（返回码 {outcome.returncode}）"
            if outcome.stderr:
                detail += f"；stderr：{outcome.stderr}"
            raise ExportUnavailable(f"LibreOffice 转换 PDF 未成功{detail}")

        pdfs = [
            path for path in outcome.files if path.suffix.lower() == ".pdf"
        ]
        if not pdfs:
            raise ExportUnavailable("LibreOffice 没有产出任何 .pdf 文件")
        data = pdfs[0].read_bytes()

    if not data.startswith(b"%PDF-"):
        # 产物不以 %PDF- 开头 = 不是 PDF（例如 LO 吐了一个错误页）。
        raise ExportUnavailable("LibreOffice 产物不是合法 PDF（缺少 %PDF- 文件头）")
    return data


# ------------------------------------------------------------------- 主入口 ----


#: 公文序号形态 → Word 标题级别。
#: 公文正文的分条不带 Markdown 的 ``##``，靠「一、」「（一）」标记层级；
#: 只按"短行 + 不以句号/逗号结尾"判成标题，避免把"一、去程票据为……。"这种
#: 分条正文也刷成黑体标题。
#: 附件清单的行（"2.陵水-海口东.pdf"）长得像"1."编号标题，但它是文件名，不是标题。
_FILE_LINE_RE = re.compile(
    r"\.(pdf|docx?|xlsx?|pptx?|txt|md|csv|jpe?g|png)$", re.IGNORECASE
)

_OFFICIAL_HEADING_PATTERNS = (
    (re.compile(r"^[一二三四五六七八九十百]+、"), "Heading 1"),
    (re.compile(r"^（[一二三四五六七八九十百]+）"), "Heading 2"),
    (re.compile(r"^\d+[．.]\s*\S"), "Heading 3"),
)


def _official_line_style(line: str) -> str | None:
    text = (line or "").strip()
    if not text or len(text) > 40 or text.endswith(("。", "；", "，", "！", "？")):
        return None
    if _FILE_LINE_RE.search(text):
        return None
    for pattern, style in _OFFICIAL_HEADING_PATTERNS:
        if pattern.match(text):
            return style
    return None


def _set_east_asian_font(run, name: str) -> None:
    """给一个 run 指定中文字体（``w:eastAsia``）。

    只设 ``run.font.name`` 只改西文（``w:ascii``），中文仍走模板默认字体 ——
    公文标题要的"方正小标宋"是中文字体，必须写 eastAsia 才生效。
    """
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(f"{{{_W_NS}}}rFonts")
    if rfonts is None:
        rfonts = rpr.makeelement(f"{{{_W_NS}}}rFonts", {})
        rpr.append(rfonts)
    rfonts.set(f"{{{_W_NS}}}eastAsia", name)


#: 成文日期的常见写法：`2026年10月19日`（也容忍 `2026-10-19` 这种）。
_SIGNATURE_DATE_RE = re.compile(r"^\s*(?:\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日|\d{4}-\d{1,2}-\d{1,2})\s*$")


def _align_official_signature(document) -> None:
    """把公文落款（单位名 + 成文日期）居右、去掉首行缩进。

    只在公文模板下调用。识别口径刻意保守：**成文日期行**是硬标志，它上面紧邻的
    非空段落视为落款单位；联系人行（以「（」开头或含「联系人」）保持原样 ——
    公文的联系方式通常跟正文一起左对齐，不是落款。
    """
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    paras = list(document.paragraphs)
    for index, para in enumerate(paras):
        text = (para.text or "").strip()
        if not _SIGNATURE_DATE_RE.match(text):
            continue
        para.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        para.paragraph_format.first_line_indent = Pt(0)
        # 往上找最近的非空段落作为落款单位；只接受"像单位名"的那种（短、不以（开头）
        for prev_index in range(index - 1, -1, -1):
            prev = paras[prev_index]
            prev_text = (prev.text or "").strip()
            if not prev_text:
                continue
            looks_like_contact = prev_text.startswith("（") or "联系人" in prev_text
            if not looks_like_contact and len(prev_text) <= 60:
                prev.alignment = WD_ALIGN_PARAGRAPH.RIGHT
                prev.paragraph_format.first_line_indent = Pt(0)
            break


def document_to_docx(document) -> bytes:
    """Word范本保留页面设置、页眉页脚和样式，只替换文档正文。

    实测缺陷（2026-10-19，公文模板）：直接套 ``Title``/``Heading N`` 会走样 ——
    用户上传的公文模板通常**没有** ``Title`` 样式（于是标题退化成正文样式、不居中），
    而模型写的一级标题是 Markdown ``##``（会映射成 ``Heading 2`` = 模板里的"二级标题"）。
    这里按公文口径修三处：标题用模板指定的字体字号居中、Markdown 标题整体上提一级、
    落款单位与成文日期居右且不首行缩进。
    """
    template = getattr(document, "template", None)
    if not template or template.suffix not in {".docx", ".dotx"}:
        return markdown_to_docx(document.title, document.content_markdown)
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt
    from .document_templates import template_path

    try:
        path = template_path(template)
        if not path.is_file():
            raise ExportUnavailable("所选Word模板原件不存在，请重新上传模板后生成。")
        source = path.read_bytes()
        if template.suffix == ".dotx":
            converted = io.BytesIO()
            with zipfile.ZipFile(io.BytesIO(source)) as package, zipfile.ZipFile(converted, "w") as out:
                for item in package.infolist():
                    data = package.read(item.filename)
                    if item.filename == "[Content_Types].xml":
                        data = data.replace(b"wordprocessingml.template.main+xml", b"wordprocessingml.document.main+xml")
                    out.writestr(item, data)
            source = converted.getvalue()
        output = Document(io.BytesIO(source))
        official = getattr(template, "category", "") == "official"
        if official:
            from .official_layout import OfficialLayout
            layout = OfficialLayout(output)
        body = output._element.body
        for child in list(body):
            if child.tag != f"{{{_W_NS}}}sectPr":
                body.remove(child)
        def paragraph(text, style="Normal", *, align=None, no_indent=False, role=None):
            style = style if style in output.styles else "Normal"
            para = output.add_paragraph(re.sub(r"\*\*|__|`", "", text), style=style)
            if official:
                layout.apply(para, role or style)
            if align is not None:
                para.alignment = align
            if no_indent:
                para.paragraph_format.first_line_indent = Pt(0)
            return para

        paragraph(document.title, "Title", align=WD_ALIGN_PARAGRAPH.CENTER, no_indent=True, role="title")
        if official and layout.title_gap:
            for _ in range(layout.title_gap):
                paragraph("", no_indent=True)
        body_started = False
        for block in parse_markdown_blocks(document.content_markdown):
            if not body_started and block.kind in {"heading", "paragraph"}:
                lines = list(block.lines or [block.text])
                if lines and re.sub(r"\*\*|__|`", "", lines[0]).strip() == document.title.strip():
                    lines.pop(0)
                    if not lines:
                        continue
                    block.lines = lines
                    block.text = "\n".join(lines)
            body_started = True
            if block.kind == "heading":
                if block.level == 1 and block.text == document.title:
                    continue
                # Markdown 的 `##` 是"一级标题"（对应公文的「一、」）：
                # 模板里的 Heading 1 才是黑体三号，Heading 2 是楷体（二级）。
                heading_level = min(max(block.level - 1, 1), 3) if official else min(block.level, 4)
                paragraph(block.text, f"Heading {heading_level}")
            elif block.kind == "list":
                for item in block.items:
                    if official:
                        # 公文不用项目符号：降级成正文段落，避免"要点体"冒充公文。
                        paragraph(item)
                    else:
                        paragraph(item, "List Number" if block.ordered else "List Bullet")
            elif block.kind == "table" and block.rows:
                table = output.add_table(rows=0, cols=max(len(row) for row in block.rows))
                for row in block.rows:
                    cells = table.add_row().cells
                    for cell, text in zip(cells, row):
                        cell.text = text
            elif block.text:
                if official:
                    # 公文的换行有意义（落款单位与成文日期必须各占一段）；
                    # 段落块里若含多行，就按行还原成独立段落，并给「一、」这类
                    # 分条套上标题样式（否则整篇会退化成一堆仿宋正文）。
                    for line in block.lines or [block.text]:
                        style = _official_line_style(line)
                        paragraph(line, style or "Normal")
                else:
                    paragraph(block.text)
        if official:
            layout.signatures()
            layout.clean_footer_instructions()
        buffer = io.BytesIO()
        output.save(buffer)
        if official:
            from .official_layout import preserve_package_parts
            return preserve_package_parts(source, buffer.getvalue(), layout.modified_parts)
        return buffer.getvalue()
    except (ExportError, ExportUnavailable):
        raise
    except Exception as exc:
        raise ExportError(f"Word模板导出失败：{exc}") from exc


def document_to_pptx(document) -> bytes:
    """按二级标题分页，复用所选 PPTX/POTX 的主题、母版和版式。"""
    from copy import deepcopy
    from pptx import Presentation
    from pptx.util import Inches, Pt

    template = getattr(document, "template", None)
    try:
        if template and template.suffix in {".pptx", ".potx"}:
            from .document_templates import template_path
            path = template_path(template)
            if not path.is_file():
                raise ExportUnavailable("所选PPT模板原件不存在，请重新上传模板后生成。")
            source = path.read_bytes()
            if template.suffix == ".potx":
                converted = io.BytesIO()
                with zipfile.ZipFile(io.BytesIO(source)) as package, zipfile.ZipFile(converted, "w") as out:
                    for item in package.infolist():
                        data = package.read(item.filename)
                        if item.filename == "[Content_Types].xml":
                            data = data.replace(b"presentationml.template.main+xml", b"presentationml.presentation.main+xml")
                        out.writestr(item, data)
                source = converted.getvalue()
            prs = Presentation(io.BytesIO(source))
            layouts = [slide.slide_layout for slide in prs.slides]
            backgrounds = []
            for original in prs.slides:
                bg = deepcopy(original._element.cSld.bg)
                relations = {}
                if bg is not None:
                    for element in bg.iter():
                        for attribute, value in element.attrib.items():
                            if attribute.startswith(f"{{{_R_NS}}}"):
                                relation = original.part.rels[value]
                                relations[value] = (relation.target_part, relation.reltype)
                backgrounds.append((bg, relations))
            for slide_id in list(prs.slides._sldIdLst):
                prs.part.drop_rel(slide_id.rId)
                prs.slides._sldIdLst.remove(slide_id)
        else:
            prs = Presentation()
            layouts, backgrounds = [], []
        sections = []
        for block in parse_markdown_blocks(document.content_markdown):
            if block.kind == "heading" and block.level <= 2:
                sections.append([block.text, []])
            else:
                if not sections:
                    sections.append([document.title, []])
                if block.kind == "list":
                    sections[-1][1].extend(block.items)
                elif block.kind == "table":
                    sections[-1][1].extend(" | ".join(row) for row in block.rows)
                elif block.text:
                    sections[-1][1].append(block.text)
        if len(sections) > 1 and sections[0][0] == document.title and not sections[0][1]:
            sections.pop(0)
        for i, (title, lines) in enumerate(sections or [[document.title, []]]):
            layout = layouts[min(i, len(layouts) - 1)] if layouts else prs.slide_layouts[1]
            slide = prs.slides.add_slide(layout)
            if backgrounds:
                source_bg, relations = backgrounds[min(i, len(backgrounds) - 1)]
                if source_bg is not None:
                    bg = deepcopy(source_bg)
                    for element in bg.iter():
                        for attribute, value in list(element.attrib.items()):
                            if attribute.startswith(f"{{{_R_NS}}}") and value in relations:
                                element.set(attribute, slide.part.relate_to(*relations[value]))
                    slide._element.cSld.insert(0, bg)
            heading = slide.shapes.title
            if heading is None:
                heading = slide.shapes.add_textbox(Inches(.5), Inches(.25), prs.slide_width - Inches(1), Inches(1))
            heading.text = title
            bodies = [p for p in slide.placeholders if p != heading and p.has_text_frame
                      and p.placeholder_format.type in {2, 7, 6}]
            body = bodies[0] if bodies else slide.shapes.add_textbox(
                Inches(.6), Inches(1.5), prs.slide_width - Inches(1.2), prs.slide_height - Inches(2))
            body.text_frame.clear()
            for j, line in enumerate(lines):
                paragraph = body.text_frame.paragraphs[0] if j == 0 else body.text_frame.add_paragraph()
                paragraph.text = re.sub(r"\*\*|__|`", "", str(line))
                paragraph.font.size = Pt(20)
            body.text_frame.word_wrap = True
        buffer = io.BytesIO()
        prs.save(buffer)
        return buffer.getvalue()
    except (ExportError, ExportUnavailable):
        raise
    except Exception as exc:
        raise ExportError(f"PPT导出失败：{exc}") from exc


def build_export(document, fmt: str) -> ExportPayload:
    """把一份派生文档导出成字节 + 文件名 + MIME。

    ``document`` 只需要 ``title`` / ``content_markdown`` 属性（``pk`` 可选），
    因此单测可以直接用 ``SimpleNamespace`` 而不用建库。

    空文档**照样能导出**（只有标题、空正文）：这是本模块的口径，"空正文要不要
    拒绝"属于调用方的业务策略（``services.execute_operation`` 现在保留了自己的
    拒绝逻辑，两者不冲突）。

    :raises UnsupportedExportFormat: 未知格式（同时是 ``ValueError``）
    :raises ExportUnavailable: 导出 PDF 但没有 LibreOffice / 转换失败
    """
    key = normalize_format(fmt)
    title = str(getattr(document, "title", "") or "")
    markdown = str(getattr(document, "content_markdown", "") or "")
    from .document_formatting import format_docx, format_html
    options = getattr(document, "format_options", {}) or {}

    if key == "md":
        content = markdown.encode("utf-8")
        note = "Markdown 原文（UTF-8 字节，未做额外包装）"
    elif key == "html":
        content = format_html(render_document_html(title, markdown), options).encode("utf-8")
        note = "完整 HTML 文档（UTF-8；与界面预览同一套渲染规则）"
    elif key == "pptx":
        content = document_to_pptx(document)
        note = "按标题分页的演示文稿；PPTX/POTX模板复用主题、母版与版式"
    elif key == "docx":
        content = format_docx(document_to_docx(document), options)
        note = "OOXML（zip）字节；Word范本复用页面设置、页眉页脚和样式"
    else:
        content = docx_to_pdf(format_docx(document_to_docx(document), options))
        note = (
            "由镜像内 LibreOffice（soffice --headless --convert-to pdf）"
            "从本模块生成的 DOCX 转换而来"
        )

    return ExportPayload(
        content=content,
        filename=export_filename(title, key),
        content_type=CONTENT_TYPES[key],
        note=note,
        format=key,
    )
