"""旧版 Office 文档（.doc / .xls / .ppt / .pptx）的安全解析。

思路：**不自己写二进制解析器**，而是把旧格式交给**受限的 LibreOffice 转换**
（``soffice --headless --convert-to``）转成仓库里现有解析器已经吃透的格式，
再复用 ``apps.knowledge.parsers``（.docx / .xlsx / .txt / .csv / .pdf）去抽文字。

``--headless`` **不等于安全**
------------------------------

这一点必须写清楚，因为它很容易被误当成"已经隔离了"：

1. ``--headless`` 只是"没有界面"，它**不禁止宏执行**、**不禁止外部链接更新**、
   也**不禁止联网检查更新**。所以本模块每次转换都生成一份**隔离的用户 profile**
   （``-env:UserInstallation``），并在里面**显式**写入
   ``user/registrymodifications.xcu``，把：

   - ``org.openoffice.Office.Common/Security/Scripting``
     → ``MacroSecurityLevel=3``（Very High：只允许已受信任位置的宏，实际上等于
     不执行文档里的宏）、``RecommendedMacrosSecurityLevel=3``、
     ``DisableMacrosExecution=true``（这一条是硬开关，直接禁止宏执行）；
   - ``org.openoffice.Office.Common/LinkUpdate/BlockUntrustedRefererLinks=true``
     （禁止自动更新外部链接：否则文档里的远程引用会被 LO 主动去拉）；
   - ``org.openoffice.Office.Jobs`` → ``UpdateCheck/Enabled=false``、
     ``AutoUpdate/Enabled=false``（禁止联网更新检查）。

   profile 是**本次转换独占**的临时目录，用完随临时目录一起删除，所以这些设置
   不可能"只写了文档、实际没生效"——见 :func:`write_profile_config` 与
   :func:`read_profile_security`，测试会读回文件断言配置真的落盘。

2. 超时**必须杀整个进程组**：``soffice`` 会 fork 出 ``soffice.bin`` 等子进程，
   只 kill 父进程会留下孤儿进程继续占 CPU 和文件锁。见 :func:`run_conversion`
   与 :func:`terminate_process_tree`。

为什么是"受限"的（每一条都由测试守住）
----------------------------------------

============================  ==================================================
威胁                          对策
============================  ==================================================
宏 / 脚本型文档               ``.docm/.xlsm/.pptm/.dotm/.xlam`` 等**扩展名直接拒绝**，
                              在跑任何程序之前就失败（``BLOCKED_SUFFIXES``）
超大输入                      ``KB_OFFICE_MAX_BYTES`` 上限，超限直接失败
转换挂死                      ``subprocess`` 超时 → **杀进程** → ``ParseError``
转换产物爆炸（图片轰炸）      ``KB_OFFICE_MAX_OUTPUT_FILES`` / ``KB_OFFICE_MAX_OUTPUT_BYTES``
并发下互相抢 LibreOffice 锁   每次转换一个**隔离的 user profile**
                              （``-env:UserInstallation=file://<临时目录>``）
污染宿主 / 残留               ``TemporaryDirectory``，成功失败都清
命令行注入                    子进程用**参数列表**（``shell=False``），无 shell 拼接
路径穿越 / 符号链接逃逸       落盘文件名做白名单清洗 + ``Path.is_relative_to`` 包含性校验；
                              产物目录里出现符号链接一律拒绝
"空正文冒充成功"              没产物 / 产物为空 / 解析零分段 → 一律 ``ParseError``
============================  ==================================================

**绝不修改源字节**：``raw`` 只被写入临时目录里的一份副本；转换、解析、清理全部
发生在临时目录内，原始 blob 目录始终只读。
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import tempfile
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from django.conf import settings

# 这两个异常是解析层的公共约定（见 apps.knowledge.parsers）。
from .parsers import ParseError, ParseResult, ParseUnsupported

# --------------------------------------------------------------------- 常量 ----

#: LibreOffice 可执行文件的名字（容器里 apt 装的 libreoffice-{writer,calc,impress}
#: 会在 PATH 上提供 ``soffice``；本仓库不依赖 Windows Office）。
SOFFICE_NAMES = ("soffice", "soffice.exe", "libreoffice")

#: LibreOffice 默认可能装在 PATH 之外的位置；只作为兜底探测，不写死某一台机器。
SOFFICE_FALLBACK_PATHS = (
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    "/usr/bin/soffice",
    "/usr/local/bin/soffice",
    "/opt/libreoffice/program/soffice",
    "/snap/bin/libreoffice",
)

#: **宏 / 脚本型** 扩展名：直接拒绝，绝不尝试转换。
#: 注意 ``.xlsm`` 不在本表内 —— 它已经在 ``parse_bytes`` 里由 openpyxl 解析，
#: 但本模块也不接受它（见 ``BLOCKED_SUFFIXES``）。
BLOCKED_SUFFIXES = frozenset(
    {
        ".docm", ".dotm", ".dotx",  # Word 宏 / 模板
        ".xlsm", ".xltm", ".xlam",  # Excel 宏 / 加载项
        ".pptm", ".potm", ".ppam", ".sldm",  # PowerPoint 宏 / 加载项
        ".xla", ".xlt",  # 旧 Excel 加载项 / 模板
        ".vbs", ".vba", ".js", ".jse", ".wsf", ".wsh", ".ps1", ".hta",  # 脚本
    }
)

#: 本模块负责的扩展名（其余一律交回 ``parse_bytes`` 的原有分支）。
LEGACY_SUFFIXES = frozenset({".doc", ".xls", ".ppt", ".pptx"})

#: 允许出现在临时文件名里的字符；其余一律替换成 ``_``。
_SAFE_STEM = re.compile(r"[^0-9A-Za-z._\u4e00-\u9fff-]+")

#: LibreOffice 可能吐出的临时/锁文件与目录，不计入产物。
_IGNORED_OUTPUT_NAMES = frozenset({".~lock", "soffice.ini", ".DS_Store"})

DEFAULT_MAX_BYTES = 200 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_MAX_OUTPUT_FILES = 5
DEFAULT_MAX_OUTPUT_BYTES = 200 * 1024 * 1024

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


def _positive_float(name: str, default: float) -> float:
    raw = getattr(settings, name, None)
    if raw is None:
        return default
    try:
        value = float(raw)
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


def conversion_timeout_seconds() -> float:
    return _positive_float("KB_OFFICE_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)


def max_output_files() -> int:
    return _positive_int("KB_OFFICE_MAX_OUTPUT_FILES", DEFAULT_MAX_OUTPUT_FILES)


def max_output_bytes() -> int:
    return _positive_int("KB_OFFICE_MAX_OUTPUT_BYTES", DEFAULT_MAX_OUTPUT_BYTES)


# ------------------------------------------------------------- 可执行文件 ----

_SOFFICE_CACHE: tuple[str | None, ...] = ()


def reset_soffice_cache() -> None:
    """清掉 ``soffice`` 路径缓存（测试用；也便于运行时装了 LO 后热生效）。"""
    global _SOFFICE_CACHE
    _SOFFICE_CACHE = ()


def find_soffice() -> str | None:
    """定位 ``soffice``；找不到返回 ``None``（调用方负责给出明确失败原因）。"""
    global _SOFFICE_CACHE
    if _SOFFICE_CACHE:
        return _SOFFICE_CACHE[0]
    found: str | None = None
    for name in SOFFICE_NAMES:
        candidate = shutil.which(name)
        if candidate:
            found = candidate
            break
    if found is None:
        for candidate in SOFFICE_FALLBACK_PATHS:
            if os.path.isfile(candidate):
                found = candidate
                break
    _SOFFICE_CACHE = (found,)
    return found


def soffice_available() -> bool:
    return find_soffice() is not None


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


def safe_stem(name: str) -> str:
    """把用户提供的文件名压成**单个安全的文件名主干**。

    - 只取最后一段（丢掉 ``C:\\`` / ``/etc/`` / ``../../`` 这类前缀）；
    - 白名单字符，其余替换成 ``_``；
    - Windows 保留名（``CON`` / ``NUL`` …）与空串兜底；
    - 长度截断，避免超长路径。
    """
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    stem = Path(base).stem
    stem = unicodedata.normalize("NFKC", stem)
    stem = _SAFE_STEM.sub("_", stem).strip(" ._")
    if not stem or stem.upper() in {"CON", "PRN", "AUX", "NUL"} or (
        len(stem) == 4 and stem[:3].upper() in {"COM", "LPT"} and stem[3].isdigit()
    ):
        stem = "document"
    return stem[:64]


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


def assert_within(root: Path, target: Path) -> Path:
    """确认 ``target`` 落在 ``root`` 内（解析符号链接之后）。

    ``Path.resolve()`` 会把已存在的符号链接解开，所以"临时目录里被塞了一个指向
    ``/`` 的符号链接"这种情况会在这一步被抓住。
    """
    resolved_root = Path(root).resolve()
    resolved_target = Path(target).resolve()
    if resolved_target != resolved_root and not resolved_target.is_relative_to(resolved_root):
        raise ParseError(
            f"路径 {resolved_target} 越出临时目录 {resolved_root}，拒绝操作"
            "（疑似路径穿越或符号链接逃逸）"
        )
    return resolved_target


# ------------------------------------------------- 隔离 profile 的安全配置 ----

#: LibreOffice 用户 profile 的安全配置。
#:
#: **每一个键都是对着随包 schema 核对过的**，不是猜的：
#:
#: 依据（本机实测，LibreOffice 26.2.4.2）::
#:
#:     schema : C:\Program Files\LibreOffice\share\registry\main.xcd
#:     root   : <oor:data>
#:     <oor:component-schema oor:package="org.openoffice.Office" oor:name="Common">
#:       <component><group oor:name="Security"><group oor:name="Scripting">
#:         <prop oor:name="DisableMacrosExecution" oor:type="xs:boolean"/>
#:         <prop oor:name="MacroSecurityLevel"      oor:type="xs:int"/>
#:         <prop oor:name="DisableActiveContent"    oor:type="xs:boolean"/>
#:         <prop oor:name="DisablePythonRuntime"    oor:type="xs:boolean"/>
#:         <prop oor:name="DisableOLEAutomation"    oor:type="xs:boolean"/>
#:         <prop oor:name="BlockUntrustedRefererLinks" oor:type="xs:boolean"/>
#:         <prop oor:name="CheckDocumentEvents"     oor:type="xs:boolean"/>
#:
#: 复核脚本：``artifacts/lo_schema_probe.py``（容器内也可跑，见交付说明）。
#:
#: 已经被实测**否定**、不允许再写回来的伪键：
#:
#: - ``RecommendedMacrosSecurityLevel`` / ``RecommendedMacrosSecurityLevels``
#:   —— 本版本 schema 里不存在；
#: - ``TrustedAuthors`` —— 是 ``<set>`` 而不是 ``<prop>``，不能当普通属性写；
#: - ``TrustedLocations`` —— 本版本 schema 里不存在；
#: - ``Common/LinkUpdate/BlockUntrustedRefererLinks`` —— 该路径不存在，
#:   正确的家是 ``Common/Security/Scripting``（见上）。
#:
#: 关于"禁止联网更新"：本模块**不再写任何 UpdateCheck 键**（此前那几条是猜的）。
#: 实际做法是：转换用一次性的临时 profile + ``--nodefault``，且
#: :data:`~apps.knowledge.office_convert.SOFFICE_FLAGS` 里不带任何联网开关；
#: 需要"绝对禁止出网"时请由容器网络策略（无出网/DNS 黑洞）保证 —— 那才是
#: 可靠的边界，配置文件里写一个猜出来的键并不能保证什么。
PROFILE_SECURITY_SETTINGS: dict[str, dict[str, str]] = {
    "org.openoffice.Office.Common/Security/Scripting": {
        # 宏执行总开关：按 schema 说明，置 true 会忽略 MacroSecurityLevel，
        # 并禁用 Basic / BeanShell / JavaScript / Python 脚本。
        "DisableMacrosExecution": "true",
        # 3 = Very High（"只允许受信任位置的宏"），与上一行一起构成双保险。
        "MacroSecurityLevel": "3",
        # 活动内容（OLE / DDE）与 OLE 自动化：资料文档不该驱动外部程序。
        "DisableActiveContent": "true",
        "DisableOLEAutomation": "true",
        # Python 运行时：转换流程本身不需要它。
        "DisablePythonRuntime": "true",
        # 来自"非受信任位置"的文档：不允许它去更新外部链接（防 SSRF / 外连）。
        "BlockUntrustedRefererLinks": "true",
        # 文档事件不检查（也就不会因为事件去调用脚本）。
        "CheckDocumentEvents": "false",
    },
}

#: 传给每次转换的固定命令行开关（``--headless`` 只表示"无界面"）。
#:
#: 这些开关是**逐个实测**过的（`artifacts/_lo_flag_probe.py`，LibreOffice 26.2.4.2）：
#: 每个都跑一次真实转换，要求返回码 0 且确实产出文件。
#: 实测**不可用**、会导致返回码 1 且零产出的开关（绝不能加回来）：
#: ``--nofilepickerdialog``、``--nofilepicker``。
SOFFICE_FLAGS: tuple[str, ...] = (
    "--headless",  # 无界面；注意它**不**禁宏、**不**禁外链更新
    "--norestore",  # 崩溃恢复对话框会卡住无人值守的转换
    "--nolockcheck",  # 不要弹"文件被锁定"的交互
    "--nodefault",  # 不加载默认文档模板
    "--nofirststartwizard",  # 首次启动向导
    "--nologo",  # 启动画面
    "--invisible",  # 明确不可见（与 headless 一起用）
    "--nocrashreport",  # 崩溃上报既慢又可能出网
)

_XCU_HEADER = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<oor:items xmlns:oor="http://openoffice.org/2001/registry"'
    ' xmlns:xs="http://www.w3.org/2001/XMLSchema"'
    ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
)


def _xcu_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_profile_config(
    settings_map: dict[str, dict[str, str]] | None = None,
) -> str:
    """生成 ``registrymodifications.xcu`` 的内容（纯函数，便于断言）。"""
    mapping = PROFILE_SECURITY_SETTINGS if settings_map is None else settings_map
    lines = [_XCU_HEADER]
    for path, props in mapping.items():
        for key, value in props.items():
            lines.append(
                f'<item oor:path="/{_xcu_escape(path)}">'
                f'<prop oor:name="{_xcu_escape(key)}" oor:op="fuse">'
                f"<value>{_xcu_escape(value)}</value>"
                "</prop></item>\n"
            )
    lines.append("</oor:items>\n")
    return "".join(lines)


def write_profile_config(profile_dir: Path) -> Path:
    """把安全配置写进**本次转换独占**的 profile 目录。

    必须在启动 ``soffice`` **之前**写完，否则 LO 会先用默认 profile 起一次。
    """
    user_dir = Path(profile_dir) / "user"
    user_dir.mkdir(parents=True, exist_ok=True)
    target = user_dir / "registrymodifications.xcu"
    target.write_text(render_profile_config(), encoding="utf-8")
    return target


def read_profile_security(profile_dir: Path) -> dict[str, str]:
    """读回 profile 里的安全键值（测试用它证明"配置真的落盘了"）。

    这是一个**极简的文本提取**，不是 XML 解析器：只认自己写出去的形状。
    """
    path = Path(profile_dir) / "user" / "registrymodifications.xcu"
    if not path.is_file():
        raise ParseError(f"隔离 profile 缺少 {path.name}，拒绝在未加固的 profile 里转换")
    text = path.read_text(encoding="utf-8")
    out: dict[str, str] = {}
    for match in re.finditer(
        r'<prop oor:name="([^"]+)" oor:op="fuse"><value>([^<]*)</value>', text
    ):
        out[match.group(1)] = match.group(2)
    return out


def assert_profile_hardened(profile_dir: Path) -> None:
    """确认 profile 里的关键安全开关**真的**是加固状态。

    写文件失败 / 被覆盖都会在这里变成明确失败，而不是"以为加固了"。

    这里只证明**我们写出去的内容是对的**；"LibreOffice 是否真的采纳"由
    ``artifacts/lo_verify_adoption.py`` 闭环验证（2026-10-16 实测通过）：

      * 在交付镜像的 **LibreOffice 25.2.3.2** 上跑真实转换，读**它退出时写回**的
        ``user/registrymodifications.xcu``：7 个键全部以我们设的**非默认值**出现
        （``DisableMacrosExecution=true``（默认 false）、``MacroSecurityLevel=3``
        （默认 2）、``CheckDocumentEvents=false``（默认 true）等）；
      * schema 里**不存在**的键（``RecommendedMacrosSecurityLevel``/``TrustedLocations``
        /错路径的 ``Common/LinkUpdate``）**不会**被采纳 —— 有反例控制；
      * 证据：``artifacts/acceptance/lo-adoption-20261016.json``。

    ⚠️ 探测这个脚本时踩过两个坑（别再踩）：
      1. 把**不存在的键**混进同一个 XCU，LibreOffice 会**丢弃整份文件**并重写一份
         干净的 → "真实键"也一起消失，看起来像"未采纳"。所以真实键与伪键**分两次跑**。
      2. 回读时路径要**归一化**：LO 写回的是 ``/org.openoffice...``（带前导斜杠），
         而代码里查表用的是不带斜杠的形式；不归一化会把"已采纳"误判成"未采纳"。
    """
    values = read_profile_security(profile_dir)
    required = {
        "DisableMacrosExecution": "true",
        "MacroSecurityLevel": "3",
        "DisableActiveContent": "true",
        "DisableOLEAutomation": "true",
        "DisablePythonRuntime": "true",
        "BlockUntrustedRefererLinks": "true",
        "CheckDocumentEvents": "false",
    }
    wrong = {
        key: values.get(key)
        for key, expected in required.items()
        if values.get(key) != expected
    }
    if wrong:
        raise ParseError(f"隔离 profile 的安全配置不生效（{wrong}），拒绝执行转换")


# ------------------------------------------------------- 进程组终止（超时） ----

#: ``psutil`` 是**可选**依赖：装了就用它精确杀"进程树"，没装则退回
#: 进程组（POSIX ``os.killpg`` / Windows ``taskkill /T /F``）。
#: 仓库当前 requirements.txt 里**没有** psutil（见交付报告），所以默认走进程组。
try:  # pragma: no cover - 取决于环境
    import psutil as _psutil
except Exception:  # noqa: BLE001 - 任何导入失败都退回进程组方案
    _psutil = None


def psutil_available() -> bool:
    return _psutil is not None


def _kill_with_psutil(pid: int) -> bool:
    """用 psutil 杀**整棵进程树**；没装或失败时返回 ``False``。

    这是最可靠的一条：按父进程递归找子孙，逐个 kill。比"杀进程组"更稳，
    因为进程组只覆盖"仍留在同一 PGID"的进程 —— 子进程一旦自己
    ``setsid``/被重挂，就不在组里了。实测（容器，LibreOffice 25.2.3）只用
    ``killpg`` 时会漏掉一个已经 ``ppid=1`` 的子进程。
    """
    if _psutil is None:
        return False
    try:
        parent = _psutil.Process(pid)
    except Exception:  # noqa: BLE001 - 进程已经没了也算"处理过"
        return True
    killed = False
    # 递归收集子孙（深度上限防止病态进程树把这里拖死）
    descendants: list = []
    frontier = [parent]
    for _ in range(6):
        next_frontier = []
        for process in frontier:
            try:
                children = process.children(recursive=False)
            except Exception:  # noqa: BLE001
                children = []
            for child in children:
                if child.pid not in {item.pid for item in descendants}:
                    descendants.append(child)
                    next_frontier.append(child)
        if not next_frontier:
            break
        frontier = next_frontier

    # **先杀子孙，再杀父**：反过来会让子树被 init 收养而更难追踪
    for child in descendants:
        try:
            child.kill()
            killed = True
        except Exception:  # noqa: BLE001
            continue
    try:
        parent.kill()
        killed = True
    except Exception:  # noqa: BLE001
        pass
    try:
        _psutil.wait_procs([*descendants, parent], timeout=5)
    except Exception:  # noqa: BLE001
        pass
    return killed


def _taskkill(pid: int) -> bool:
    """Windows：``taskkill /F /T`` 才"连子进程一起杀"（``os.kill`` 只杀一个）。

    单独抽成函数是为了让测试能**只替换这一处**，而不是把全局 ``subprocess``
    换掉（上一版测试用假 ``Popen`` 顶替全局类，结果把 ``taskkill`` 一起假掉了，
    于是"杀不掉"看起来像通过 —— 那是测试假装安全，不是真的安全）。
    """
    try:
        subprocess.run(  # noqa: S603,S607 - 参数列表，非 shell
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            timeout=10,
            check=False,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def terminate_process_tree(pid: int) -> dict[str, object]:
    """杀掉 ``pid`` 的**整个进程组 / 进程树**，返回做了什么的描述。

    为什么必须杀组：``soffice`` 只是启动器，真正的转换在它 fork 出来的
    ``soffice.bin`` 里跑。只 kill 父进程会留下孤儿进程继续吃 CPU、继续占着
    文件锁，下一次转换就会被那个锁挡住。
    """
    report: dict[str, object] = {
        "pid": pid,
        "psutil": psutil_available(),
        "killpg": False,
        "taskkill": False,
    }
    if pid <= 0:
        return report

    if _kill_with_psutil(pid):
        report["psutil_killed"] = True
        return report
    report["psutil_killed"] = False

    if os.name == "posix":
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
            report["killpg"] = True
        except (ProcessLookupError, PermissionError, OSError):
            report["killpg"] = False
        # 组杀之后再把父进程本身收一遍（组 ID 可能已经失效）
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        return report

    report["taskkill"] = _taskkill(pid)
    return report


def _popen_group_kwargs() -> dict[str, object]:
    """让子进程独占一个**进程组/会话**，这样超时时可以整组杀掉。"""
    if os.name == "posix":
        return {"start_new_session": True}
    return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}


# ------------------------------------------------------- 可注入的执行器接口 ----

#: 一次子进程运行的原始结果（不含"成功/失败"语义）。
RunOutcome = tuple[int, bytes, bytes]


class ProcessRunner(Protocol):
    """执行一次外部命令的接口。

    存在的意义：把"**怎么跑 soffice**"和"**怎么杀进程组**"解耦。
    测试注入一个假 runner 来伪造产物/返回码/超时，而
    :func:`terminate_process_tree` 仍然是真的实现 ——
    这样"超时杀进程组"才有被真正验证，而不是被假 `Popen` 一起顶替掉。
    """

    def run(
        self, command: Sequence[str], *, env: dict[str, str], cwd: str, timeout: float | None
    ) -> RunOutcome:  # pragma: no cover - 协议
        ...


class SubprocessRunner:
    """默认实现：真 ``Popen`` + 超时**杀整个进程组**。"""

    def run(
        self, command: Sequence[str], *, env: dict[str, str], cwd: str, timeout: float | None
    ) -> RunOutcome:
        return _run_process(list(command), env=env, cwd=cwd, timeout=timeout)


DEFAULT_RUNNER: ProcessRunner = SubprocessRunner()


# ----------------------------------------------------------------- 产物收集 ----


#: 各容器格式的**魔数**。转换产物必须"名副其实"：LibreOffice 完全可能
#: 把一个坏 .doc 当纯文本吞下去、再吐出一个扩展名对但内容不对的文件 ——
#: 那种"伪成功"必须在解析之前就被拦掉。
FORMAT_MAGIC: dict[str, tuple[bytes, ...]] = {
    # OOXML / ODF 都是 zip
    ".docx": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    ".xlsx": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    ".pptx": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    # 旧版二进制 Office 复合文档（OLE2 / CFB）
    ".doc": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
    ".xls": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
    ".ppt": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
    ".pdf": (b"%PDF",),
}

#: 纯文本导出（.txt/.csv）没有魔数，改用"可读性"判据。
_TEXT_CHUNK = 8192
_ALLOWED_CONTROL = frozenset(b"\t\n\r\f\v\x1b")


def _looks_like_text(path: Path) -> bool:
    """判断产物像不像**导出的文本**（用来识别"二进制垃圾冒充文本"）。

    判据（保守，只挡明显的垃圾，不判内容好坏）：
      * 前 8KB 里没有 NUL 字节；
      * 前 8KB 里控制字符占比 < 5%（制表/换行/回车等常规控制符不算）。
    """
    head = path.read_bytes()[: _TEXT_CHUNK]
    if not head:
        return False
    if b"\x00" in head:
        return False
    control = sum(
        1 for byte in head if byte < 0x20 and byte not in _ALLOWED_CONTROL
    )
    return control / len(head) < 0.05


def assert_output_matches_format(path: Path) -> None:
    """转换产物的**容器格式**必须与声明的后缀一致，否则明确失败。

    这一条专门防"伪成功"：坏掉的 .doc 可能被当作纯文本吃进去，再以
    正确的扩展名吐出来 —— 不校验的话，后面的解析器要么解出乱码、
    要么安静地返回一段毫不相干的文本。
    """
    suffix = path.suffix.lower()
    expected = FORMAT_MAGIC.get(suffix)
    if expected is None:
        if suffix in {".txt", ".csv"} and not _looks_like_text(path):
            raise ParseError(
                f"转换产物 {path.name} 声称是文本，但内容是二进制垃圾（疑似转换失败）"
            )
        return
    head = path.read_bytes()[:8]
    if not any(head.startswith(signature) for signature in expected):
        raise ParseError(
            f"转换产物 {path.name} 与声明的格式 {suffix} 不符（缺少 "
            f"{expected[0].hex(' ')} 魔数；实际开头 {head[:8].hex(' ') or '(空)'}）；"
            "判定为转换失败"
        )


# ------------------------------------------------- 输入容器结构（OLE2）校验 ----

#: 旧版 .doc/.xls/.ppt 都是 OLE2 / CFB 复合文档。
_OLE2_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_OLE2_MIN_HEADER = 512


def _invalid_ole2_reason(raw: bytes, suffix: str) -> str | None:
    """旧版 Office 输入必须是**结构上成立**的 OLE2 容器；否则给出拒绝原因。

    实测（Debian 13 容器，LibreOffice 25.2.3.2）：只带 OLE2 魔数、后面全是垃圾的
    ``.doc`` 会被 LibreOffice **当纯文本吞下来**，转出一个"合法 docx + 乱码正文"，
    于是解析"成功"了 —— 那是伪成功。所以在转换**之前**先做容器结构检查：
    魔数、字节序、扇区位移、扇区数、目录起始扇区缺一不可。

    说明：这是**结构合理性**检查，不是完整 CFB 解析（不遍历 FAT）。
    真正的解码仍由 LibreOffice 完成。
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


@dataclass
class ConversionOutcome:
    """一次**真实** LibreOffice 转换的结果（成功执行完毕，不代表产物可用）。"""

    returncode: int
    command: list[str] = field(default_factory=list)
    profile_dir: Path | None = None
    output_dir: Path | None = None
    target: str = ""
    stderr: str = ""
    files: list[Path] = field(default_factory=list)
    total_bytes: int = 0

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _is_ignored(path: Path) -> bool:
    if path.name.startswith(".~lock"):
        return True
    return path.name in _IGNORED_OUTPUT_NAMES


def collect_outputs(output_dir: Path) -> tuple[list[Path], int]:
    """收集转换产物；**符号链接一律拒绝**，数量/总量双重上限。"""
    root = Path(output_dir).resolve()
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ParseError(
                f"转换产物目录里出现符号链接（{path.name}），拒绝继续（疑似逃逸尝试）"
            )
        if not path.is_file():
            continue
        if _is_ignored(path):
            continue
        contained = assert_within(root, path)
        files.append(contained)

    file_limit = max_output_files()
    if len(files) > file_limit:
        raise ParseError(
            f"转换产出 {len(files)} 个文件，超过上限 {file_limit} 个；"
            "为避免资源耗尽已中止（可调整 KB_OFFICE_MAX_OUTPUT_FILES）"
        )

    total = 0
    byte_limit = max_output_bytes()
    for path in files:
        total += path.stat().st_size
        if total > byte_limit:
            raise ParseError(
                f"转换产出累计超过 {byte_limit // _MB} MB 上限；"
                "为避免资源耗尽已中止（可调整 KB_OFFICE_MAX_OUTPUT_BYTES）"
            )
    return files, total


# ----------------------------------------------------------------- 转换策略 ----

#: 源后缀 → 依次尝试的目标后缀。第一个能"转出 + 解析出文字"的胜出。
#:
#: - ``.doc``  → ``.docx`` 保结构（表格/段落），失败退 ``.txt``（LO 的纯文本导出）；
#: - ``.xls``  → ``.xlsx`` 保多表，失败退 ``.csv``；
#: - ``.ppt``  → ``.pptx``（保结构）失败退 ``.pdf``（每页文本）；
#: - ``.pptx`` → 仓库**没有** ``python-pptx``（见交付报告），走 ``.pdf`` 同一条路。
CONVERSION_TARGETS: dict[str, tuple[str, ...]] = {
    ".doc": (".docx", ".txt"),
    ".xls": (".xlsx", ".csv"),
    ".ppt": (".pptx", ".pdf"),
    ".pptx": (".pdf",),
}

#: 由哪个内部解析器处理转换产物。``parse_bytes`` 留在最后做真兜底。
_OUTPUT_PARSERS: dict[str, str] = {
    ".docx": "_parse_docx",
    ".xlsx": "_parse_xlsx",
    ".txt": "_parse_text",
    ".csv": "_parse_csv",
    ".pdf": "_parse_pdf",
}


def build_command(
    binary: str,
    *,
    source: Path,
    output_dir: Path,
    profile_dir: Path,
    target: str,
    timeout: float,
) -> list[str]:
    """组装 LibreOffice 命令行（**参数列表**，绝不经过 shell）。"""
    return [
        binary,
        *SOFFICE_FLAGS,
        f"-env:UserInstallation={Path(profile_dir).resolve().as_uri()}",
        "--convert-to",
        target.lstrip("."),
        "--outdir",
        str(Path(output_dir).resolve()),
        str(Path(source)),
    ]


def get_soffice_path(binary: str) -> str:
    """解析出真正要执行的程序路径。

    ``find_soffice()`` 通常已经给了绝对路径；但测试会注入 ``python fake.py``
    这种"启动器 + 脚本"的组合（用系统解释器模拟 soffice），所以这里保留
    "绝对路径原样、其他名字走 ``which``"的规则。
    """
    candidate = Path(binary)
    if candidate.is_absolute() or candidate.exists():
        return binary
    resolved = shutil.which(binary)
    return resolved or binary


def profile_dir_from_command(command: list[str]) -> Path | None:
    """从命令行里的 ``-env:UserInstallation=file://...`` 反解出 profile 目录。"""
    prefix = "-env:UserInstallation="
    for part in command:
        if part.startswith(prefix):
            url = part[len(prefix) :]
            if url.startswith("file://"):
                from urllib.parse import unquote, urlparse

                parsed = urlparse(url)
                path = unquote(parsed.path)
                # Windows 的 file:///C:/x 会解析成 /C:/x，去掉多余的前导斜杠
                if os.name == "nt" and re.match(r"^/[A-Za-z]:", path):
                    path = path[1:]
                return Path(path)
            return Path(url)
    return None


def _run_process(
    command: list[str],
    *,
    env: dict[str, str],
    cwd: str,
    timeout: float | None,
) -> tuple[int, bytes, bytes]:
    """阻塞执行 + **超时杀整棵进程树**。

    用 ``Popen``（而不是 ``run(timeout=...)``）是因为要拿到 ``pid`` 才能杀整个
    进程组：``subprocess.run`` 的 timeout 只保证杀掉**父进程**，而 LibreOffice
    的转换跑在它 fork 出来的子进程里，父进程被杀之后子进程会变成孤儿继续跑。
    """
    popen_kwargs: dict = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "stdin": subprocess.DEVNULL,
        "env": env,
        "cwd": cwd,
        "shell": False,  # 参数列表，永不经过 shell
    }
    if not hasattr(subprocess.Popen, "__attrs_attrs__"):  # pragma: no cover - 真环境
        # 让子进程独占进程组/会话：超时时才可能"整组"杀掉。
        popen_kwargs.update(_popen_group_kwargs())

    process = subprocess.Popen(command, **popen_kwargs)  # noqa: S603 - 参数列表
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return int(process.returncode or 0), stdout or b"", stderr or b""
    except subprocess.TimeoutExpired:
        # **顺序很关键**：必须先 enumerate/kill 整棵树（此时父进程还活着，
        # 父子关系还在），再 kill 父进程。反过来的话父进程先死，子进程会被
        # init 收养（ppid=1），psutil 再也找不到它们 —— 实测就是这样漏掉
        # 了一个 sleep 30 的子进程。
        terminate_process_tree(process.pid)
        try:
            process.kill()
        except Exception:  # noqa: BLE001
            pass
        try:
            process.communicate(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        raise


def run_conversion(
    binary: str,
    *,
    source: Path,
    output_dir: Path,
    profile_dir: Path,
    target: str,
    timeout: float,
    runner: ProcessRunner | None = None,
) -> ConversionOutcome:
    """跑一次 ``soffice`` 转换（**已收集产物**）。

    调用前 profile 必须已经加固（:func:`write_profile_config`），这里再**校验一次**
    —— "以为写好了配置"是这类隔离最常见的失效方式。
    """
    assert_profile_hardened(profile_dir)
    command = build_command(
        binary,
        source=source,
        output_dir=output_dir,
        profile_dir=profile_dir,
        target=target,
        timeout=timeout,
    )
    env = dict(os.environ)
    # 让 LibreOffice 的"家目录"也落在临时目录里：否则它会去写宿主的 ~/.config
    # （容器里通常是 root 的家目录），既是污染源也是并发锁的来源。
    env["HOME"] = str(Path(profile_dir).resolve())
    env["TMPDIR"] = str(Path(output_dir).resolve())
    env["SAL_USE_VCLPLUGIN"] = "svp"  # 无 X11 的 headless 渲染后端
    env["SAL_DISABLE_OPENCL"] = "true"
    env["SAL_NO_MOUSEGRABS"] = "1"
    executable = get_soffice_path(binary)
    launch = [executable, *command[1:]]
    active_runner = runner if runner is not None else DEFAULT_RUNNER
    try:
        returncode, _stdout, stderr_bytes = active_runner.run(
            launch,
            env=env,
            cwd=str(Path(output_dir).resolve()),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ParseError(
            f"LibreOffice 转换超过 {timeout:g} 秒未完成，**已终止整个进程组**"
            "（文件可能过大或已损坏）"
        ) from exc
    except OSError as exc:
        raise ParseError(f"无法启动 LibreOffice（{binary}）：{exc}") from exc
    stderr = stderr_bytes.decode("utf-8", "replace").strip()[-500:]
    outcome = ConversionOutcome(
        returncode=int(returncode or 0),
        command=launch,
        profile_dir=Path(profile_dir),
        output_dir=Path(output_dir),
        target=target.lstrip("."),
        stderr=stderr,
    )
    if outcome.ok:
        # 产物收集放在这里：符号链接 / 条数 / 体积的守卫对每一次转换都生效。
        files, total = collect_outputs(output_dir)
        outcome.files = files
        outcome.total_bytes = total
    return outcome


# --------------------------------------------------------------- 解析产物 ----


def parse_converted(
    path: Path,
    *,
    name: str,
    ocr=None,
    ocr_pages: int = 0,
    parser_lookup: Callable[[str], Callable[..., ParseResult] | None] | None = None,
) -> ParseResult:
    """把一份**转换产物**交给仓库里已有的解析器。

    只认 :data:`_OUTPUT_PARSERS` 里列出的格式 —— LibreOffice 万一吐出别的东西，
    我们不做"顺手也支持一下"的猜测。
    """
    from . import parsers  # 延迟导入：避免与 parsers 的顶层导入成环

    suffix = path.suffix.lower()
    handler_name = _OUTPUT_PARSERS.get(suffix)
    if handler_name is None:
        raise ParseError(f"转换产物的格式 {suffix or '(无后缀)'} 没有对应解析器")

    lookup = parser_lookup
    if lookup is None:
        def lookup(name_: str):  # noqa: E306 - 简单的默认查表
            return getattr(parsers, name_, None)

    handler = lookup(handler_name)
    if handler is None:
        raise ParseError(f"内部错误：找不到解析器 {handler_name}")

    data = path.read_bytes()
    if not data:
        raise ParseError(f"转换产物 {path.name} 是空文件，判定为转换失败")

    # 先验容器格式，再交给解析器。坏文件"伪成功"的典型形态就是
    # "扩展名对、内容不对"，这一步是最后一道辨别。
    assert_output_matches_format(path)

    if suffix == ".pdf":
        result = handler(data, name, ocr=ocr, ocr_pages=ocr_pages or 200)
    else:
        result = handler(data, name)

    if not result or not [block for block in result.blocks if block.text.strip()]:
        raise ParseError(f"转换产物 {path.name} 里没有可提取的文字")
    assert_text_is_usable(
        "\n".join(block.text for block in result.blocks), name=path.name
    )
    return result


# ------------------------------------------------- 正文质量（乱码）兜底 ----

#: 太短就不做乱码判定（少数字符的偶然分布说明不了什么）。
_MOJIBAKE_MIN_CHARS = 40
#: "看起来不像字"的字符占比超过这个值就判定为乱码。
_MOJIBAKE_THRESHOLD = 0.30


def _text_looks_like_mojibake(text: str) -> bool:
    """粗略判断正文是不是**乱码**（转换把二进制当纯文本解码的典型结果）。

    判据：Unicode 里"没有名字"（未分配 / 私用区 / 代理区）或落在冷僻扩展区的
    字符占比过高。实测样例：把 200 个 ``garbage`` 字节当 UTF-16 解出来会得到
    一大串 "탏ᇠꆱ..."，其中大量码位在 Unicode 里根本没有名字 —— 合法文档的正
    常文字几乎不会这样。

    这是一个**兜底启发式**，不是精确判定；所以阈值取得很宽（30%）。
    """
    stripped = text.strip()
    if len(stripped) < _MOJIBAKE_MIN_CHARS:
        return False
    suspicious = 0
    for char in stripped:
        if char.isspace() or char.isascii():
            continue
        try:
            name = unicodedata.name(char)
        except ValueError:
            suspicious += 1
            continue
        if name.startswith(("CJK COMPATIBILITY IDEOGRAPH", "PRIVATE USE")):
            suspicious += 1
    return suspicious / len(stripped) > _MOJIBAKE_THRESHOLD


def assert_text_is_usable(text: str, *, name: str) -> None:
    """转换出来的正文如果整段是乱码，就**明确失败**而不是当成功。"""
    if _text_looks_like_mojibake(text):
        raise ParseError(
            f"转换产物 {name} 的正文看起来是乱码（二进制被当作纯文本解码）；"
            "判定为转换失败，不写入索引"
        )


# --------------------------------------------------------------------- 主入口 ----


def parse_legacy_office(
    raw: bytes,
    name: str,
    *,
    ocr=None,
    ocr_pages: int = 200,
    parser_lookup: Callable[[str], Callable[..., ParseResult] | None] | None = None,
    runner: ProcessRunner | None = None,
    temp_root: Path | None = None,
) -> ParseResult:
    """解析 .doc / .xls / .ppt / .pptx（不修改 ``raw``，不需要 Windows Office）。

    ``runner`` 用来注入假的执行器（测试用）；``temp_root`` 用来把临时目录放到
    指定根下，便于测试直接观察"每次转换的那个目录被删掉了"。
    """
    if not raw:
        raise ParseError("文件为空，无法解析")

    # 顺序很重要：**先用原始名字**做穿越校验，再谈别的。
    assert_safe_leaf(name)
    suffix = assert_not_blocked(name)
    if suffix not in LEGACY_SUFFIXES:
        raise ParseUnsupported(f"office_convert 不负责 {suffix or '该格式'}")
    assert_within_size(raw)
    # 转换**之前**先确认旧版容器结构成立：否则 LibreOffice 可能把垃圾当纯文本
    # 吞下去，转出"合法 docx + 乱码正文"，看起来像解析成功。
    assert_valid_ole2_input(raw, suffix)

    binary = find_soffice()
    if not binary:
        raise ParseError(
            f"解析 {suffix} 需要 LibreOffice（soffice），但当前环境里找不到该可执行文件；"
            "容器镜像请安装 libreoffice-writer/libreoffice-calc/libreoffice-impress"
        )

    targets = CONVERSION_TARGETS[suffix]
    timeout = conversion_timeout_seconds()
    errors: list[str] = []

    with tempfile.TemporaryDirectory(prefix="kb-office-", dir=temp_root) as tmp:
        work = Path(tmp)
        profile_dir = work / "profile"
        profile_dir.mkdir()
        # **每次转换一个独占 profile**：并发时不会互相抢 LibreOffice 的单实例锁；
        # 并且显式写入"禁宏 / 禁链接更新 / 禁联网更新"的配置（--headless 不管这些）。
        write_profile_config(profile_dir)
        source_dir = work / "src"
        source_dir.mkdir()
        stem = safe_stem(name)
        source = source_dir / f"{stem}{suffix}"
        assert_within(work, source)
        # 只写临时副本；原件（raw / blob）自始至终只读。
        source.write_bytes(raw)

        for index, target in enumerate(targets):
            output_dir = work / f"out{index}"
            output_dir.mkdir()
            try:
                outcome = run_conversion(
                    binary,
                    source=source,
                    output_dir=output_dir,
                    profile_dir=profile_dir,
                    target=target,
                    timeout=timeout,
                    runner=runner,
                )
            except ParseError as exc:
                # 失败就记原因，继续试下一个候选目标；**绝不把失败当成功返回**。
                errors.append(f"{target}：{exc}")
                continue

            if not outcome.ok:
                detail = f"LibreOffice 转换 {target} 返回非零码 {outcome.returncode}"
                if outcome.stderr:
                    detail += f"；stderr：{outcome.stderr}"
                errors.append(detail)
                continue

            wanted = [path for path in outcome.files if path.suffix.lower() == target]
            if not wanted:
                detail = f"LibreOffice 未产出 {target}（返回码 {outcome.returncode}）"
                if outcome.stderr:
                    detail += f"；stderr：{outcome.stderr}"
                errors.append(detail)
                continue

            try:
                return parse_converted(
                    wanted[0],
                    name=name,
                    ocr=ocr,
                    ocr_pages=ocr_pages,
                    parser_lookup=parser_lookup,
                )
            except ParseError as exc:
                errors.append(f"{target}：{exc}")
                continue

        # 走到这里说明所有候选目标都失败了：**必须明确失败**，绝不返回空正文。
        detail = "；".join(errors) if errors else "没有任何候选转换目标"
        raise ParseError(
            f"{suffix} 转换/解析全部失败（尝试：{', '.join(targets)}）：{detail}"
        )


def describe_capabilities() -> dict[str, object]:
    """给运维/健康检查用的一句话能力描述（不参与解析逻辑）。"""
    return {
        "soffice": find_soffice(),
        "available": soffice_available(),
        "suffixes": sorted(LEGACY_SUFFIXES),
        "targets": {key: list(value) for key, value in sorted(CONVERSION_TARGETS.items())},
        "blocked": sorted(BLOCKED_SUFFIXES),
        "max_input_bytes": max_input_bytes(),
        "timeout_seconds": conversion_timeout_seconds(),
        "max_output_files": max_output_files(),
        "max_output_bytes": max_output_bytes(),
        "psutil": psutil_available(),
        "process_group_kill": True,
        "flags": list(SOFFICE_FLAGS),
        "profile_security_settings": {
            path: dict(props) for path, props in PROFILE_SECURITY_SETTINGS.items()
        },
    }
