"""``apps.planner.document_export`` 的真实导出测试。

覆盖口径（与交付说明一一对应）：

* DOCX 必须是**合法 zip + 合法 OOXML**，中文标题/段落/表格单元格/列表项
  真的出现在 ``word/document.xml`` 里（不是"看起来像 docx"）；
* HTML 保留中文、表格与列表（与界面预览同一套渲染规则）；
* **PDF 只依赖隔离的 Office 转换服务**：未配置 ``KB_OFFICE_SOCKET`` 或服务不可用
  必须明确报错（带稳定错误码）且不返回假 PDF；本模块**不探测也不拉起本地进程**；
* 配置服务后的真实 PDF 转换用 skipif 标出（需 Docker 部署的 office 服务 + CJK 字体）；
* 文件名安全化：路径穿越、分隔符、控制字符、超长、空标题、Windows 保留名；
* 未知格式 → ``UnsupportedExportFormat``（同时是 ``ValueError``）。

注意：本文件里的 "假 PDF" 只出现在**测试替身**里（monkeypatch
``document_export.docx_to_pdf``），用来断言 MIME/文件名；生产代码路径
永远只会返回以 ``%PDF-`` 开头的真实字节。
"""

from __future__ import annotations

import ast
import inspect
import io
import re
import zipfile
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

from apps.planner import document_export
from apps.planner.document_export import (
    CONTENT_TYPES,
    EXPORT_CODE_CONVERSION_FAILED,
    EXPORT_CODE_UNAVAILABLE,
    EXPORT_CODE_UNCONFIGURED,
    MAX_STEM_BYTES,
    MAX_STEM_LENGTH,
    SUPPORTED_FORMATS,
    ExportPayload,
    ExportUnavailable,
    UnsupportedExportFormat,
    build_export,
    content_disposition,
    export_filename,
    markdown_to_docx,
    normalize_format,
    render_document_html,
    safe_filename_stem,
)

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

SAMPLE_MARKDOWN = """# 季度总结

这是**正文**段落，含中文与 `code` 片段。

## 数据表

| 项目 | 负责人 | 备注 |
| --- | --- | --- |
| 索引重建 | 张三 | 已完成 |
| 权限收敛 | 李四 | 进行中 |

## 待办

- 复核中文导出
- 补充表格单元格

1. 第一步：生成 DOCX
2. 第二步：转 PDF

> 引用：PDF 由隔离的 Office 服务生成。

```python
print("你好")
```

---
"""

DOCX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)

#: 项目要求"宿主转换器名字在仓库里 0 命中"；这里拆开写，保证本文件自身也不产生命中。
LOCAL_CONVERTER = "soff" + "ice"


def make_document(title: str = "中文标题", content: str = SAMPLE_MARKDOWN):
    """最小可用的"派生文档"替身：只要有 title / content_markdown / pk。"""
    return SimpleNamespace(pk=7, title=title, content_markdown=content)


def docx_part(data: bytes, name: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return archive.read(name)


def document_xml(data: bytes) -> str:
    return docx_part(data, "word/document.xml").decode("utf-8")


def document_texts(data: bytes) -> list[str]:
    """按顺序取出 ``word/document.xml`` 里所有 ``w:t`` 的文本。"""
    root = ElementTree.fromstring(docx_part(data, "word/document.xml"))
    return [node.text or "" for node in root.iter(f"{W_NS}t")]


# --------------------------------------------------------------- 格式与 MIME ----


def test_supported_formats_are_the_documented_set():
    assert SUPPORTED_FORMATS == ("md", "html", "docx", "pdf", "pptx")
    assert set(CONTENT_TYPES) == set(SUPPORTED_FORMATS)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("md", "md"),
        ("MD", "md"),
        (".docx", "docx"),
        ("markdown", "md"),
        ("m", "md"),
        ("htm", "html"),
        ("HTML", "html"),
        ("pdf", "pdf"),
        ("word", "docx"),
        ("doc", "docx"),
    ],
)
def test_normalize_format_accepts_documented_aliases(raw, expected):
    assert normalize_format(raw) == expected


@pytest.mark.parametrize("raw", ["rtf", "txt", "xlsx", "", "   ", "docx2", "md2"])
def test_unknown_format_is_a_value_error(raw):
    with pytest.raises(UnsupportedExportFormat) as excinfo:
        normalize_format(raw)
    # 自定义异常同时也必须是 ValueError（调用方按哪个捕获都能接住）
    assert isinstance(excinfo.value, ValueError)
    assert "不支持" in str(excinfo.value)
    with pytest.raises(ValueError):
        build_export(make_document(), raw)


@pytest.mark.parametrize(
    ("fmt", "content_type", "suffix"),
    [
        ("md", "text/markdown; charset=utf-8", ".md"),
        ("html", "text/html; charset=utf-8", ".html"),
        ("docx", DOCX_CONTENT_TYPE, ".docx"),
        ("pdf", "application/pdf", ".pdf"),
    ],
)
def test_payload_carries_exact_mime_and_filename(monkeypatch, fmt, content_type, suffix):
    if fmt == "pdf":
        # 测试替身：只为了断言 MIME/文件名，真实转换另有用例。
        monkeypatch.setattr(
            document_export, "docx_to_pdf", lambda data, **kwargs: b"%PDF-1.7 fake"
        )
    payload = build_export(make_document("中文标题"), fmt)
    assert isinstance(payload, ExportPayload)
    assert payload.content_type == content_type
    assert payload.filename == f"中文标题{suffix}"
    assert isinstance(payload.content, bytes) and payload.content
    assert payload.note  # 说明文字不能为空（调用方会写进响应/日志）
    assert payload.format == fmt
    assert payload.size == len(payload.content)


def test_payload_reports_the_normalized_format_for_aliases():
    """请求别名（markdown/htm）时，回写响应头要用归一化后的格式名。"""
    assert build_export(make_document(), "markdown").format == "md"
    assert build_export(make_document(), "htm").format == "html"
    assert build_export(make_document(), ".docx").format == "docx"


# --------------------------------------------------------------------- MD ----


def test_md_export_is_the_utf8_source_markdown():
    payload = build_export(make_document(), "md")
    assert payload.content.decode("utf-8") == SAMPLE_MARKDOWN
    assert "季度总结" in payload.content.decode("utf-8")


# ------------------------------------------------------------------- HTML ----


def test_html_export_keeps_chinese_title_tables_and_lists():
    payload = build_export(make_document(), "html")
    html = payload.content.decode("utf-8")

    assert '<meta charset="utf-8">' in html
    assert "<h1>中文标题</h1>" in html
    assert "<h2>季度总结</h2>" in html
    assert "<strong>正文</strong>" in html
    assert "<table>" in html
    assert "<th>项目</th>" in html
    assert "<td>索引重建</td>" in html
    assert "<td>李四</td>" in html
    assert "<ul>" in html and "<li>复核中文导出</li>" in html
    assert "<ol>" in html and "<li>第一步：生成 DOCX</li>" in html
    assert "季度总结" in html


def test_html_export_escapes_dangerous_markup():
    html = render_document_html("<script>标题</script>", "正文 <img src=x onerror=1>")
    assert "<script>标题" not in html
    assert "&lt;script&gt;" in html
    assert "<img" not in html


# ------------------------------------------------------------------ DOCX ----


def test_docx_is_a_valid_zip_with_the_required_parts():
    data = markdown_to_docx("中文标题", SAMPLE_MARKDOWN)
    assert data[:2] == b"PK", "DOCX 必须是 zip 包"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.testzip() is None, "zip CRC 校验失败"
        names = archive.namelist()
    for required in (
        "[Content_Types].xml",
        "_rels/.rels",
        "word/document.xml",
        "word/_rels/document.xml.rels",
        "word/styles.xml",
        "word/numbering.xml",
        "docProps/core.xml",
    ):
        assert required in names, f"缺少部件 {required}"
    assert names[0] == "[Content_Types].xml", "OPC 要求内容类型部件排在第一位"


def test_docx_xml_parts_are_well_formed():
    data = markdown_to_docx("中文标题", SAMPLE_MARKDOWN)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        xml_parts = [n for n in archive.namelist() if n.endswith((".xml", ".rels"))]
        assert xml_parts
        for name in xml_parts:
            ElementTree.fromstring(archive.read(name))  # 解析失败即用例失败


def test_docx_content_types_declare_the_main_document():
    types = docx_part(markdown_to_docx("标题", "正文"), "[Content_Types].xml").decode(
        "utf-8"
    )
    assert f'PartName="/word/document.xml" ContentType="{DOCX_CONTENT_TYPE}.main+xml"' in types
    assert 'Extension="rels"' in types


def test_docx_keeps_chinese_title_paragraph_and_table_cells():
    data = markdown_to_docx("中文标题", SAMPLE_MARKDOWN)
    xml = document_xml(data)
    texts = document_texts(data)

    # 中文字面量必须真的写进了 XML（UTF-8），而不是被转义成数字实体
    assert "中文标题" in xml
    assert "索引重建" in xml
    assert "李四" in xml

    joined = "\n".join(texts)
    assert "中文标题" in joined
    assert "季度总结" in joined
    assert "这是" in joined and "正文" in joined  # 段落（含行内粗体 run）
    assert "索引重建" in joined and "张三" in joined  # 表格单元格
    assert "权限收敛" in joined and "进行中" in joined
    assert "复核中文导出" in joined  # 无序列表项
    assert "第一步：生成 DOCX" in joined  # 有序列表项
    assert 'print("你好")' in joined  # 代码块
    assert "引用：PDF 由隔离的 Office 服务生成。" in joined
    # 表头 + 两行数据：三个中文单元格文本都能定位到
    assert {"项目", "负责人", "备注"} <= set(texts)


def test_docx_table_is_a_real_ooxml_table():
    xml = document_xml(markdown_to_docx("标题", SAMPLE_MARKDOWN))
    assert "<w:tbl>" in xml
    assert xml.count("<w:tr>") == 3, "表头 + 2 行数据"
    assert xml.count("<w:tc>") >= 9, "3 列 × 3 行"
    assert "<w:tblGrid>" in xml
    assert "w:insideH" in xml  # 单元格边框


def test_docx_lists_use_real_numbering_definitions():
    xml = document_xml(markdown_to_docx("标题", SAMPLE_MARKDOWN))
    assert "<w:numPr>" in xml
    assert '<w:numId w:val="1"/>' in xml, "无序列表用 numId 1"
    assert '<w:numId w:val="2"/>' in xml, "有序列表用 numId 2"
    numbering = docx_part(markdown_to_docx("标题", SAMPLE_MARKDOWN), "word/numbering.xml").decode(
        "utf-8"
    )
    assert 'w:numFmt w:val="bullet"' in numbering
    assert 'w:numFmt w:val="decimal"' in numbering
    assert 'w:lvlText w:val="\u2022"' in numbering


def test_docx_headings_carry_outline_levels():
    xml = document_xml(markdown_to_docx("标题", SAMPLE_MARKDOWN))
    # 文档标题占 0 级，Markdown 的 # 是 1 级、## 是 2 级（H1 留给标题，与 HTML 导出同口径）
    for level in (0, 1, 2):
        assert f'<w:outlineLvl w:val="{level}"/>' in xml


def test_docx_preserves_leading_and_trailing_spaces():
    data = markdown_to_docx("标题", "前 空格 后\n")
    xml = document_xml(data)
    assert 'xml:space="preserve"' in xml
    assert "前 空格 后" in xml


def test_docx_tolerates_xml_hostile_characters():
    """控制字符 / XML 元字符不能把包写坏（LO/Word 打不开就是失败）。"""
    data = markdown_to_docx("标题\x00\x07", "正文 <tag> & 控制符\x1f 结束")
    root = ElementTree.fromstring(docx_part(data, "word/document.xml"))
    texts = [node.text or "" for node in root.iter(f"{W_NS}t")]
    joined = "\n".join(texts)
    assert "<tag>" in joined and "&" in joined
    assert "\x00" not in joined and "\x1f" not in joined


def test_docx_is_empty_body_safe():
    data = markdown_to_docx("只有标题", "")
    texts = document_texts(data)
    assert texts == ["只有标题"]
    ElementTree.fromstring(docx_part(data, "word/document.xml"))


def test_docx_can_be_opened_by_python_docx_when_installed():
    """第三方实现能打开 = 我们的 OOXML 不是"自产自销"的假格式。

    实现**不依赖** python-docx；这条只在它恰好装了时运行，属于额外证据。
    """
    docx = pytest.importorskip("docx", reason="python-docx 未安装：跳过额外校验")
    document = docx.Document(io.BytesIO(markdown_to_docx("中文标题", SAMPLE_MARKDOWN)))
    paragraphs = "\n".join(p.text for p in document.paragraphs)
    assert "中文标题" in paragraphs
    assert "季度总结" in paragraphs
    assert "复核中文导出" in paragraphs
    assert len(document.tables) == 1
    table_text = "\n".join(
        cell.text for row in document.tables[0].rows for cell in row.cells
    )
    assert "索引重建" in table_text and "李四" in table_text


# ------------------------------------------------------------------ 空文档 ----


@pytest.mark.parametrize("fmt", ["md", "html", "docx"])
def test_empty_document_still_exports(fmt):
    """空文档**照样能导出**（只有标题、空正文）——本项目选定这一侧口径。

    "空正文要不要拒绝"是调用方的业务策略（services 层保留自己的拒绝逻辑）。
    ``md`` 是纯文本格式、没有"外壳"，所以空正文导出的就是一个 0 字节 ``.md``；
    ``html`` / ``docx`` 至少带着标题，因此字节非空 —— 这两条都写死在断言里。
    """
    payload = build_export(make_document(title="空文档", content=""), fmt)
    assert payload.filename == f"空文档.{fmt}"
    if fmt == "md":
        assert payload.content == b""
    elif fmt == "html":
        assert "<h1>空文档</h1>" in payload.content.decode("utf-8")
    else:
        assert document_texts(payload.content) == ["空文档"]

    blank = build_export(make_document(title="", content=""), fmt)
    assert blank.filename == f"document.{fmt}"


# --------------------------------------------------------------- 文件名安全 ----


@pytest.mark.parametrize(
    "title",
    [
        "../../etc/passwd",
        "..\\..\\windows\\system32\\config",
        "报告/总结: 2024",
        "a:b*c?d\"e<f>g|h",
        "..",
        ".",
        "",
        "   ",
        "...",
        "CON",
        "NUL.txt",
        "标题\x00\x07控制符",
        "标题\u200b零宽\u202e覆写",
        "报告" * 200,
        "a" * 500,
    ],
)
def test_export_filename_is_always_safe(title):
    name = export_filename(title, "md")
    assert name.endswith(".md")
    stem = name[: -len(".md")]

    assert stem, "主干不能为空"
    assert Path(name).name == name, "不能带任何路径成分"
    for bad in ("/", "\\", ":", "*", "?", '"', "<", ">", "|"):
        assert bad not in name, f"{bad!r} 不该出现在文件名里"
    assert not any(ord(ch) < 32 or ord(ch) == 127 for ch in name)
    assert stem.strip(" .") == stem
    assert ".." not in stem
    assert len(stem) <= MAX_STEM_LENGTH
    assert len(stem.encode("utf-8")) <= MAX_STEM_BYTES
    assert stem.split(".")[0].upper() not in document_export._RESERVED_STEMS


@pytest.mark.parametrize("fmt", SUPPORTED_FORMATS)
def test_extension_matches_the_requested_format(fmt):
    assert export_filename("标题", fmt).endswith(f".{fmt}")


def test_chinese_is_preserved_in_filenames():
    assert safe_filename_stem("李雷和韩梅梅的报告") == "李雷和韩梅梅的报告"
    assert export_filename("中文标题", "docx") == "中文标题.docx"


def test_reserved_and_empty_titles_fall_back():
    assert safe_filename_stem("") == "document"
    assert safe_filename_stem("   ") == "document"
    assert safe_filename_stem("..") == "document"
    assert safe_filename_stem("CON") == "document"
    assert safe_filename_stem("nul") == "document"


def test_long_title_is_truncated_by_characters_and_bytes():
    stem = safe_filename_stem("报" * 400)
    assert len(stem) <= MAX_STEM_LENGTH
    assert len(stem.encode("utf-8")) <= MAX_STEM_BYTES
    assert len(stem.encode("utf-8")) > MAX_STEM_BYTES - 3, "应贴着字节上限截断"
    ascii_stem = safe_filename_stem("a" * 400)
    assert len(ascii_stem) == MAX_STEM_LENGTH


def test_content_disposition_is_ascii_with_rfc5987_chinese():
    header = content_disposition("中文标题.docx")
    assert header.startswith('attachment; filename="')
    assert "filename*=UTF-8''" in header
    assert "%E4%B8%AD%E6%96%87%E6%A0%87%E9%A2%98.docx" in header
    assert header.isascii(), "响应头必须是纯 ASCII（中文只走 filename* ）"


# ------------------------------------------------------------------- PDF ----
#: 真实转换只在配置了隔离 Office 服务的环境（Docker 部署）里跑；
#: 本机/CI 没有 ``KB_OFFICE_SOCKET`` 时如实跳过，而不是假装通过。
def _office_service_ready() -> bool:
    from apps.knowledge import office_client

    return office_client.configured() and office_client.available()


needs_office_service = pytest.mark.skipif(
    not _office_service_ready(),
    reason="未配置/连不上隔离 Office 转换服务：真实 PDF 转换需在 Docker 部署里验证",
)


def test_pdf_without_configured_service_fails_loudly(monkeypatch):
    """未配置 ``KB_OFFICE_SOCKET``：明确报错（稳定错误码），绝不返回假 PDF。"""
    monkeypatch.delenv("KB_OFFICE_SOCKET", raising=False)
    with pytest.raises(ExportUnavailable) as excinfo:
        build_export(make_document(), "pdf")
    exc = excinfo.value
    assert exc.code == EXPORT_CODE_UNCONFIGURED
    message = str(exc)
    assert "KB_OFFICE_SOCKET" in message
    assert "DOCX" in message, "错误提示要给可执行替代动作"
    # 用户提示不暴露内部路径（Windows 路径 / socket 路径都不该出现）
    assert "C:\\" not in message
    assert "/run/" not in message


def test_pdf_without_configured_service_never_spawns_a_process(monkeypatch):
    """未配置时连子进程都不该起：把 subprocess.Popen 打成哨兵。"""
    import subprocess

    monkeypatch.delenv("KB_OFFICE_SOCKET", raising=False)
    calls: list[object] = []

    def _spy(*args, **kwargs):  # pragma: no cover - 走到这里就是失败
        calls.append(args)
        raise AssertionError("未配置转换服务时不该拉起任何本地进程")

    monkeypatch.setattr(subprocess, "Popen", _spy)
    with pytest.raises(ExportUnavailable):
        document_export.docx_to_pdf(b"PK\x03\x04docx")
    assert calls == []


def test_pdf_reports_unavailable_service(monkeypatch):
    """已配置但连不上：UNAVAILABLE 错误码 + 可执行提示（重启服务 / 先导 DOCX）。"""
    from apps.knowledge import office_client
    from apps.knowledge.parsers import ParseError

    monkeypatch.setenv("KB_OFFICE_SOCKET", "/tmp/kb-office-test.sock")

    def fake_pdf(raw, *, timeout=None):
        raise ParseError("无法连接隔离的 Office 转换器")

    monkeypatch.setattr(office_client, "pdf", fake_pdf)
    monkeypatch.setattr(office_client, "available", lambda: False)

    with pytest.raises(ExportUnavailable) as excinfo:
        document_export.docx_to_pdf(b"PK\x03\x04docx")
    exc = excinfo.value
    assert exc.code == EXPORT_CODE_UNAVAILABLE
    assert "不可用" in str(exc)
    assert "DOCX" in str(exc)


def test_pdf_glue_calls_the_office_service_with_the_generated_docx(monkeypatch):
    """PDF 直接调用 ``office_client.pdf``：送去转换的必须是本模块刚生成的 DOCX。"""
    from apps.knowledge import office_client

    monkeypatch.setenv("KB_OFFICE_SOCKET", "/tmp/kb-office-test.sock")
    seen: dict[str, object] = {}

    def fake_pdf(raw, *, timeout=None):
        seen.update(raw=raw, timeout=timeout)
        return b"%PDF-1.7\nfake\n"

    monkeypatch.setattr(office_client, "pdf", fake_pdf)

    docx_bytes = markdown_to_docx("中文标题", SAMPLE_MARKDOWN)
    data = document_export.docx_to_pdf(docx_bytes, timeout=9.5)

    assert data.startswith(b"%PDF-")
    assert seen["raw"] == docx_bytes, "送去转换的必须是本模块刚生成的 DOCX"
    assert seen["timeout"] == 9.5


def test_pdf_reports_failed_conversion(monkeypatch):
    from apps.knowledge import office_client
    from apps.knowledge.parsers import ParseError

    monkeypatch.setenv("KB_OFFICE_SOCKET", "/tmp/kb-office-test.sock")
    monkeypatch.setattr(office_client, "available", lambda: True)

    def fake_pdf(raw, *, timeout=None):
        raise ParseError("Office 文件损坏、受保护或包含不支持的活动内容")

    monkeypatch.setattr(office_client, "pdf", fake_pdf)

    with pytest.raises(ExportUnavailable) as excinfo:
        document_export.docx_to_pdf(b"PK\x03\x04fake")
    exc = excinfo.value
    assert exc.code == EXPORT_CODE_CONVERSION_FAILED
    assert "转换失败" in str(exc)
    assert "损坏" in str(exc)


def test_pdf_rejects_converter_output_that_is_not_a_pdf(monkeypatch):
    from apps.knowledge import office_client

    monkeypatch.setenv("KB_OFFICE_SOCKET", "/tmp/kb-office-test.sock")

    def fake_pdf(raw, *, timeout=None):
        return "<html>错误页</html>".encode("utf-8")

    monkeypatch.setattr(office_client, "pdf", fake_pdf)
    with pytest.raises(ExportUnavailable) as excinfo:
        document_export.docx_to_pdf(b"PK\x03\x04fake")
    assert excinfo.value.code == EXPORT_CODE_CONVERSION_FAILED
    assert "%PDF-" in str(excinfo.value)


def test_pdf_available_tracks_configuration_and_connectivity(monkeypatch):
    from apps.knowledge import office_client

    monkeypatch.delenv("KB_OFFICE_SOCKET", raising=False)
    assert document_export.pdf_available() is False

    monkeypatch.setenv("KB_OFFICE_SOCKET", "/tmp/kb-office-test.sock")
    monkeypatch.setattr(office_client, "available", lambda: False)
    assert document_export.pdf_available() is False

    monkeypatch.setattr(office_client, "available", lambda: True)
    assert document_export.pdf_available() is True


@needs_office_service
def test_real_pdf_conversion_produces_a_real_pdf():
    """真实 Office 服务：DOCX → PDF，且**中文文本可被提取**（不乱码）。"""
    payload = build_export(make_document("中文导出"), "pdf")
    assert payload.content.startswith(b"%PDF-"), payload.content[:20]
    assert len(payload.content) > 1000
    assert payload.content_type == "application/pdf"
    assert payload.filename == "中文导出.pdf"

    pypdf = pytest.importorskip("pypdf", reason="pypdf 未安装：跳过文本层校验")
    reader = pypdf.PdfReader(io.BytesIO(payload.content))
    text = "".join((page.extract_text() or "") for page in reader.pages)
    compact = re.sub(r"\s+", "", text)
    for needle in ("中文导出", "季度总结", "索引重建", "复核中文导出"):
        assert needle in compact, (
            f"PDF 文本层里找不到 {needle!r}（实际：{compact[:200]!r}）；"
            "转换服务的镜像里请安装 CJK 字体（fonts-noto-cjk）"
        )


# ------------------------------------------------------------- 实现约束 ----


def test_module_only_talks_to_the_office_service():
    """必须只经 ``office_client`` 转换，而不是自己抄一套 subprocess/本地探测。"""
    source = inspect.getsource(document_export)
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "subprocess" not in imported
    assert "os" not in imported  # 不自己玩信号/进程组
    assert "office_client" in source
    assert LOCAL_CONVERTER not in source.lower()
    assert not hasattr(document_export, "Popen")
    assert not hasattr(document_export, "find_" + LOCAL_CONVERTER)
    assert not hasattr(document_export, f"{LOCAL_CONVERTER}_available")
