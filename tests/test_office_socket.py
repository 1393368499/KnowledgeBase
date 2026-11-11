import io
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from apps.knowledge import office_client
from apps.knowledge.parsers import ParseError, parse_bytes
from apps.planner.document_export import ExportUnavailable, build_export
from deploy.office import server


def _package(path, name, payload):
    with ZipFile(path, "w") as archive:
        archive.writestr(name, payload)
    return path


@pytest.mark.parametrize("entry,payload", [
    ("../outside.xml", b"<root/>"),
    ("word/vbaProject.bin", b"active"),
    ("word/document.xml", b'<!DOCTYPE x SYSTEM "file:///etc/passwd"><x/>'),
    ("word/document.xml", b'<w:x xmlns:w="urn:word"><w:fldChar w:fldCharType="begin"/><w:instrText> INCLUDE</w:instrText><w:instrText>PICTURE x </w:instrText><w:fldChar w:fldCharType="end"/></w:x>'),
    ("word/document.xml", b'<w:x xmlns:w="urn:word"><w:fldSimple w:instr="HYPERLINK file:///etc/passwd"/></w:x>'),
    ("word/media/logo.svg", b'<svg><image href="file:///etc/passwd"/></svg>'),
    ("word/_rels/document.xml.rels", b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Type="urn/test/image" TargetMode="External" Target="https://example.invalid/logo.png"/></Relationships>'),
    ("word/_rels/document.xml.rels", b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Type="urn/test/image" Target="file:///etc/passwd"/></Relationships>'),
])
def test_package_rejects_active_or_external_content(tmp_path, entry, payload):
    source = _package(tmp_path / "sample.docx", entry, payload)
    with pytest.raises(server.BadDocument):
        server.validate_package(source, float("inf"))


def test_package_allows_passive_web_hyperlinks_and_page_numbers(tmp_path):
    source = tmp_path / "sample.docx"
    with ZipFile(source, "w") as archive:
        archive.writestr("word/document.xml", '<w:document xmlns:w="urn:word"><w:fldSimple w:instr="PAGE"/></w:document>')
        archive.writestr("word/_rels/document.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Type="urn/test/hyperlink" TargetMode="External" Target="https://example.invalid/"/></Relationships>')
    server.validate_package(source, float("inf"))


def test_package_validation_has_deadline(tmp_path):
    import subprocess

    source = _package(tmp_path / "sample.docx", "word/document.xml", b"<root/>")
    with pytest.raises(subprocess.TimeoutExpired):
        server.validate_package(source, 0)


def test_office_ingestion_uses_socket_and_preserves_slide_metadata(monkeypatch):
    monkeypatch.setenv("KB_OFFICE_SOCKET", "/run/test.sock")
    content = io.BytesIO()
    with ZipFile(content, "w") as archive:
        archive.writestr("ppt/presentation.xml", "<root/>")

    def request(mode, raw, **kwargs):
        assert mode == "pptx" and raw == content.getvalue()
        return b"2\ttable\t5Lit5paH\n"

    monkeypatch.setattr(office_client, "request", request)
    result = parse_bytes(content.getvalue(), "示例.pptx")
    assert result.parser == "office-poi-pptx"
    assert [(block.page, block.kind, block.text) for block in result.blocks] == [(2, "table", "中文")]


def test_pdf_export_uses_socket_and_reports_failure(monkeypatch):
    monkeypatch.setenv("KB_OFFICE_SOCKET", "/run/test.sock")

    def request(mode, raw, **kwargs):
        assert mode == "pdf" and raw.startswith(b"PK")
        return b"%PDF-1.7\n"

    monkeypatch.setattr(office_client, "request", request)
    document = SimpleNamespace(title="合成验收", content_markdown="中文正文。")
    assert build_export(document, "pdf").content == b"%PDF-1.7\n"

    def unavailable(*args, **kwargs):
        raise ParseError("转换超时")

    monkeypatch.setattr(office_client, "request", unavailable)
    with pytest.raises(ExportUnavailable, match="超时"):
        build_export(document, "pdf")
