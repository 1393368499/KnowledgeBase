"""旧版 / 二进制 Office 输入的**校验与安全守卫**。

实际转换由**隔离的 office 服务**完成（``deploy/office`` + ``apps.knowledge.office_client``，
通过 Unix socket 通信）；本模块不再探测或拉起任何本地转换进程，只保留
"在把字节交给转换服务之前"必须成立的检查，以及解析层共享的常量与异常。

为什么这些守卫留在应用侧（而不是全交给服务）
--------------------------------------------

============================  ==================================================
检查                          作用
============================  ==================================================
宏 / 脚本型扩展名             ``.docm/.xlsm/.pptm`` 等在**任何解析器或转换服务之前**
                              直接拒绝：那是一份可执行文档，不是一份资料
文件名路径穿越               用**原始名字**做校验（先 basename 会把穿越意图洗掉）
输入体积上限                  超限文件在建立连接前失败，不必等转换服务超时
OLE2 容器结构                 旧版 ``.doc/.xls/.ppt`` 声明的是复合文档，结构不成立
                              时直接判损坏（转换服务会把垃圾当纯文本吞下，产出
                              "成功但乱码"的伪结果）
============================  ==================================================

转换服务自己还会再做一层独立校验（包结构、活动内容、外部链接、XML 实体），
两层检查互不替代：这里挡的是"明显不该送出去"的输入，服务端挡的是"送出去之后
仍然危险"的内容。
"""

from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings

# 解析层的公共异常：office_client 与调用方都按这两个类型捕获。
from .parsers import ParseError, ParseUnsupported

# --------------------------------------------------------------------- 常量 ----

#: **宏 / 脚本型** 扩展名：直接拒绝，绝不送给任何解析器或转换服务。
BLOCKED_SUFFIXES = frozenset(
    {
        ".docm", ".dotm", ".dotx",  # Word 宏 / 模板
        ".xlsm", ".xltm", ".xlam",  # Excel 宏 / 加载项
        ".pptm", ".potm", ".ppam", ".sldm",  # PowerPoint 宏 / 加载项
        ".xla", ".xlt",  # 旧 Excel 加载项 / 模板
        ".vbs", ".vba", ".js", ".jse", ".wsf", ".wsh", ".ps1", ".hta",  # 脚本
    }
)

DEFAULT_MAX_BYTES = 200 * 1024 * 1024
_MB = 1024 * 1024


# --------------------------------------------------------------------- 配置 ----

def _positive_int(name: str, default: int) -> int:
    """读一个正整数设置；配错（0/负数/非数字）时**退回默认值**而不是崩溃。

    解析发生在 worker 里，一条坏配置不应该让整个进程起不来；但也绝不把
    上限当"无上限"用。
    """
    raw = getattr(settings, name, None)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def max_input_bytes() -> int:
    """单份旧版 Office 文件的字节上限。

    默认复用 ``KB_UPLOAD_MAX_BYTES``（上传层已经在挡），因此**不新增设置**也能
    有上限；要更严就用 ``KB_OFFICE_MAX_BYTES`` 覆盖。
    """
    explicit = getattr(settings, "KB_OFFICE_MAX_BYTES", None)
    if explicit is not None:
        return _positive_int("KB_OFFICE_MAX_BYTES", DEFAULT_MAX_BYTES)
    return _positive_int("KB_UPLOAD_MAX_BYTES", DEFAULT_MAX_BYTES)


# --------------------------------------------------------------- 守卫函数 ----


def split_extension(name: str) -> str:
    """取小写扩展名（只看纯文件名部分）。"""
    if not name:
        return ""
    normalized = name.replace("\\", "/")
    return Path(normalized).suffix.lower()


def assert_not_blocked(name: str) -> str:
    """宏 / 脚本型扩展名直接拒绝。返回扩展名，方便调用方记录。"""
    suffix = split_extension(name)
    if suffix in BLOCKED_SUFFIXES:
        raise ParseUnsupported(
            f"拒绝解析 {suffix}：该格式可携带宏/脚本，出于安全考虑不允许转换"
            "（请另存为不含宏的 .docx / .xlsx / .pptx 后重新上传）"
        )
    return suffix


def assert_within_size(raw: bytes) -> None:
    limit = max_input_bytes()
    if len(raw) > limit:
        raise ParseError(
            f"文件 {len(raw)} 字节，超过旧版 Office 转换上限 {limit} 字节"
            f"（{limit // _MB} MB）；请拆分或压缩后重试"
        )


def assert_safe_leaf(name: str) -> str:
    """校验文件名**没有路径穿越意图**；返回纯文件名。

    只在真的可疑时报错（而不是静默清洗），这样"用户传了 ``../../x.doc``"
    在日志里是可见的。注意：**必须用原始名字调这个函数** —— 先用 ``Path().name``
    取 basename 再校验，等于把 ``../../etc/passwd`` 变成了 ``passwd``，
    穿越意图就被洗掉了（这正是本函数存在的意义）。
    """
    if not name or not name.strip():
        raise ParseError("文件名是空的，无法确定要怎样转换")
    candidate = name.strip()
    if "\x00" in candidate:
        raise ParseError("文件名含 NUL 字节，拒绝处理")
    if "/" in candidate or "\\" in candidate:
        raise ParseError(f"文件名 {candidate!r} 含路径分隔符，拒绝处理（疑似路径穿越）")
    if candidate in {".", ".."}:
        raise ParseError(f"文件名 {candidate!r} 不是有效文件，拒绝处理")
    if Path(candidate).is_absolute() or re.match(r"^[A-Za-z]:", candidate):
        raise ParseError(f"文件名 {candidate!r} 是绝对路径，拒绝处理")
    return candidate


# ------------------------------------------------- 输入容器结构（OLE2）校验 ----

#: 旧版 .doc/.xls/.ppt 都是 OLE2 / CFB 复合文档。
_OLE2_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_OLE2_MIN_HEADER = 512


def _invalid_ole2_reason(raw: bytes, suffix: str) -> str | None:
    """旧版 Office 输入必须是**结构上成立**的 OLE2 容器；否则给出拒绝原因。

    只带 OLE2 魔数、后面全是垃圾的 ``.doc`` 会被转换器当纯文本吞下来，
    转出一个"合法 docx + 乱码正文"，于是解析"成功"了 —— 那是伪成功。
    所以在**建立连接之前**先做容器结构检查：魔数、字节序、扇区位移、
    扇区数、目录起始扇区缺一不可。

    说明：这是**结构合理性**检查，不是完整 CFB 解析（不遍历 FAT）。
    真正的解码仍由隔离的 Office 转换服务完成。
    """
    if suffix not in {".doc", ".xls", ".ppt"}:
        return None
    if not raw.startswith(_OLE2_SIGNATURE):
        return (
            f"声明是 {suffix}（OLE2 复合文档），但开头不是 OLE2 魔数"
            f"（实际 {raw[:8].hex(' ')}）"
        )
    if len(raw) < _OLE2_MIN_HEADER:
        return f"声明是 {suffix}，但文件只有 {len(raw)} 字节，连 OLE2 头都不够"
    byte_order = int.from_bytes(raw[28:30], "little")
    if byte_order != 0xFFFE:
        return f"OLE2 头里的字节序标记非法（{byte_order:#06x}，应为 0xfffe）"
    sector_shift = int.from_bytes(raw[30:32], "little")
    if not 7 <= sector_shift <= 20:
        return f"OLE2 头里的扇区位移非法（{sector_shift}，应在 7..20）"
    sector_size = 1 << sector_shift
    if sector_size > len(raw):
        return f"OLE2 扇区大小 {sector_size} 大于整个文件（{len(raw)} 字节）"
    fat_sectors = int.from_bytes(raw[44:48], "little")
    first_dir_sector = int.from_bytes(raw[48:52], "little")
    if fat_sectors == 0 and first_dir_sector == 0xFFFFFFFE:
        return "OLE2 既没有 FAT 也没有目录起始扇区，不是可读的复合文档"
    if first_dir_sector >= 0xFFFFFFFA:
        return f"OLE2 目录起始扇区是保留值（{first_dir_sector:#010x}），容器已损坏"
    return None


def assert_valid_ole2_input(raw: bytes, suffix: str) -> None:
    """旧版 .doc/.xls/.ppt 的容器结构校验；不合法直接 ``ParseError``。"""
    reason = _invalid_ole2_reason(raw, suffix)
    if reason:
        raise ParseError(f"{reason}；判定为损坏文件，拒绝转换")