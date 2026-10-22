"""原件预览：真实呈现 + 安全边界（用户复现缺陷的定向回归）。

缺陷背景：三个入口把 `chunks.text` 放进 `<p>` —— PDF 的原页/表格/水印全丢，
文字挤成一段，水印还会混进正文。本套用例锁死**后端**的真实行为：

  * PDF **按原页**出 PNG（不是索引文字）；
  * 权限：不可读 → 404（不泄露存在性）；匿名不给数据；
  * 页号有界（0 → 400，越界 → 404）；
  * 坏 PDF **如实报错**（不返回空图、不冒充可渲染）；
  * 原件缺失 → 明确标注"只能看索引文字"；
  * 路径逃逸（`../` 与符号链接）→ 拒绝；
  * **原件字节不变**（预览/下载都不得改动原件）；
  * 文本保留换行；DOCX/XLSX 结构化提取并标注"非原始版式"。

全部使用**合成文件**（本用例现场生成 PDF/DOCX/XLSX/TXT），不触碰任何既有原件。
"""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path

import pytest

from apps.knowledge import storage

PDF_URL = "/api/v1/knowledge/files/{pk}/preview/"
PAGE_URL = "/api/v1/knowledge/files/{pk}/preview/pages/{page}/"
DL_URL = "/api/v1/knowledge/files/{pk}/download/"


def _synth_pdf(pages: int = 2) -> bytes:
    """合成一个真实的 PDF（pypdf 写空页）——不依赖任何外部素材。"""
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    writer.close()
    return buffer.getvalue()


def _make_real(make_library, make_file, grant_member, user, name: str, data: bytes, code: str):
    """建一份**有真实原件**的资料（内容寻址落盘在隔离的 raw store 里）。"""
    library = make_library(code=code, name=f"{code}-库")
    grant_member(library, user, role="admin")
    stored = storage.store_bytes(data)
    # **必须在创建时写入** `raw_path`：它是不可篡改列，事后 save 会被守卫拒绝
    record = make_file(
        library=library,
        name=name,
        sha=stored.sha256,
        raw_path=stored.rel_path,
        raw_size_bytes=len(data),
    )
    return record, library


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _original_path(record) -> Path:
    from apps.knowledge.preview import resolve_original

    path, reason = resolve_original(record)
    assert path is not None, reason
    return path


# --------------------------------------------------------------- PDF 原页 ----


def test_pdf_meta_reports_real_page_count(db, user, auth_api, make_library, make_file, grant_member):
    record, _lib = _make_real(
        make_library, make_file, grant_member, user, "two-pages.pdf", _synth_pdf(2), "pv-pdf"
    )
    client = auth_api(user)
    body = client.get(PDF_URL.format(pk=record.pk)).json()
    assert body["kind"] == "pdf"
    assert body["page_count"] == 2, body
    assert body["original_present"] is True
    assert body["index_text_only"] is False
    assert body["external_model_calls"] is False, "预览不得涉及模型外呼"


def test_pdf_page_endpoint_returns_real_png(
    db, user, auth_api, make_library, make_file, grant_member
):
    """**核心**：页面端点是真实 PDF 渲染出的 PNG（不是索引文字、不是空图）。"""
    record, _lib = _make_real(
        make_library, make_file, grant_member, user, "render.pdf", _synth_pdf(2), "pv-page"
    )
    client = auth_api(user)
    for page in (1, 2):
        resp = client.get(PAGE_URL.format(pk=record.pk, page=page))
        assert resp.status_code == 200, resp.content[:200]
        assert resp["Content-Type"] == "image/png"
        assert resp["X-Preview-Page"] == str(page)
        data = resp.content
        assert data[:8] == b"\x89PNG\r\n\x1a\n", "不是真正的 PNG"
        assert len(data) > 200, "PNG 过小，疑似空图"


def test_pdf_page_bounds(db, user, auth_api, make_library, make_file, grant_member):
    record, _lib = _make_real(
        make_library, make_file, grant_member, user, "bounds.pdf", _synth_pdf(2), "pv-bounds"
    )
    client = auth_api(user)
    assert client.get(PAGE_URL.format(pk=record.pk, page=0)).status_code == 400
    assert client.get(PAGE_URL.format(pk=record.pk, page=3)).status_code == 404


def test_bad_pdf_is_reported_not_faked(db, user, auth_api, make_library, make_file, grant_member):
    """坏 PDF：元数据**如实报错**，页面端点返回错误码（绝不返回空图当成功）。"""
    record, _lib = _make_real(
        make_library, make_file, grant_member, user, "broken.pdf",
        b"%PDF-1.4\nthis is not a real pdf\n", "pv-bad",
    )
    client = auth_api(user)
    body = client.get(PDF_URL.format(pk=record.pk)).json()
    assert body["original_present"] is True
    assert body.get("parse_error"), "坏 PDF 必须给出解析错误"
    assert body["index_text_only"] is True
    assert "损坏" in body["note"] or "无法" in body["note"]
    resp = client.get(PAGE_URL.format(pk=record.pk, page=1))
    assert resp.status_code in (404, 422), resp.status_code
    assert b"\x89PNG" not in resp.content, "坏 PDF 竟然返回了 PNG"


# ------------------------------------------------------------------ 权限 ----


def test_other_user_gets_404_on_all_preview_endpoints(
    db, user, other_user, auth_api, make_library, make_file, grant_member
):
    record, _lib = _make_real(
        make_library, make_file, grant_member, user, "private.pdf", _synth_pdf(1), "pv-priv"
    )
    client = auth_api(other_user)
    for url in (
        PDF_URL.format(pk=record.pk),
        PAGE_URL.format(pk=record.pk, page=1),
        DL_URL.format(pk=record.pk),
    ):
        resp = client.get(url)
        assert resp.status_code == 404, f"{url} → {resp.status_code}（应 404，不泄露存在性）"


# ------------------------------------------------------- 缺少原件 / 逃逸 ----


def test_missing_original_is_labelled_index_text_only(
    db, user, auth_api, make_library, make_file, grant_member
):
    """没有原件的记录：必须明确说"只能看索引文字"，**不得**假装能预览 PDF。"""
    library = make_library(code="pv-none", name="无原件库")
    grant_member(library, user, role="admin")
    record = make_file(library=library, name="ghost.pdf")  # 没有 raw_path / blob
    client = auth_api(user)
    body = client.get(PDF_URL.format(pk=record.pk)).json()
    assert body["original_present"] is False
    assert body["index_text_only"] is True
    assert "原件" in body["note"]
    assert body.get("page_count") is None
    assert client.get(PAGE_URL.format(pk=record.pk, page=1)).status_code == 404
    assert client.get(DL_URL.format(pk=record.pk)).status_code == 404


def test_path_escape_is_rejected(db, user, auth_api, make_library, make_file, grant_member):
    """`raw_path` 越界（`../`）必须被拒绝，绝不能读出 raw 根之外的文件。"""
    library = make_library(code="pv-esc", name="越界库")
    grant_member(library, user, role="admin")
    record = make_file(
        library=library, name="escape.pdf", sha="f" * 64, raw_path="../outside-secret.txt"
    )
    outside = Path(os.path.realpath(storage.raw_root())).parent / "outside-secret.txt"
    outside.write_text("SECRET-OUTSIDE", encoding="utf-8")
    try:
        client = auth_api(user)
        body = client.get(PDF_URL.format(pk=record.pk)).json()
        assert body["original_present"] is False, "越界路径竟然被当成原件"
        assert client.get(DL_URL.format(pk=record.pk)).status_code == 404
    finally:
        outside.unlink(missing_ok=True)


def test_symlink_escape_is_rejected(db, user, auth_api, make_library, make_file, grant_member):
    """指向 raw 根之外的**符号链接**同样必须被拒绝（Windows 上可能无权限创建 → 跳过）。"""
    library = make_library(code="pv-link", name="链接库")
    grant_member(library, user, role="admin")
    root = Path(os.path.realpath(storage.raw_root()))
    outside = root.parent / "outside-target.txt"
    outside.write_text("SECRET", encoding="utf-8")
    link = root / "evil-link.pdf"
    try:
        if link.exists() or link.is_symlink():
            link.unlink()
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("本环境无法创建符号链接（Windows 权限）")
        # 记录在**创建时**就指向那个符号链接（不可篡改列不能事后改）
        record = make_file(
            library=library, name="link.pdf", sha="e" * 64, raw_path="evil-link.pdf"
        )
        client = auth_api(user)
        body = client.get(PDF_URL.format(pk=record.pk)).json()
        assert body["original_present"] is False, "符号链接逃逸竟然被接受"
    finally:
        try:
            if link.is_symlink():
                link.unlink()
        except OSError:
            pass
        outside.unlink(missing_ok=True)


# ------------------------------------------------- 原件字节不变 / 下载 ------


def test_original_bytes_unchanged_by_preview_and_download(
    db, user, auth_api, make_library, make_file, grant_member
):
    """**硬约束**：预览与下载都不得改动原件字节。"""
    data = _synth_pdf(1)
    record, _lib = _make_real(
        make_library, make_file, grant_member, user, "immutable.pdf", data, "pv-imm"
    )
    path = _original_path(record)
    before = _sha(path)
    client = auth_api(user)
    client.get(PDF_URL.format(pk=record.pk))
    client.get(PAGE_URL.format(pk=record.pk, page=1))
    downloaded = client.get(DL_URL.format(pk=record.pk))
    assert downloaded.status_code == 200
    body_bytes = b"".join(downloaded.streaming_content)
    assert body_bytes == data, "下载内容与原件不一致"
    assert downloaded["X-Original-Sha256"] == record.content_sha256
    assert _sha(path) == before, "原件字节被改动了"


# -------------------------------------------------------- 文本 / 结构化 -----


def test_text_preview_preserves_newlines_and_escapes_client_side(
    db, user, auth_api, make_library, make_file, grant_member
):
    text = "第一行\n第二行\n\n第四行 <script>alert(1)</script>\n"
    record, _lib = _make_real(
        make_library, make_file, grant_member, user, "note.txt", text.encode("utf-8"), "pv-txt"
    )
    body = auth_api(user).get(PDF_URL.format(pk=record.pk)).json()
    assert body["kind"] == "text"
    assert body["text"] == text, "文本必须原样（保留换行）"
    assert body["truncated"] is False


def test_docx_is_structured_and_labelled_not_original_layout(
    db, user, auth_api, make_library, make_file, grant_member
):
    from docx import Document

    document = Document()
    document.add_paragraph("第一段")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "A"
    table.cell(0, 1).text = "B"
    buffer = io.BytesIO()
    document.save(buffer)
    record, _lib = _make_real(
        make_library, make_file, grant_member, user, "doc.docx", buffer.getvalue(), "pv-docx"
    )
    body = auth_api(user).get(PDF_URL.format(pk=record.pk)).json()
    assert body["kind"] == "docx"
    assert body["blocks"], body
    kinds = {block["type"] for block in body["blocks"]}
    assert "paragraph" in kinds and "table" in kinds, body["blocks"]
    assert "非原始版式" in body["note"], "必须诚实标注不是原始版式"


def test_xlsx_is_structured(db, user, auth_api, make_library, make_file, grant_member):
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "S1"
    sheet.append(["名称", "数量"])
    sheet.append(["螺丝", 3])
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    record, _lib = _make_real(
        make_library, make_file, grant_member, user, "table.xlsx", buffer.getvalue(), "pv-xlsx"
    )
    body = auth_api(user).get(PDF_URL.format(pk=record.pk)).json()
    assert body["kind"] == "xlsx"
    assert "blocks" in body, "未返回结构化块，note=" + str(body.get("note"))
    assert body["blocks"] and body["blocks"][0]["type"] == "table"
    assert "非原始版式" in body["note"]


def test_index_chunks_endpoint_is_separate_not_used_as_preview(
    db, user, auth_api, make_library, make_file, grant_member
):
    """索引文字仍是独立数据源（客户端只有在原件缺失时才显示它，并明确标注）。"""
    record, _lib = _make_real(
        make_library, make_file, grant_member, user, "sep.pdf", _synth_pdf(1), "pv-sep"
    )
    body = auth_api(user).get(PDF_URL.format(pk=record.pk)).json()
    assert "text" not in body, "PDF 预览元数据里不应混入索引文字"
    assert body["index_text_only"] is False
