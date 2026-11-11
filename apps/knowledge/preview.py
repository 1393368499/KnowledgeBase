"""原件预览：真实展示**原件内容**，不把索引分段冒充成文件预览。

背景（用户复现的缺陷）：
  三个入口（`kb-files.js openPreview` / `kb-search.js previewFile` /
  `kb-pages.js openTaskPreview`）都把 `chunks.text` 放进 `<p>` —— 那不是预览，
  而是把索引文字挤成一段：PDF 的**原页/表格/水印位置全丢**，水印文字还会混进正文。

本模块的**边界**（硬约束）：
  * **只读**：绝不修改原件字节，绝不改写原文件；
  * **本地渲染**：PDF 用已有的 `pypdfium2` 在服务端按页出 PNG，
    不调用任何外部 Office/PDF 云、不引 CDN、**不把原件内容送去任何模型**；
  * **不 AI 重排**：PDF 一律按**原页**渲染，保留表格与水印的位置关系；
  * **路径安全**：原件路径必须是"本记录自己的内容寻址路径"，且**实际哈希校验**通过 ——
    不允许指向 raw 根内**别人的** blob；
  * **资源有界**：页号、缩放、像素、文本体量、压缩包展开量都有上限；
  * **诚实**：原件缺失/不支持/损坏时**明确说明**，只能显示"索引文字"时如实标注；
    路径不合法时**直接拒绝**，不做"静默换另一条路径"的掩盖。

PDFium 线程安全：见 `_PDFIUM_LOCK` 的说明（官方文档明确 PDFium 非线程安全）。
"""

from __future__ import annotations

import contextlib
import gc
import io
import math
import os
import re
import threading
from pathlib import Path
from typing import Any

from .parsers import MAX_ZIP_ENTRIES, MAX_ZIP_UNCOMPRESSED, guard_ooxml_size

#: 文本类预览的最大字节数（超出即截断，并如实标注 truncated）
MAX_TEXT_BYTES = 200_000
#: 结构化提取（docx/xlsx）的块数与单块长度上限
MAX_BLOCKS = 200
MAX_BLOCK_CHARS = 2_000
#: 表格渲染上限（行列）与 DOCX 表格数量上限
MAX_TABLE_ROWS = 100
MAX_TABLE_COLS = 20
MAX_DOCX_TABLES = 3
#: PDF 单页渲染的像素上限（宽×高），防止超大页面把内存打满
MAX_PAGE_PIXELS = 4_000_000
#: PDF 缩放上下限
MIN_SCALE, MAX_SCALE = 0.5, 3.0
#: 允许渲染的 PDF 文件大小上限（超过直接拒绝，避免无界消耗）
MAX_PDF_BYTES = 200 * 1024 * 1024
#: 允许做**完整哈希校验**的文件大小上限：更大的文件跳过逐字节校验，
#: 但仍然强制"路径 == 本记录的内容寻址路径"（因此不可能指向他人 blob），
#: 并在元数据里如实报告 `hash_verified=False`。
MAX_VERIFY_BYTES = 64 * 1024 * 1024

_HEX64 = re.compile(r"^[0-9a-f]{64}$")

_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".tsv", ".log", ".json", ".xml", ".yaml", ".yml"}

CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

#: **进程级** PDFium 互斥锁。
#:
#: 为什么必须是"覆盖整个生命周期"的单锁，而不是给 render 加锁：
#: PDFium **不是线程安全的**（官方文档《Incompatibility with threading》：
#: https://pypdfium2.readthedocs.io/en/stable/python_api.html#incompatibility-with-threading）。
#: 本服务的 web 是 **Gunicorn 每进程多线程**，多线程同时进 PDFium 会崩溃/错图/句柄错乱。
#: 因此 `<打开文档 → init_forms → len/getpage → render → close>` 整段必须由**同一个互斥**
#: 串行化 —— 只锁 render 是不够的。
#: 另：`init_forms()` 必须在 `len`/取页**之前**调用，交互式表单的内容才会被渲染。
_PDFIUM_LOCK = threading.RLock()


@contextlib.contextmanager
def _pdfium_document(path: Path):
    """在进程级互斥下打开 PDF，并在取页前初始化交互表单。

    **为什么把字节读进内存、而不是把路径交给 PDFium**：
    PDFium 会持有磁盘文件的句柄/映射；`close()` 之后在 Windows 上仍可能短暂占用该文件
    （实测：会话结束清理 raw 目录时报 `WinError 32`）。改为从内存字节构造文档后，
    PDFium **完全不碰磁盘文件**，句柄立即释放。读取量本身有界（`MAX_PDF_BYTES`）。
    """
    import pypdfium2 as pdfium

    _guard_pdf_size(path)
    with open(path, "rb") as handle:
        data = handle.read(MAX_PDF_BYTES + 1)
    if len(data) > MAX_PDF_BYTES:
        raise ValueError(
            f"PDF 过大，超出在线预览上限（> {MAX_PDF_BYTES // 1024 // 1024} MiB）"
        )
    with _PDFIUM_LOCK:
        doc = pdfium.PdfDocument(data)
        try:
            try:
                doc.init_forms()
            except Exception:  # noqa: BLE001 - 无表单能力时不影响普通页面渲染
                pass
            yield doc
        finally:
            doc.close()
            # 原生库：显式回收，保证句柄/映射及时释放、不随请求累积
            del doc
            gc.collect()


def real_raw_root() -> Path:
    """raw 根的**真实路径**（解析符号链接后再比较，才能挡住链接逃逸）。"""
    from .storage import raw_root

    return Path(os.path.realpath(raw_root()))


def resolve_original(record) -> tuple[Path | None, str]:
    """把资料记录解析成**原件路径**；返回 `(路径 或 None, 拒绝/缺失原因)`。

    安全规则（每条都对应一种真实攻击/事故）：
      1. **内容指纹必须合法**（64 位十六进制）。没有指纹就无法做"这份原件属于这条记录"的绑定。
      2. 合法位置**只有一处**：`<raw 根>/<sha[:2]>/<sha>`（内容寻址）。
         记录里的 `raw_path` 若存在，必须**恰好等于**这条路径的相对形式 ——
         否则**直接拒绝**，**绝不静默 fallback 到内容寻址路径**
         （那会把"路径异常"掩盖成"看起来正常"，也可能读到 raw 根里**别人的** blob）。
      3. 解析 `realpath` 后必须仍在 raw 根内（挡住 `../` 与**符号链接**逃逸），且不是根本身。
      4. **实际哈希校验**：文件字节的 sha256 必须等于指纹（≤ `MAX_VERIFY_BYTES` 时执行；
         超过则跳过并在元数据里如实标注 `hash_verified=False`）。
    """
    root = real_raw_root()
    digest = (getattr(record, "content_sha256", "") or "").strip().lower()
    if not _HEX64.match(digest):
        return None, "内容指纹不合法（需要 64 位十六进制 sha256），拒绝解析原件"

    expected = Path(os.path.realpath(root / digest[:2] / digest))
    # **无论是否给了 raw_path，都要对 expected 做包含性校验**：
    # 内容寻址目标本身也可能是一个指向外部的符号链接（例如有人把
    # `<root>/ab/<sha>` 做成指向 C:\Windows\... 的链接）—— 只校验 declared
    # 会让这条路径绕过检查。
    try:
        inside_expected = expected != root and os.path.commonpath(
            [str(expected), str(root)]
        ) == str(root)
    except ValueError:
        inside_expected = False
    if not inside_expected:
        return None, "原件路径越界（拒绝）"

    declared_rel = (getattr(record, "raw_path", "") or "").strip().replace("\\", "/")
    if declared_rel:
        declared = Path(os.path.realpath(root / declared_rel))
        try:
            inside = declared != root and os.path.commonpath(
                [str(declared), str(root)]
            ) == str(root)
        except ValueError:
            inside = False
        if not inside:
            return None, "原件路径越界（拒绝）"
        if declared != expected:
            return None, "原件路径与内容指纹不一致（拒绝，避免指向他人原件）"

    if not expected.is_file():
        return None, "原件缺失或不可访问（可能未落盘、已隔离或路径不合法）"
    try:
        too_big = expected.stat().st_size > MAX_VERIFY_BYTES
    except OSError as exc:
        return None, f"原件不可读：{exc}"
    if not too_big:
        from .storage import sha256_of_file

        actual = sha256_of_file(expected)
        if actual != digest:
            return None, "原件内容与内容指纹不符（拒绝预览）"
    return expected, ""


def _kind(record) -> str:
    name = (getattr(record, "original_name", "") or "").lower()
    ext = os.path.splitext(name)[1]
    if ext == ".pdf":
        return "pdf"
    if ext in _IMAGE_EXT:
        return "image"
    if ext in _TEXT_EXT:
        return "text"
    if ext == ".docx":
        return "docx"
    if ext == ".xlsx":
        return "xlsx"
    return "other"


def content_type_for(record) -> str:
    ext = os.path.splitext((getattr(record, "original_name", "") or "").lower())[1]
    return CONTENT_TYPES.get(ext, "application/octet-stream")


def _guard_pdf_size(path: Path) -> None:
    """页数统计与渲染**之前**都必须先做大小检查（两条路径都要有界）。"""
    size = path.stat().st_size
    if size > MAX_PDF_BYTES:
        raise ValueError(
            f"PDF 过大，超出在线预览上限（> {MAX_PDF_BYTES // 1024 // 1024} MiB）"
        )


def pdf_page_count(path: Path) -> int:
    """PDF 页数（先做大小检查，全程持有 PDFium 互斥锁）。"""
    _guard_pdf_size(path)
    with _pdfium_document(path) as doc:
        return len(doc)


def render_pdf_page(path: Path, page_index: int, scale: float) -> bytes:
    """把 PDF 的**第 page_index 页（0 基）**渲染成 PNG 字节。

    * 大小超限 / 缩放非有限值 → `ValueError`；页号越界 → `IndexError`；
    * 缩放夹到 `[MIN_SCALE, MAX_SCALE]`，并按 `MAX_PAGE_PIXELS` 回落缩放；
    * 0 页文档、超大页面都被有界处理；
    * 关闭顺序：`image` → `bitmap` → `page` → `doc`（**图片对象先关**）；
    * 整个 PDFium 交互都在**进程级互斥**内完成。
    """
    _guard_pdf_size(path)
    if not isinstance(scale, (int, float)) or not math.isfinite(float(scale)):
        raise ValueError("scale 必须是有限数字")
    with _pdfium_document(path) as doc:
        total = len(doc)
        if total == 0:
            raise IndexError("该 PDF 没有任何页面")
        if page_index < 0 or page_index >= total:
            raise IndexError(f"页码越界：{page_index + 1}/{total}")
        page = doc[page_index]
        try:
            requested = max(MIN_SCALE, min(MAX_SCALE, float(scale)))
            width = float(page.get_width()) * requested
            height = float(page.get_height()) * requested
            pixels = width * height
            effective = requested
            if not math.isfinite(pixels) or pixels <= 0:
                effective = MIN_SCALE
            elif pixels > MAX_PAGE_PIXELS:
                effective = requested * ((MAX_PAGE_PIXELS / pixels) ** 0.5)
            bitmap = page.render(scale=effective)
            try:
                image = bitmap.to_pil()
                try:
                    buffer = io.BytesIO()
                    image.save(buffer, format="PNG", optimize=False)
                    return buffer.getvalue()
                finally:
                    # **图片对象必须先于位图关闭**
                    image.close()
            finally:
                bitmap.close()
        finally:
            page.close()


def _read_text_preview(path: Path) -> tuple[str, bool]:
    """文本预览：**保留换行**，超长截断并标注。

    只读 `MAX_TEXT_BYTES + 1` 字节（旧实现 `read_bytes()` 会把整个巨文件读进内存，
    声明"有界"却实际无界）。
    """
    with open(path, "rb") as handle:
        raw = handle.read(MAX_TEXT_BYTES + 1)
    truncated = len(raw) > MAX_TEXT_BYTES
    raw = raw[:MAX_TEXT_BYTES]
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding), truncated
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), truncated


def _guard_ooxml(path: Path) -> None:
    guard_ooxml_size(path, max_entries=MAX_ZIP_ENTRIES, max_bytes=MAX_ZIP_UNCOMPRESSED)


def _docx_blocks(path: Path) -> tuple[list[dict[str, Any]], bool]:
    """DOCX → **结构化**块（段落/表格），**按文档真实顺序**；返回 `(块, 是否截断)`。

    旧实现"先所有段落、再所有表格"会打乱原顺序（表格位置整体后移）。
    这里遍历 `document.element.body` 的子元素，遇到 `w:p` 出段落、`w:tbl` 出表格。

    截断不再"悄悄进行"：命中任一上限（块数 / 表格数 / 单块字数 / 行列）都会把
    `truncated` 置真，由界面**明确提示**用户"内容已截断"。
    """
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    _guard_ooxml(path)
    with open(path, "rb") as handle:
        # 用文件对象（而不是路径）：内容寻址的 blob **没有 .docx 扩展名**，
        # 某些库会按扩展名判型；文件对象可绕开这种误判。
        document = Document(io.BytesIO(handle.read()))
    blocks: list[dict[str, Any]] = []
    truncated = False
    tables = 0
    for child in document.element.body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            text = (Paragraph(child, document).text or "").strip()
            if text:
                if len(text) > MAX_BLOCK_CHARS:
                    truncated = True
                blocks.append({"type": "paragraph", "text": text[:MAX_BLOCK_CHARS]})
        elif tag == "tbl":
            if tables >= MAX_DOCX_TABLES:
                truncated = True          # 超出表格数量上限：明确标记，不静默丢弃
                continue
            tables += 1
            table = Table(child, document)
            rows = [
                [((cell.text or "").strip())[:MAX_BLOCK_CHARS]
                 for cell in row.cells[:MAX_TABLE_COLS]]
                for row in table.rows[:MAX_TABLE_ROWS]
            ]
            if len(table.rows) > MAX_TABLE_ROWS or len(table.columns) > MAX_TABLE_COLS:
                truncated = True
            blocks.append({"type": "table", "rows": rows})
        if len(blocks) >= MAX_BLOCKS:
            truncated = True
            break
    return blocks, truncated


def _trim_table(rows: list[list[str]]) -> list[list[str]]:
    """裁掉表格**尾部全空的行/列**，但**保留中间的空列与空行**。

    为什么必须裁：`iter_rows(max_col=MAX_TABLE_COLS)` 会把每行**补齐到 20 列**，
    于是只有 3 列的真实工作表在界面上也渲染成 20 列（17 列全空）——
    既难看又误导"这份表有 20 列"。裁的是**尾部**，中间的空列（往往是真实占位）
    保留原样，因此列的对齐关系不会被打乱。
    """
    if not rows:
        return rows
    width = max(len(row) for row in rows)
    grid = [list(row) + [""] * (width - len(row)) for row in rows]
    # 尾部全空行
    while grid and all(not str(cell).strip() for cell in grid[-1]):
        grid.pop()
    if not grid:
        return []
    # 尾部全空列
    width = max(len(row) for row in grid)
    while width > 0 and all(not str(row[width - 1]).strip() for row in grid):
        width -= 1
    return [row[:width] for row in grid]


def _xlsx_blocks(path: Path) -> tuple[list[dict[str, Any]], bool]:
    """XLSX → **结构化**块（按工作表返回前若干行）；返回 `(块, 是否截断)`。"""
    from openpyxl import load_workbook

    _guard_ooxml(path)
    with open(path, "rb") as handle:
        # 同上：必须用文件对象，否则 blob 无扩展名会被判为"不支持的格式"
        workbook = load_workbook(io.BytesIO(handle.read()), read_only=True, data_only=True)
    blocks: list[dict[str, Any]] = []
    truncated = False
    try:
        sheets = list(workbook.worksheets)
        if len(sheets) > 3:
            truncated = True
        for sheet in sheets[:3]:
            rows: list[list[str]] = []
            for row in sheet.iter_rows(
                max_row=MAX_TABLE_ROWS, max_col=MAX_TABLE_COLS, values_only=True
            ):
                rows.append([
                    "" if value is None else str(value)[:MAX_BLOCK_CHARS] for value in row
                ])
            # 工作表实际规模超过上限 → 明确标记截断（**裁列之后仍然要标记**，
            # 否则"因为上限被截"这件事会被裁没）
            if (sheet.max_row or 0) > MAX_TABLE_ROWS or (sheet.max_column or 0) > MAX_TABLE_COLS:
                truncated = True
            trimmed = _trim_table(rows)
            if trimmed:
                blocks.append({"type": "table", "title": sheet.title, "rows": trimmed})
            if len(blocks) >= MAX_BLOCKS:
                truncated = True
                break
    finally:
        workbook.close()
    return blocks, truncated


def build_preview(record, *, base_url: str = "") -> dict[str, Any]:
    """预览元数据（**不渲染整篇**：只给客户端渲染所需的有界数据与端点）。

    URL 由本函数**独立**算出（不再依赖调用方拼 base），避免出现
    `.../preview/download/` 这种实际未注册、点开就 404 的地址。
    """
    kind = _kind(record)
    path, reason = resolve_original(record)
    original_name = getattr(record, "original_name", "") or ""
    pk = record.pk
    payload: dict[str, Any] = {
        "file_id": pk,
        "original_name": original_name,
        "library_id": record.library_id,
        "library_code": getattr(getattr(record, "library", None), "code", "") or "",
        "size_bytes": getattr(record, "size_bytes", 0) or 0,
        "raw_size_bytes": getattr(record, "raw_size_bytes", 0) or 0,
        "kind": kind,
        "content_type": content_type_for(record),
        "original_present": path is not None,
        "index_text_only": path is None,
        "chunk_count": getattr(record, "chunk_count", 0) or 0,
        "note": "",
        "rendered_locally": True,
        "hash_verified": None,
        # 与 `urls.py` 注册的路径**逐一对应**（有测试直接断言这两个值）
        "download_url": f"/api/v1/knowledge/files/{pk}/download/",
        "page_url": f"/api/v1/knowledge/files/{pk}/preview/pages/{{page}}/",
    }
    if path is None:
        payload["note"] = reason
        return payload

    payload["note"] = "原件可用"
    try:
        payload["hash_verified"] = path.stat().st_size <= MAX_VERIFY_BYTES
    except OSError:
        payload["hash_verified"] = False

    if kind == "pdf":
        try:
            # 页数统计**之前**先做大小检查（与渲染同一条约束）
            payload["page_count"] = pdf_page_count(path)
        except Exception as exc:  # noqa: BLE001 - 坏 PDF/超大 PDF 都必须如实报告
            payload["kind"] = "other"
            payload["parse_error"] = f"PDF 无法解析：{type(exc).__name__}: {exc}"
            payload["index_text_only"] = True
            payload["note"] = "原件存在但无法按原页渲染（结构损坏或超出预览上限）"
            return payload
        if not payload["page_count"]:
            payload["parse_error"] = "该 PDF 没有任何页面"
            payload["index_text_only"] = True
            payload["note"] = "原件存在但 PDF 没有页面，无法按原页渲染"
            return payload
        payload["note"] = "按原页渲染（保留表格与水印位置），不做任何重排"
    elif kind == "text":
        try:
            text, truncated = _read_text_preview(path)
            payload["text"] = text
            payload["truncated"] = truncated
            payload["note"] = "原样文本（保留换行）；超长已截断并标注"
        except OSError as exc:
            payload["index_text_only"] = True
            payload["note"] = f"文本读取失败：{exc}"
    elif kind == "docx":
        try:
            blocks, cut = _docx_blocks(path)
            payload["blocks"] = blocks
            payload["blocks_truncated"] = cut
            payload["note"] = "结构化提取（段落/表格，按文档顺序），非原始版式"
        except Exception as exc:  # noqa: BLE001
            payload["index_text_only"] = True
            payload["note"] = f"DOCX 结构化提取失败：{type(exc).__name__}: {exc}"
    elif kind == "xlsx":
        try:
            blocks, cut = _xlsx_blocks(path)
            payload["blocks"] = blocks
            payload["blocks_truncated"] = cut
            payload["note"] = "结构化提取（单元格表），非原始版式"
        except Exception as exc:  # noqa: BLE001
            payload["index_text_only"] = True
            payload["note"] = f"XLSX 结构化提取失败：{type(exc).__name__}: {exc}"
    elif kind == "image":
        payload["note"] = "原图（不做任何处理）"
    else:
        payload["index_text_only"] = True
        payload["note"] = "该格式暂无原版渲染，可下载原件或查看索引文字"
    return payload
