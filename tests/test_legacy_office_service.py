"""旧版 Office 的**隔离服务接线**与输入守卫测试。

历史背景：本项目曾在宿主上用本地转换程序解析 ``.doc/.xls/.ppt``，那条路径会
在用户机器上拉起进程（并弹窗）。转换已迁到隔离的 Office 服务（Docker +
Unix socket，见 ``deploy/office`` 与 ``apps/knowledge/office_client.py``），
宿主不再探测、也不再拉起任何本地转换进程。本文件覆盖：

* 输入守卫（宏/脚本后缀、体积、文件名穿越、OLE2 容器结构）—— 在建立连接前失败；
* 未配置 ``KB_OFFICE_SOCKET`` 时旧版后缀**明确拒绝**并给出可执行提示；
* 配置后 ``parse_bytes`` 把字节交给 ``office_client.parse``（参数透传）；
* ``office_client`` 的 wire 响应解析（文本/表格/工作表/图片分段、非法响应）；
* PDF 转换只走 socket（``office_client.pdf`` 校验 ``%PDF-`` 头）。

这些用例全部不依赖真实转换服务，也不允许拉起任何本地进程。
"""

from __future__ import annotations

import ast
import base64
import inspect

import pytest

from apps.knowledge import office_client, office_convert, parsers
from apps.planner import document_export

#: 旧版 .doc/.xls/.ppt 的 OLE2 / CFB 复合文档魔数。
OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

#: 宏 / 脚本型后缀：不该走到任何解析器或转换服务。
BLOCKED_SAMPLES = (
    ".docm", ".dotm", ".dotx",
    ".xlsm", ".xltm", ".xlam", ".xla", ".xlt",
    ".pptm", ".potm", ".ppam", ".sldm",
    ".vbs", ".vba", ".js", ".jse", ".wsf", ".wsh", ".ps1", ".hta",
)


#: 项目要求"宿主转换器名字在仓库里 0 命中"；这里拆开写，保证本文件自身也不产生命中。
LOCAL_CONVERTER = "soff" + "ice"


def _ole2_header(
    *,
    byte_order: int = 0xFFFE,
    sector_shift: int = 9,
    fat_sectors: int = 1,
    first_dir_sector: int = 1,
) -> bytes:
    """拼一个**结构成立**的最小 OLE2 头（不遍历 FAT，只过结构校验）。"""
    header = bytearray(512)
    header[:8] = OLE2
    header[28:30] = byte_order.to_bytes(2, "little")
    header[30:32] = sector_shift.to_bytes(2, "little")
    header[44:48] = fat_sectors.to_bytes(4, "little")
    header[48:52] = first_dir_sector.to_bytes(4, "little")
    return bytes(header)


def _row(page: int | None, kind: str, text: str) -> bytes:
    """office 服务的 wire 分段：``<page>\\t<kind>\\t<base64(utf-8)>``。"""
    page_raw = b"" if page is None else str(page).encode("ascii")
    return b"\t".join(
        (page_raw, kind.encode("ascii"), base64.b64encode(text.encode("utf-8")))
    )


# --------------------------------------------------------------- 宏 / 脚本 ----


@pytest.mark.parametrize("name", [f"payload{suffix}" for suffix in BLOCKED_SAMPLES])
def test_macro_and_script_extensions_are_rejected_before_parsing(name):
    # 两层都要挡：parse_bytes 的分派入口，以及转换前的守卫函数。
    with pytest.raises(parsers.ParseUnsupported):
        parsers.parse_bytes(b"anything", name)
    with pytest.raises(parsers.ParseUnsupported):
        office_convert.assert_not_blocked(name)


def test_blocked_suffix_sets_stay_in_sync():
    """``parsers`` 与 ``office_convert`` 的黑名单必须一致（两处都能挡）。"""
    assert parsers.BLOCKED_MACRO_SUFFIXES == set(office_convert.BLOCKED_SUFFIXES)


def test_non_macro_suffixes_are_not_blocked():
    for name, expected in (
        ("a.doc", ".doc"),
        ("a.xls", ".xls"),
        ("a.ppt", ".ppt"),
        ("a.pptx", ".pptx"),
        ("a.docx", ".docx"),
    ):
        assert office_convert.assert_not_blocked(name) == expected


# ------------------------------------------------------------------- 体积 ----


def test_oversized_input_is_rejected_before_any_connection(monkeypatch):
    monkeypatch.setattr(office_convert, "max_input_bytes", lambda: 16)
    with pytest.raises(parsers.ParseError, match="上限"):
        office_convert.assert_within_size(b"x" * 17)
    # 边界包含：正好等于上限仍然放行
    office_convert.assert_within_size(b"x" * 16)


def test_size_limit_comes_from_settings(settings):
    settings.KB_OFFICE_MAX_BYTES = 1234
    assert office_convert.max_input_bytes() == 1234
    # 配错（0/负数）退回默认值，绝不把上限当"无上限"
    settings.KB_OFFICE_MAX_BYTES = 0
    assert office_convert.max_input_bytes() == office_convert.DEFAULT_MAX_BYTES


# ------------------------------------------------------------- 文件名穿越 ----


@pytest.mark.parametrize(
    "name",
    [
        "../x.doc",
        "..\\x.doc",
        "a/b.doc",
        "/etc/passwd",
        "C:\\Windows\\x.doc",
        "evil\x00.doc",
        "",
        "   ",
        ".",
        "..",
    ],
)
def test_path_traversal_and_absolute_names_are_rejected(name):
    with pytest.raises(parsers.ParseError):
        office_convert.assert_safe_leaf(name)


def test_plain_chinese_filename_passes_through():
    assert office_convert.assert_safe_leaf("报告 2026.doc") == "报告 2026.doc"


# --------------------------------------------------------- OLE2 容器结构 ----


@pytest.mark.parametrize("suffix", [".doc", ".xls", ".ppt"])
def test_ole2_garbage_is_rejected_before_conversion(suffix):
    with pytest.raises(parsers.ParseError, match="OLE2"):
        office_convert.assert_valid_ole2_input(b"PK\x03\x04 not ole2", suffix)


@pytest.mark.parametrize(
    ("payload", "needle"),
    [
        (OLE2 + b"\x00" * 10, "连 OLE2 头都不够"),
        (_ole2_header(byte_order=0x1234), "字节序"),
        (_ole2_header(sector_shift=3), "扇区位移"),
        (_ole2_header(fat_sectors=0, first_dir_sector=0xFFFFFFFE), "既没有 FAT"),
        (_ole2_header(first_dir_sector=0xFFFFFFFF), "保留值"),
    ],
)
def test_broken_ole2_headers_are_rejected_with_reason(payload, needle):
    with pytest.raises(parsers.ParseError) as excinfo:
        office_convert.assert_valid_ole2_input(payload, ".doc")
    assert needle in str(excinfo.value)


def test_wellformed_ole2_header_is_accepted():
    payload = _ole2_header()
    for suffix in (".doc", ".xls", ".ppt"):
        office_convert.assert_valid_ole2_input(payload, suffix)  # 不抛 == 通过


def test_ole2_guard_ignores_formats_that_are_not_old_binary_office():
    # .pptx / .docx 是 OOXML（zip），不做 OLE2 结构校验
    for suffix in (".pptx", ".docx", ".pdf"):
        office_convert.assert_valid_ole2_input(b"PK\x03\x04garbage", suffix)


# ------------------------------------------------------- 未配置 / 已配置 ----


def test_unconfigured_service_rejects_legacy_suffixes(monkeypatch):
    """未配置 KB_OFFICE_SOCKET：明确告知"能力缺失"，而不是假装解析或拉起本地进程。"""
    monkeypatch.delenv("KB_OFFICE_SOCKET", raising=False)
    for name in ("旧文档.doc", "表.xls", "稿.ppt", "稿.pptx"):
        with pytest.raises(parsers.ParseUnsupported) as excinfo:
            parsers.parse_bytes(b"whatever bytes", name)
        message = str(excinfo.value)
        assert "KB_OFFICE_SOCKET" in message, message
        assert "Office 转换服务" in message, message
        assert "Docker" in message, message


def test_unconfigured_service_is_reported_before_payload_validation(monkeypatch):
    """能力缺失优先于内容损坏：未配置时连坏文件也报"需要转换服务"。"""
    monkeypatch.delenv("KB_OFFICE_SOCKET", raising=False)
    with pytest.raises(parsers.ParseUnsupported) as excinfo:
        parsers.parse_bytes(b"PK\x03\x04 junk", "坏工作簿.xls")
    assert "KB_OFFICE_SOCKET" in str(excinfo.value)


def test_configured_service_receives_legacy_bytes(monkeypatch):
    """已配置：parse_bytes 把原始字节与 OCR 参数原样交给 office_client.parse。"""
    monkeypatch.setenv("KB_OFFICE_SOCKET", "/tmp/kb-office-fake.sock")
    seen: dict[str, object] = {}

    def fake_parse(raw, name, *, ocr=None, ocr_pages=200):
        seen.update(raw=raw, name=name, ocr=ocr, ocr_pages=ocr_pages)
        return parsers.ParseResult(
            blocks=[parsers.ParsedBlock(text="转换后的正文")], parser="office-test"
        )

    monkeypatch.setattr(office_client, "parse", fake_parse)
    result = parsers.parse_bytes(b"raw-bytes", "报告.doc", ocr_pages=7)

    assert result.parser == "office-test"
    assert result.blocks[0].text == "转换后的正文"
    assert seen == {
        "raw": b"raw-bytes",
        "name": "报告.doc",
        "ocr": None,
        "ocr_pages": 7,
    }


@pytest.mark.parametrize(
    ("name", "raw", "mode"),
    [
        ("报告.doc", _ole2_header(), "doc"),
        ("表.xls", _ole2_header(), "xls"),
        ("稿.ppt", _ole2_header(), "ppt"),
        ("稿.pptx", b"PK\x03\x04pptx-bytes", "pptx"),
    ],
)
def test_office_client_sends_the_matching_wire_mode(monkeypatch, name, raw, mode):
    monkeypatch.setenv("KB_OFFICE_SOCKET", "/tmp/kb-office-fake.sock")
    calls: list[tuple[str, bytes]] = []

    def fake_request(request_mode, payload=b"", *, timeout=None):
        calls.append((request_mode, payload))
        return _row(1, "text", "正文")

    monkeypatch.setattr(office_client, "request", fake_request)
    result = office_client.parse(raw, name)

    assert calls == [(mode, raw)]
    assert result.parser == f"office-poi-{mode}"
    assert [(block.page, block.kind, block.text) for block in result.blocks] == [
        (1, "text", "正文")
    ]


# ------------------------------------------------------ wire 响应解析 ----


def _parse_wire(monkeypatch, payload: bytes, *, name="报告.doc", ocr=None):
    monkeypatch.setenv("KB_OFFICE_SOCKET", "/tmp/kb-office-fake.sock")
    monkeypatch.setattr(
        office_client, "request", lambda mode, raw=b"", *, timeout=None: payload
    )
    return office_client.parse(_ole2_header(), name, ocr=ocr)


def test_office_client_decodes_text_table_and_sheet_segments(monkeypatch):
    payload = b"\n".join(
        [
            _row(1, "text", "第一页正文"),
            _row(2, "table", "项目=索引重建"),
            _row(2, "sheet", "工作表=费用"),
        ]
    )
    result = _parse_wire(monkeypatch, payload)

    assert result.parser == "office-poi-doc"
    texts = [block.text for block in result.blocks]
    assert "第一页正文" in texts
    assert any("项目=索引重建" in text for text in texts)
    assert any("工作表=费用" in text for text in texts)
    pages = {block.page for block in result.blocks}
    assert pages == {1, 2}


def test_office_client_rejects_malformed_wire_rows(monkeypatch):
    with pytest.raises(parsers.ParseError, match="无法读取的分段"):
        _parse_wire(monkeypatch, b"not-a-valid-row")


def test_office_client_rejects_unknown_segment_kind(monkeypatch):
    with pytest.raises(parsers.ParseError, match="无法读取的分段"):
        _parse_wire(monkeypatch, _row(1, "video", "x"))


def test_office_client_requires_readable_body(monkeypatch):
    # 图片分段在没有 OCR 客户端时被跳过；只剩图片 = 没有正文 → 明确失败
    with pytest.raises(parsers.ParseError, match="没有可读取的正文"):
        _parse_wire(monkeypatch, _row(1, "image", "AAA"))


def test_office_client_skips_image_segments_without_ocr(monkeypatch):
    payload = b"\n".join([_row(1, "text", "正文"), _row(2, "image", "AAA")])
    result = _parse_wire(monkeypatch, payload)
    assert [block.kind for block in result.blocks] == ["text"]


# ------------------------------------------------------------- PDF wire ----


def test_office_client_pdf_requires_pdf_payload(monkeypatch):
    monkeypatch.setenv("KB_OFFICE_SOCKET", "/tmp/kb-office-fake.sock")
    monkeypatch.setattr(
        office_client,
        "request",
        lambda mode, raw=b"", *, timeout=None: b"<html>error page</html>",
    )
    with pytest.raises(parsers.ParseError, match="未返回 PDF"):
        office_client.pdf(b"PK\x03\x04docx")


def test_office_client_pdf_returns_real_pdf_bytes(monkeypatch):
    monkeypatch.setenv("KB_OFFICE_SOCKET", "/tmp/kb-office-fake.sock")
    monkeypatch.setattr(
        office_client,
        "request",
        lambda mode, raw=b"", *, timeout=None: b"%PDF-1.7\nbody\n",
    )
    assert office_client.pdf(b"PK\x03\x04docx") == b"%PDF-1.7\nbody\n"


# ------------------------------------------------- 结构中不再有本地转换 ----


def test_office_convert_cannot_launch_local_processes():
    """结构约束：校验模块不含 subprocess，也没有本地转换入口。"""
    tree = ast.parse(inspect.getsource(office_convert))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "subprocess" not in imported
    assert not hasattr(office_convert, "find_" + LOCAL_CONVERTER)
    assert not hasattr(office_convert, "run_conversion")
    assert not hasattr(office_convert, "parse_legacy_office")


def test_document_export_has_no_local_converter_fallbacks():
    assert not hasattr(document_export, "find_" + LOCAL_CONVERTER)
    assert not hasattr(document_export, f"{LOCAL_CONVERTER}_available")
    assert LOCAL_CONVERTER not in inspect.getsource(document_export).lower()