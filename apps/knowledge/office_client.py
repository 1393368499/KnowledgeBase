"""Call the isolated Office converter over its Docker Unix socket."""

import base64
import binascii
import os
import socket

from . import office_wire
from .ocr import OcrError
from .parsers import MAX_BLOCKS, ParseError, ParseResult, _blocks_from_text, normalize_text


def configured():
    return bool(os.environ.get("KB_OFFICE_SOCKET"))


def request(mode, raw=b"", *, timeout=office_wire.JOB_TIMEOUT + 15):
    if len(raw) > office_wire.MAX_INPUT:
        raise ParseError("Office 文件超过转换器体积上限")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(timeout)
            conn.connect(os.environ["KB_OFFICE_SOCKET"])
            conn.sendall(office_wire.HEADER.pack(office_wire.OPERATIONS[mode], len(raw)))
            conn.sendall(raw)
            with conn.makefile("rb") as stream:
                header = stream.read(office_wire.HEADER.size)
                if len(header) != office_wire.HEADER.size:
                    raise ParseError("Office 转换器未返回完整响应")
                status, length = office_wire.HEADER.unpack(header)
                if length > office_wire.MAX_OUTPUT:
                    raise ParseError("Office 转换结果超过体积上限")
                if status == 2:
                    raise ParseError("Office 转换超时")
                if status == 3:
                    raise ParseError("隔离的 Office 转换器暂不可用")
                if status:
                    raise ParseError("Office 文件损坏、受保护或包含不支持的活动内容")
                data = stream.read(length)
                if len(data) != length:
                    raise ParseError("Office 转换结果不完整")
                return data
    except TimeoutError as exc:
        raise ParseError("Office 转换超时") from exc
    except OSError as exc:
        raise ParseError("无法连接隔离的 Office 转换器") from exc


def available():
    # A conversion can occupy the server while the listening socket still accepts jobs.
    # AttributeError：平台没有 AF_UNIX（如 Windows 宿主）——按"不可用"处理，让调用方
    # 给出可执行提示，而不是把平台差异抛成 500。
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(1.0)
            conn.connect(os.environ["KB_OFFICE_SOCKET"])
        return True
    except (KeyError, OSError, AttributeError):
        return False


def parse(raw, name, *, ocr=None, ocr_pages=200):
    from .office_convert import assert_not_blocked, assert_safe_leaf, assert_valid_ole2_input, assert_within_size

    assert_safe_leaf(name)
    suffix = assert_not_blocked(name)
    assert_within_size(raw)
    assert_valid_ole2_input(raw, suffix)
    data = request(suffix[1:], raw)
    blocks = []
    used_ocr = False
    remaining_ocr = max(0, ocr_pages)
    for row in data.splitlines():
        try:
            page_raw, kind_raw, encoded = row.split(b"\t", 2)
            page = int(page_raw) or None
            kind = kind_raw.decode("ascii")
            if kind not in {"text", "table", "sheet", "image"} or (page is not None and page < 0):
                raise ValueError
            content = base64.b64decode(encoded, validate=True)
            text = "" if kind == "image" else content.decode("utf-8")
        except (ValueError, binascii.Error) as exc:
            raise ParseError("Office 转换器返回了无法读取的分段") from exc
        if kind == "image":
            if not remaining_ocr or ocr is None or not ocr.available:
                continue
            remaining_ocr -= 1
            try:
                text = ocr.recognize_image(content, hint=f"幻灯片 {page}")
            except OcrError as exc:
                raise ParseError(f"幻灯片 OCR 失败：{exc}") from exc
            used_ocr = True
            kind = "image-ocr"
        blocks.extend(_blocks_from_text(normalize_text(text), page=page, kind=kind, start_order=len(blocks)))
        if len(blocks) > MAX_BLOCKS:
            raise ParseError("Office 分段数超过解析上限")
    if not blocks:
        raise ParseError("Office 文件没有可读取的正文")
    return ParseResult(blocks=blocks, used_ocr=used_ocr, parser=f"office-poi-{suffix[1:]}")


def pdf(raw, *, timeout=None):
    options = {} if timeout is None else {"timeout": timeout}
    data = request("pdf", raw, **options)
    if not data.startswith(b"%PDF-"):
        raise ParseError("Office 转换器未返回 PDF 文件")
    return data
