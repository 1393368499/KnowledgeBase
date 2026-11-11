"""预览加固的专项用例（独立审核指出的边界）。

覆盖：
  * `raw_path` 指向 raw 根内**别人的 blob** → 必须拒绝（sha 绑定）；
  * `raw_path` 与内容指纹不一致 → **直接拒绝**，绝不静默 fallback 到内容寻址路径；
  * 原件字节与指纹不符 → 拒绝预览；
  * 文本预览**有界**（巨文件只读上限字节数，不整读）；
  * OOXML zip bomb 防护（条目数/解压后总量）；
  * PDF 缩放 NaN/Inf → 400；超大 PDF → 不渲染且如实说明。
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest

from apps.knowledge import parsers, preview, storage

PDF_URL = "/api/v1/knowledge/files/{pk}/preview/"
PAGE_URL = "/api/v1/knowledge/files/{pk}/preview/pages/{page}/"
DL_URL = "/api/v1/knowledge/files/{pk}/download/"


def _lib_and_record(make_library, make_file, grant_member, user, code, name, **extra):
    library = make_library(code=code, name=f"{code}-库")
    grant_member(library, user, role="admin")
    return library, make_file(library=library, name=name, **extra)


def test_raw_path_pointing_to_another_blob_is_rejected(
    db, user, auth_api, make_library, make_file, grant_member
):
    """**sha 绑定**：`raw_path` 不能指向 raw 根里**别人的**原件。"""
    other = storage.store_bytes(b"%PDF-1.4 other user's file\n")
    _lib, record = _lib_and_record(
        make_library, make_file, grant_member, user, "hx-bind", "mine.pdf",
        sha="a" * 64, raw_path=other.rel_path,
    )
    body = auth_api(user).get(PDF_URL.format(pk=record.pk)).json()
    assert body["original_present"] is False, "竟然接受了指向他人 blob 的路径"
    assert "不一致" in body["note"] or "越界" in body["note"], body["note"]
    assert auth_api(user).get(DL_URL.format(pk=record.pk)).status_code == 404


def test_raw_path_mismatch_does_not_silently_fall_back(
    db, user, auth_api, make_library, make_file, grant_member
):
    """路径异常时**不得**悄悄改用内容寻址路径（那会掩盖问题）。"""
    stored = storage.store_bytes(b"%PDF-1.4 real content\n")
    _lib, record = _lib_and_record(
        make_library, make_file, grant_member, user, "hx-nofall", "real.pdf",
        sha=stored.sha256, raw_path="some/other/place.pdf",
    )
    body = auth_api(user).get(PDF_URL.format(pk=record.pk)).json()
    assert body["original_present"] is False, "路径不一致却被静默回退成功"
    assert "不一致" in body["note"] or "越界" in body["note"]


def test_content_hash_mismatch_is_rejected(
    db, user, auth_api, make_library, make_file, grant_member
):
    """路径对得上、但**字节与指纹不符** → 拒绝预览（防止张冠李戴）。"""
    stored = storage.store_bytes(b"%PDF-1.4 content A\n")
    _lib, record = _lib_and_record(
        make_library, make_file, grant_member, user, "hx-hash", "tampered.pdf",
        sha="b" * 64,  # 与真实 blob 的指纹不同
        raw_path=stored.rel_path,
    )
    body = auth_api(user).get(PDF_URL.format(pk=record.pk)).json()
    assert body["original_present"] is False
    assert "不一致" in body["note"] or "指纹" in body["note"], body["note"]


def test_text_preview_is_bounded(db, user, auth_api, make_library, make_file, grant_member):
    """巨文本只读上限字节数（旧实现 read_bytes() 会整读）。"""
    payload_bytes = b"x" * (preview.MAX_TEXT_BYTES + 5_000)
    stored = storage.store_bytes(payload_bytes)
    _lib, record = _lib_and_record(
        make_library, make_file, grant_member, user, "hx-bound", "big.txt",
        sha=stored.sha256, raw_path=stored.rel_path, size_bytes=len(payload_bytes),
    )
    body = auth_api(user).get(PDF_URL.format(pk=record.pk)).json()
    assert body["kind"] == "text"
    assert body["truncated"] is True, "未标注截断"
    assert len(body["text"]) == preview.MAX_TEXT_BYTES, len(body["text"])


def test_ooxml_zip_bomb_guard(monkeypatch, tmp_path):
    """解压后总量超限 → 拒绝（用较小的阈值验证守卫逻辑本身）。"""
    import zipfile

    target = tmp_path / "bomb.xlsx"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("a" * 100, b"0" * 1000)          # 文件名较长、内容 1000 字节
        archive.writestr("b" * 100, b"0" * 1000)
    monkeypatch.setattr(preview, "MAX_ZIP_UNCOMPRESSED", 1500)
    with pytest.raises(ValueError) as exc:
        preview._guard_ooxml(target)
    assert "解压后过大" in str(exc.value)


def test_ooxml_entry_count_guard(monkeypatch, tmp_path):
    import zipfile

    target = tmp_path / "many.docx"
    with zipfile.ZipFile(target, "w") as archive:
        for index in range(6):
            archive.writestr(f"e{index}", b"x")
    monkeypatch.setattr(preview, "MAX_ZIP_ENTRIES", 3)
    with pytest.raises(ValueError) as exc:
        preview._guard_ooxml(target)
    assert "条目过多" in str(exc.value)


def test_xlsx_ingestion_rejects_oversized_expanded_package(monkeypatch):
    import zipfile

    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/worksheets/sheet1.xml", b" " * 2_000)
    monkeypatch.setattr(parsers, "MAX_ZIP_UNCOMPRESSED", 1_000)
    with pytest.raises(parsers.ParseError, match="解压后过大"):
        parsers.parse_bytes(raw.getvalue(), "oversized.xlsx")


def test_pdf_scale_nan_or_inf_is_rejected(db, user, auth_api, make_library, make_file, grant_member):
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    buffer = io.BytesIO()
    writer.write(buffer)
    writer.close()
    stored = storage.store_bytes(buffer.getvalue())
    _lib, record = _lib_and_record(
        make_library, make_file, grant_member, user, "hx-nan", "nan.pdf",
        sha=stored.sha256, raw_path=stored.rel_path,
    )
    client = auth_api(user)
    for value in ("nan", "inf", "-inf", "abc"):
        resp = client.get(PAGE_URL.format(pk=record.pk, page=1) + f"?scale={value}")
        assert resp.status_code == 400, f"scale={value} → {resp.status_code}"
        assert b"\x89PNG" not in resp.content


def test_zero_page_pdf_is_reported(db, user, auth_api, make_library, make_file, grant_member):
    """0 页 PDF：如实说明，不给出可翻页的假象。"""
    from pypdf import PdfWriter

    writer = PdfWriter()          # 不加任何页
    buffer = io.BytesIO()
    writer.write(buffer)
    writer.close()
    stored = storage.store_bytes(buffer.getvalue())
    _lib, record = _lib_and_record(
        make_library, make_file, grant_member, user, "hx-zero", "zero.pdf",
        sha=stored.sha256, raw_path=stored.rel_path,
    )
    body = auth_api(user).get(PDF_URL.format(pk=record.pk)).json()
    assert body["original_present"] is True
    assert body["index_text_only"] is True or not body.get("page_count")
    assert "页面" in body["note"] or body.get("parse_error")


def test_oversized_pdf_is_refused_without_rendering(
    db, user, auth_api, make_library, make_file, grant_member, monkeypatch
):
    """超过预览上限的 PDF：**不渲染**并如实说明（而不是硬撑着出图）。"""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    buffer = io.BytesIO()
    writer.write(buffer)
    writer.close()
    stored = storage.store_bytes(buffer.getvalue())
    _lib, record = _lib_and_record(
        make_library, make_file, grant_member, user, "hx-big", "big.pdf",
        sha=stored.sha256, raw_path=stored.rel_path,
    )
    monkeypatch.setattr(preview, "MAX_PDF_BYTES", 10)   # 让任何 PDF 都超限
    client = auth_api(user)
    body = client.get(PDF_URL.format(pk=record.pk)).json()
    assert body["index_text_only"] is True
    assert body.get("parse_error"), body
    assert "上限" in body["parse_error"] or "过大" in body["parse_error"]
    assert client.get(PAGE_URL.format(pk=record.pk, page=1)).status_code == 400


# ------------------------------------------- 审核补充：URL/逃逸/缓存头 ----


def test_meta_urls_match_registered_endpoints_and_are_reachable(
    db, user, auth_api, make_library, make_file, grant_member
):
    """元数据里的 URL 必须**与实际注册的端点一致**，且**真的能打开**。

    旧缺陷：`download_url` 被拼成 `.../preview/download/`（未注册）→ 点开就 404。
    """
    from pypdf import PdfWriter
    import io as _io

    writer = PdfWriter()
    writer.add_blank_page(width=80, height=80)
    buffer = _io.BytesIO()
    writer.write(buffer)
    writer.close()
    stored = storage.store_bytes(buffer.getvalue())
    _lib, record = _lib_and_record(
        make_library, make_file, grant_member, user, "hx-url", "url.pdf",
        sha=stored.sha256, raw_path=stored.rel_path,
    )
    client = auth_api(user)
    body = client.get(PDF_URL.format(pk=record.pk)).json()
    assert body["download_url"] == f"/api/v1/knowledge/files/{record.pk}/download/", body["download_url"]
    assert body["page_url"] == f"/api/v1/knowledge/files/{record.pk}/preview/pages/{{page}}/", body["page_url"]
    # **真的能打开**（而不是只有字符串对）
    assert client.get(body["download_url"]).status_code == 200
    assert client.get(body["page_url"].replace("{page}", "1")).status_code == 200


def test_preview_responses_are_not_cached(
    db, user, auth_api, make_library, make_file, grant_member
):
    """切账号/撤权后不得沿用旧页 → 一律 `private, no-store` + `nosniff`。"""
    from pypdf import PdfWriter
    import io as _io

    writer = PdfWriter()
    writer.add_blank_page(width=80, height=80)
    buffer = _io.BytesIO()
    writer.write(buffer)
    writer.close()
    stored = storage.store_bytes(buffer.getvalue())
    _lib, record = _lib_and_record(
        make_library, make_file, grant_member, user, "hx-cache", "cache.pdf",
        sha=stored.sha256, raw_path=stored.rel_path,
    )
    client = auth_api(user)
    for url in (
        PDF_URL.format(pk=record.pk),
        PAGE_URL.format(pk=record.pk, page=1),
        DL_URL.format(pk=record.pk),
    ):
        resp = client.get(url)
        assert resp.status_code == 200, url
        assert resp["Cache-Control"] == "private, no-store", (url, resp["Cache-Control"])
        assert resp["X-Content-Type-Options"] == "nosniff", url


def test_content_addressed_symlink_escape_is_rejected(
    db, user, auth_api, make_library, make_file, grant_member
):
    """**内容寻址路径本身**若是指向外部的符号链接，也必须拒绝（raw_path 为空时同样）。"""
    root = Path(os.path.realpath(storage.raw_root()))
    outside = root.parent / "outside-ca-target.bin"
    outside.write_bytes(b"%PDF-1.4 outside\n")
    digest = "d" * 64
    link = root / digest[:2] / digest
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        if link.exists() or link.is_symlink():
            link.unlink()
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("本环境无法创建符号链接（Windows 权限）")
        # raw_path **留空**，逼迫走内容寻址路径
        _lib, record = _lib_and_record(
            make_library, make_file, grant_member, user, "hx-ca", "ca.pdf", sha=digest
        )
        body = auth_api(user).get(PDF_URL.format(pk=record.pk)).json()
        assert body["original_present"] is False, "内容寻址的符号链接逃逸被接受了"
        assert auth_api(user).get(DL_URL.format(pk=record.pk)).status_code == 404
    finally:
        try:
            if link.is_symlink():
                link.unlink()
        except OSError:
            pass
        outside.unlink(missing_ok=True)


def test_docx_truncation_is_reported(db, user, auth_api, make_library, make_file, grant_member):
    """超过表格上限时必须**明确标记截断**（前端据此提示，不静默少显示）。"""
    import io as _io

    from docx import Document

    document = Document()
    for index in range(5):          # 5 个表 > MAX_DOCX_TABLES(3)
        document.add_paragraph(f"第 {index + 1} 节")
        table = document.add_table(rows=1, cols=2)
        table.cell(0, 0).text = f"T{index}"
        table.cell(0, 1).text = "值"
    buffer = _io.BytesIO()
    document.save(buffer)
    stored = storage.store_bytes(buffer.getvalue())
    _lib, record = _lib_and_record(
        make_library, make_file, grant_member, user, "hx-cut", "cut.docx",
        sha=stored.sha256, raw_path=stored.rel_path,
    )
    body = auth_api(user).get(PDF_URL.format(pk=record.pk)).json()
    assert body["kind"] == "docx"
    assert body["blocks_truncated"] is True, "超过表格上限却未标记截断"
    tables = [b for b in body["blocks"] if b["type"] == "table"]
    assert len(tables) <= preview.MAX_DOCX_TABLES, len(tables)


def test_docx_no_truncation_flag_when_small(db, user, auth_api, make_library, make_file, grant_member):
    """小文档不应误报截断。"""
    import io as _io

    from docx import Document

    document = Document()
    document.add_paragraph("只有一段")
    buffer = _io.BytesIO()
    document.save(buffer)
    stored = storage.store_bytes(buffer.getvalue())
    _lib, record = _lib_and_record(
        make_library, make_file, grant_member, user, "hx-nocut", "small.docx",
        sha=stored.sha256, raw_path=stored.rel_path,
    )
    body = auth_api(user).get(PDF_URL.format(pk=record.pk)).json()
    assert body["blocks_truncated"] is False, body


# ------------------------------------------- 视觉验收小修：列宽与文案 ----


def test_xlsx_trims_trailing_empty_columns(db, user, auth_api, make_library, make_file, grant_member):
    """**真实缺陷**：3 列的工作表原样渲染成 20 列（17 列全空）。

    修复口径：裁掉**尾部全空列**，但**中间的空列必须保留**（列对齐不能乱）。
    """
    import io as _io

    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "三列"
    sheet.append(["名称", "", "数量"])      # 中间列故意留空
    sheet.append(["螺丝", "", 12])
    buffer = _io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    stored = storage.store_bytes(buffer.getvalue())
    _lib, record = _lib_and_record(
        make_library, make_file, grant_member, user, "hx-cols", "cols.xlsx",
        sha=stored.sha256, raw_path=stored.rel_path,
    )
    body = auth_api(user).get(PDF_URL.format(pk=record.pk)).json()
    rows = body["blocks"][0]["rows"]
    assert all(len(row) == 3 for row in rows), f"未裁掉尾部空列：{[len(r) for r in rows]}"
    # 中间空列保留（不能把空列一起挤掉，否则列含义错位）
    assert rows[0] == ["名称", "", "数量"], rows[0]


def test_xlsx_trims_trailing_empty_rows(db, user, auth_api, make_library, make_file, grant_member):
    """尾部全空行同样裁掉（不是显示一片空行）。"""
    import io as _io

    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["A", "B"])
    sheet.append([None, None])          # 中间空行：保留
    sheet.append(["C", "D"])
    buffer = _io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    stored = storage.store_bytes(buffer.getvalue())
    _lib, record = _lib_and_record(
        make_library, make_file, grant_member, user, "hx-rows", "rows.xlsx",
        sha=stored.sha256, raw_path=stored.rel_path,
    )
    rows = auth_api(user).get(PDF_URL.format(pk=record.pk)).json()["blocks"][0]["rows"]
    assert rows == [["A", "B"], ["", ""], ["C", "D"]], rows


def test_xlsx_column_cap_is_still_reported_after_trimming(
    db, user, auth_api, make_library, make_file, grant_member
):
    """超过列上限时，**裁列之后仍要如实标记截断**（不能把"被截"这件事裁没）。"""
    import io as _io

    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.append([f"c{index}" for index in range(preview.MAX_TABLE_COLS + 5)])
    buffer = _io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    stored = storage.store_bytes(buffer.getvalue())
    _lib, record = _lib_and_record(
        make_library, make_file, grant_member, user, "hx-cap", "cap.xlsx",
        sha=stored.sha256, raw_path=stored.rel_path,
    )
    body = auth_api(user).get(PDF_URL.format(pk=record.pk)).json()
    assert body["blocks_truncated"] is True, "超过列上限却未标记截断"


def test_user_visible_copy_has_no_markdown_markers(
    db, user, auth_api, make_library, make_file, grant_member
):
    """**用户可见文案不得带 Markdown 标记**（真实缺陷：note 里的 `**结构化提取**` 被原样显示）。"""
    import io as _io

    from docx import Document

    document = Document()
    document.add_paragraph("一段")
    buffer = _io.BytesIO()
    document.save(buffer)
    stored = storage.store_bytes(buffer.getvalue())
    _lib, record = _lib_and_record(
        make_library, make_file, grant_member, user, "hx-md", "md.docx",
        sha=stored.sha256, raw_path=stored.rel_path,
    )
    body = auth_api(user).get(PDF_URL.format(pk=record.pk)).json()
    assert "**" not in body["note"], body["note"]

    # PDF 的 note 同样不能带星号
    from pypdf import PdfWriter
    import io as _io2

    writer = PdfWriter()
    writer.add_blank_page(width=60, height=60)
    pdf_buffer = _io2.BytesIO()
    writer.write(pdf_buffer)
    writer.close()
    pdf_stored = storage.store_bytes(pdf_buffer.getvalue())
    _lib2, pdf_record = _lib_and_record(
        make_library, make_file, grant_member, user, "hx-md2", "md.pdf",
        sha=pdf_stored.sha256, raw_path=pdf_stored.rel_path,
    )
    pdf_body = auth_api(user).get(PDF_URL.format(pk=pdf_record.pk)).json()
    assert "**" not in pdf_body["note"], pdf_body["note"]
