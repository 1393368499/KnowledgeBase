"""文档解析：把一份原始文件变成"带页码的分段文本"。

支持格式（与 ``KB_ALLOWED_UPLOAD_EXT`` 对齐）：

| 扩展名 | 解析器 | 说明 |
|---|---|---|
| `.pdf` | ``_parse_pdf`` | 先按文本层抽；某页抽不出文本（扫描件）就渲染成图交给 AS711 OCR |
| `.docx` | ``_parse_docx`` | 段落 + 表格 |
| `.xlsx` / `.xlsm` | ``_parse_xlsx`` | 每个工作表逐行转成 ``列=值`` 文本 |
| `.doc` / `.xls` / `.ppt` / `.pptx` | ``office_convert.parse_legacy_office`` | 受限 LibreOffice 转换后复用上面的解析器 |
| `.txt` / `.md` | ``_parse_text`` | 按行分段，Markdown 去标记 |
| `.csv` | ``_parse_csv`` | 按行转文本 |
| 图片 | ``_parse_image`` | 直接交给 AS711 OCR |

设计原则：

1. **解析器只产出 ``ParsedBlock``**，不碰数据库。写库（``DocumentChunk``）由服务层负责，
   这样"解析失败 → 不写半成品"是天然成立的。
2. **不猜内容**。不支持的格式抛 ``ParseUnsupported``，不返回空字符串；
   "解析成功但 0 分段"会被服务层判为可疑并报错（见 ``apps.knowledge.pipeline``）。
3. **OCR 走 AS711**（需求 4：PaddleOCR-VL-1.5），本地不装 OCR 引擎；
   只有真的没有文本层时才调用，避免无谓地把 PDF 整本渲染成图片。
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

from .ocr import OcrClient, OcrError
from . import progress

#: 单块文本上限（字符）。太长的段落切开展示，但分段之间不留语义空洞。
MAX_BLOCK_CHARS = 1200

#: 一次解析最多产出的分段数，防止坏文件把库撑爆。
MAX_BLOCKS = 20000


class ParseError(RuntimeError):
    """解析失败（可重试；服务层会记录原因并保留重试机会）。"""


class ParseUnsupported(ParseError):
    """格式不支持（重试也不会变好，属于终态失败）。"""


@dataclass(frozen=True)
class ParsedBlock:
    """一段解析出的文本。"""

    text: str
    page: int | None = None
    kind: str = "text"
    order: int = 0


@dataclass
class ParseResult:
    blocks: list[ParsedBlock] = field(default_factory=list)
    #: 本次解析用到的能力（真实用了才写，便于事后核对）
    used_ocr: bool = False
    parser: str = ""

    @property
    def text_chars(self) -> int:
        return sum(len(block.text) for block in self.blocks)


# --------------------------------------------------------------------- 工具 ----


_WS = re.compile(r"[ \t\u00a0]+")


def normalize_text(raw: str) -> str:
    """统一空白与换行，去掉纯装饰行。"""
    if not raw:
        return ""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    for line in text.split("\n"):
        line = _WS.sub(" ", line).strip()
        lines.append(line)
    # 折叠连续空行
    out: list[str] = []
    blank = 0
    for line in lines:
        if line:
            blank = 0
            out.append(line)
        else:
            blank += 1
            if blank <= 1:
                out.append("")
    return "\n".join(out).strip()


def split_long(text: str, *, limit: int = MAX_BLOCK_CHARS) -> list[str]:
    """把过长文本按段落边界切开，切点优先落在换行/句号上。"""
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = max(window.rfind("\n"), window.rfind("。"), window.rfind("；"), window.rfind(". "))
        if cut < limit // 3:
            cut = limit
        parts.append(remaining[:cut].strip())
        remaining = remaining[cut:].lstrip()
    if remaining.strip():
        parts.append(remaining.strip())
    return [part for part in parts if part]


def _blocks_from_text(
    text: str, *, page: int | None, kind: str, start_order: int, limit: int = MAX_BLOCK_CHARS
) -> list[ParsedBlock]:
    return [
        ParsedBlock(text=piece, page=page, kind=kind, order=start_order + index)
        for index, piece in enumerate(split_long(text, limit=limit))
    ]


# ------------------------------------------------------------------ 各解析器 ----


def _parse_text(raw: bytes, name: str) -> ParseResult:
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            content = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ParseError("文本文件不是 UTF-8 / GB18030 编码，无法解析")
    if Path(name).suffix.lower() == ".md":
        content = strip_markdown(content)
    blocks = _blocks_from_text(normalize_text(content), page=None, kind="text", start_order=0)
    return ParseResult(blocks=blocks, parser="text")


def strip_markdown(content: str) -> str:
    """Markdown 去标记：保留文字与结构，不保留语法噪音。"""
    lines_out: list[str] = []
    in_code = False
    for line in content.replace("\r\n", "\n").split("\n"):
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            lines_out.append(line)
            continue
        line = re.sub(r"^\s{0,3}#{1,6}\s*", "", line)          # 标题
        line = re.sub(r"^\s{0,3}>\s?", "", line)               # 引用
        line = re.sub(r"^\s*[-*+]\s+", "", line)               # 无序列表
        line = re.sub(r"^\s*\d+[.)]\s+", "", line)             # 有序列表
        line = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", line)       # 图片
        line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)   # 链接保留文字
        line = line.replace("**", "").replace("__", "").replace("`", "")
        lines_out.append(line)
    return "\n".join(lines_out)


def _parse_csv(raw: bytes, name: str) -> ParseResult:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            content = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ParseError("CSV 不是 UTF-8 / GB18030 编码，无法解析")
    reader = csv.reader(io.StringIO(content))
    rows = [row for row in reader]
    if not rows:
        raise ParseError("CSV 没有内容")
    header = [cell.strip() for cell in rows[0]]
    lines: list[str] = []
    for index, row in enumerate(rows[1:], start=2):
        pairs = []
        for pos, cell in enumerate(row):
            key = header[pos] if pos < len(header) and header[pos] else f"列{pos + 1}"
            value = cell.strip()
            if value:
                pairs.append(f"{key}={value}")
        if pairs:
            lines.append(f"第{index}行 " + "，".join(pairs))
    blocks: list[ParsedBlock] = []
    if header:
        blocks.append(ParsedBlock(text="表头：" + "，".join(h for h in header if h), kind="csv-header", order=0))
    for group in _group(lines, 40):
        blocks.extend(
            _blocks_from_text(
                "\n".join(group), page=None, kind="csv", start_order=len(blocks)
            )
        )
    return ParseResult(blocks=blocks, parser="csv")


def _group(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _parse_docx(raw: bytes, name: str) -> ParseResult:
    import docx  # python-docx

    try:
        document = docx.Document(io.BytesIO(raw))
    except Exception as exc:  # noqa: BLE001 - 具体异常类型由库决定
        raise ParseError(f"DOCX 打开失败：{exc}") from exc

    blocks: list[ParsedBlock] = []
    order = 0
    for paragraph in document.paragraphs:
        text = normalize_text(paragraph.text)
        if not text:
            continue
        blocks.extend(_blocks_from_text(text, page=None, kind="paragraph", start_order=order))
        order = len(blocks)
    for table_index, table in enumerate(document.tables, start=1):
        rows: list[str] = []
        for row in table.rows:
            cells = [normalize_text(cell.text) for cell in row.cells]
            cells = [cell for cell in cells if cell]
            if cells:
                rows.append("，".join(cells))
        if rows:
            for group in _group(rows, 30):
                blocks.extend(
                    _blocks_from_text(
                        "\n".join(group),
                        page=None,
                        kind="table",
                        start_order=len(blocks),
                    )
                )
    if not blocks:
        raise ParseError("DOCX 里没有可提取的文字")
    return ParseResult(blocks=blocks, parser="docx")


def _parse_xlsx(raw: bytes, name: str) -> ParseResult:
    import openpyxl

    try:
        workbook = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        raise ParseError(f"XLSX 打开失败：{exc}") from exc

    blocks: list[ParsedBlock] = []
    try:
        for sheet in workbook.worksheets:
            rows: list[list[str]] = []
            for row in sheet.iter_rows(values_only=True):
                cells = ["" if cell is None else str(cell).strip() for cell in row]
                if any(cells):
                    rows.append(cells)
            if not rows:
                continue
            header = rows[0]
            lines: list[str] = []
            for row_index, row in enumerate(rows[1:], start=2):
                pairs = []
                for pos, cell in enumerate(row):
                    if not cell:
                        continue
                    key = header[pos] if pos < len(header) and header[pos] else f"列{pos + 1}"
                    pairs.append(f"{key}={cell}")
                if pairs:
                    lines.append(f"{sheet.title} 第{row_index}行 " + "，".join(pairs))
            blocks.append(
                ParsedBlock(
                    text=f"工作表：{sheet.title}（表头：" + "，".join(h for h in header if h) + "）",
                    kind="sheet-header",
                    order=len(blocks),
                )
            )
            for group in _group(lines, 40):
                blocks.extend(
                    _blocks_from_text(
                        "\n".join(group), page=None, kind="sheet", start_order=len(blocks)
                    )
                )
    finally:
        workbook.close()
    # 空工作簿：blocks 为空时绝不允许去索引 blocks[0]（那会 IndexError），
    # 也不能返回"解析成功但 0 分段"——那种状态下服务层无法判断对错。
    if not [block for block in blocks if block.text.strip()]:
        raise ParseError("XLSX 里没有可提取的单元格（空工作簿或所有单元格为空）")
    return ParseResult(blocks=blocks, parser="xlsx")


def _parse_pdf(raw: bytes, name: str, *, ocr: OcrClient | None, ocr_pages: int) -> ParseResult:
    """PDF：先抽文本层；抽不到的那几页（扫描件）渲染成图交给 AS711 OCR。"""
    import pypdf

    try:
        reader = pypdf.PdfReader(io.BytesIO(raw))
    except Exception as exc:  # noqa: BLE001
        raise ParseError(f"PDF 打开失败：{exc}") from exc

    blocks: list[ParsedBlock] = []
    blank_pages: list[int] = []
    used_ocr = False
    progress.report('extracting', '正在读取 PDF 文本', 0, len(reader.pages), '页')
    for page_index, page in enumerate(reader.pages, start=1):
        try:
            text = normalize_text(page.extract_text() or "")
        except Exception:  # noqa: BLE001 - 单页抽不出不算整本失败
            text = ""
        if text:
            blocks.extend(
                _blocks_from_text(text, page=page_index, kind="pdf-text", start_order=len(blocks))
            )
        else:
            blank_pages.append(page_index)
        progress.report('extracting', '正在读取 PDF 文本', page_index, len(reader.pages), '页')

    if blank_pages:
        if ocr is None or not ocr.available:
            # 没有 OCR 能力（未配置端点）时明确报错，不把扫描件当"空文件"糊过去。
            # 注意：configured 的客户端 available=True，允许发起首次真实调用，
            # 调用失败会在下面转成 ParseError —— 不能因为"还没成功过"就永不尝试。
            raise ParseError(
                f"PDF 第 {blank_pages[:5]} 页没有文本层（疑为扫描件），"
                "且未配置可用的 OCR 服务，无法解析"
            )
        used_ocr = True
        # 扫描页超过单次上限时**必须报错**，不能只处理前 N 页然后宣称整份成功——
        # 那等于静默丢页（审查意见 2）。宁可失败让人看见，也不给一份缺页的索引。
        if len(blank_pages) > ocr_pages:
            raise ParseError(
                f"PDF 有 {len(blank_pages)} 页需要 OCR，超过单份上限 {ocr_pages} 页；"
                "为避免静默丢页已中止解析（可提高 KB_OCR_MAX_PAGES 后重试）"
            )
        def images_by_page():
            # 每次只持有一页图片，长扫描件不会一次渲染数十页占满内存。
            for number in blank_pages:
                try:
                    yield from render_pdf_pages(raw, [number])
                except Exception as exc:
                    raise ParseError(f"第 {number} 页扫描页渲染失败：{exc}") from exc
        for completed, (page_number, png) in enumerate(images_by_page()):
            progress.report('ocr', '正在识别扫描页', completed, len(blank_pages), '页',
                            detail=f'正在识别原件第 {page_number} 页')
            try:
                text = ocr.recognize_image(png, hint=f"{Path(name).name} 第 {page_number} 页")
            except OcrError as exc:
                raise ParseError(f"第 {page_number} 页 OCR 失败：{exc}") from exc
            text = normalize_text(text)
            if text:
                blocks.extend(
                    _blocks_from_text(
                        text, page=page_number, kind="pdf-ocr", start_order=len(blocks)
                    )
                )
            progress.report('ocr', '正在识别扫描页', completed + 1, len(blank_pages), '页')
    if not blocks:
        raise ParseError("PDF 里没有可提取的文字")
    blocks.sort(key=lambda block: (block.page or 0, block.order))
    for index, block in enumerate(blocks):
        object.__setattr__(block, "order", index)
    return ParseResult(blocks=blocks, used_ocr=used_ocr, parser="pdf-ocr" if used_ocr else "pdf")


def render_pdf_pages(raw: bytes, pages: list[int], *, scale: float = 2.0) -> list[tuple[int, bytes]]:
    """把 PDF 指定页渲染成 PNG 字节（1-based 页号）。"""
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(io.BytesIO(raw))
    out: list[tuple[int, bytes]] = []
    try:
        for page_number in pages:
            if page_number < 1 or page_number > len(document):
                continue
            page = document[page_number - 1]
            bitmap = page.render(scale=scale)
            image = bitmap.to_pil().convert("RGB")
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            out.append((page_number, buffer.getvalue()))
            image.close()
            bitmap.close()
            page.close()
    finally:
        document.close()
    return out


def _parse_image(raw: bytes, name: str, *, ocr: OcrClient | None) -> ParseResult:
    if ocr is None or not ocr.available:
        raise ParseError("图片需要 OCR，但未配置可用的 OCR 服务端点")
    try:
        text = ocr.recognize_image(raw, hint=Path(name).name)
    except OcrError as exc:
        raise ParseError(f"图片 OCR 失败：{exc}") from exc
    text = normalize_text(text)
    if not text:
        raise ParseError("OCR 没有识别出文字")
    return ParseResult(
        blocks=_blocks_from_text(text, page=1, kind="image-ocr", start_order=0),
        used_ocr=True,
        parser="image-ocr",
    )


def _unsupported(name: str, hint: str) -> ParseResult:
    raise ParseUnsupported(f"{Path(name).suffix.lower() or '该格式'} {hint}")


# ------------------------------------------------------------------ 入口 ----


TEXT_SUFFIXES = {".txt", ".md", ".markdown"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}

#: 需要 LibreOffice 转换的旧版 / 二进制 Office 后缀（实现在 ``office_convert``）。
LEGACY_OFFICE_SUFFIXES = {".doc", ".xls", ".ppt", ".pptx"}

#: **宏 / 脚本型** 后缀：直接拒绝（与 ``office_convert.BLOCKED_SUFFIXES`` 一致，
#: 这里再挡一次是因为 ``.xlsm`` 原本会走 openpyxl 分支 —— 宏不该进任何解析器）。
BLOCKED_MACRO_SUFFIXES = {
    ".docm", ".dotm", ".dotx",
    ".xlsm", ".xltm", ".xlam", ".xla", ".xlt",
    ".pptm", ".potm", ".ppam", ".sldm",
    ".vbs", ".vba", ".js", ".jse", ".wsf", ".wsh", ".ps1", ".hta",
}


def parse_bytes(
    raw: bytes,
    name: str,
    *,
    ocr: OcrClient | None = None,
    ocr_pages: int = 200,
) -> ParseResult:
    """按扩展名分派解析器。识别只看扩展名与内容是否可读，不做过度猜测。"""
    suffix = Path(name).suffix.lower()
    # 宏 / 脚本型文档在**分派之前**就拒绝：它们不该走到任何解析器里，
    # 也不该被 LibreOffice 打开（那是一份可执行的文档，不是一份资料）。
    # 放在"空文件"检查**之前**：安全判定不能被别的错误抢先遮住。
    if suffix in BLOCKED_MACRO_SUFFIXES:
        raise ParseUnsupported(
            f"拒绝解析 {suffix}：该格式可携带宏/脚本，出于安全考虑不予解析"
            "（请另存为不含宏的 .docx / .xlsx / .pptx 后重新上传）"
        )
    if not raw:
        raise ParseError("文件为空，无法解析")
    if suffix in TEXT_SUFFIXES:
        return _parse_text(raw, name)
    if suffix == ".csv":
        return _parse_csv(raw, name)
    if suffix == ".docx":
        return _parse_docx(raw, name)
    if suffix in {".xlsx", ".xlsm"}:
        # .xlsm 到不了这里（上面已按宏格式拒绝），保留在集合里只为兼容旧调用点。
        return _parse_xlsx(raw, name)
    if suffix == ".pdf":
        return _parse_pdf(raw, name, ocr=ocr, ocr_pages=ocr_pages)
    if suffix in IMAGE_SUFFIXES:
        return _parse_image(raw, name, ocr=ocr)
    if suffix in LEGACY_OFFICE_SUFFIXES:
        # 旧版 / 需要转换的 Office：受限 LibreOffice 转换后再复用上面的解析器
        # （.doc→.docx|.txt、.xls→.xlsx|.csv、.ppt→.pptx|.pdf、.pptx→.pdf）。
        # 实现与全部安全守卫在 ``apps.knowledge.office_convert``。
        from .office_convert import parse_legacy_office

        return parse_legacy_office(raw, name, ocr=ocr, ocr_pages=ocr_pages)
    return _unsupported(name, "暂不支持解析")
