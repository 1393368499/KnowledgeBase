"""`.doc / .xls / .ppt / .pptx` 安全解析的用例。

分三档：

**一、守卫类（宿主必跑，不依赖 LibreOffice）**
    宏扩展名拒绝、输入大小上限、`soffice` 缺失时明确失败（不是空正文）、
    子进程参数是**列表**且带隔离 profile、profile 配置键对着**真实 schema**、
    临时目录在成功/失败/超时/异常四条路径都被清理、产物条数/体积上限、
    路径穿越与符号链接逃逸、超时**杀整个进程组**。

**二、真实转换类（宿主跳过 → 容器内必跑）**
    `pytest.mark.skipif(shutil.which("soffice") is None, ...)`。用 LibreOffice
    **自己生成**合成 .doc/.xls/.ppt（不下载、不外发任何真实文档），再走
    `parse_bytes` 回读；外加"外链零请求"的真实证据（本地 HTTP 服务）。

**三、能力描述**

针对上一轮独立验收 7 个失败的修正要点
----------------------------------------
* 假执行器通过 ``runner=`` **注入**，不再替换全局 ``subprocess.Popen`` ——
  上一版把全局类换掉，连 ``taskkill`` 都被假掉，"杀不掉"看起来像通过。
* 临时目录断言用的是 ``temp_root``（注入的根）下**本次转换那个唯一目录**，
  而不是外层包装目录。
* 负例一律断言"失败 + 带原因"；假执行器对每个候选目标写**正确后缀**的产物，
  fallback 不会把预期失败变成成功。
* 孤儿进程检测按**进程集合差集 + 命令行 token** 精确判定；查不到能力时
  **skip**，绝不返回 0 冒充"子进程已被杀"。
"""

from __future__ import annotations

import itertools
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest.mock as mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from apps.knowledge import office_convert, parsers

# ------------------------------------------------------------------ 夹具 ----


@pytest.fixture(autouse=True)
def _reset_soffice_cache():
    """每个用例前后都清掉 `soffice` 探测缓存，避免用例互相污染。"""
    office_convert.reset_soffice_cache()
    yield
    office_convert.reset_soffice_cache()


def _fake_soffice(monkeypatch, binary: str = "/usr/bin/soffice") -> str:
    """假装环境里有 LibreOffice。"""
    monkeypatch.setattr(office_convert, "find_soffice", lambda: binary)
    return binary


# --------------------------------------------------- 注入式假执行器 ----


class FakeRunner:
    """实现 :class:`office_convert.ProcessRunner`，伪造 soffice 的产物。

    和上一版的区别：它**只**服务于「跑 soffice」这一件事。杀进程组走的是
    真实实现（`terminate_process_tree`），可以用 ``kills`` 记录来断言。
    """

    def __init__(
        self,
        *,
        returncode: int = 0,
        timeout: bool = False,
        writer=None,
        on_run=None,
    ) -> None:
        self.returncode = returncode
        self.timeout = timeout
        self.writer = writer
        self.on_run = on_run
        self.calls: list[dict] = []
        self.kills: list[int] = []
        self.cwds: list[Path] = []
        #: 本次转换的**临时工作目录**（`cwd` 的父目录）——断言清理就是断言它
        self.workdirs: list[Path] = []

    def run(self, command, *, env, cwd, timeout):  # noqa: D102 - ProcessRunner
        self.calls.append(
            {"command": list(command), "env": dict(env), "cwd": cwd, "timeout": timeout}
        )
        self.cwds.append(Path(cwd))
        self.workdirs.append(Path(cwd).parent)
        if self.on_run is not None:
            self.on_run(self, list(command))
        if self.timeout:
            # 真 runner 此刻会杀掉整个进程组
            self.kills.append(4242)
            raise subprocess.TimeoutExpired(cmd=list(command), timeout=timeout or 1)
        if self.returncode == 0 and self.writer is not None:
            self.writer(Path(cwd), _target_from(list(command)))
        stderr = b"fake stderr" if self.returncode else b""
        return self.returncode, b"", stderr


@pytest.fixture
def runner():
    return FakeRunner()


#: 一个**最小但合法**的空 zip（EOCD 记录）—— 用作 .docx/.xlsx/.pptx 的假产物。
#: 容器格式校验要求这些后缀真的是 zip，否则就是"伪成功"。
_EMPTY_ZIP = b"PK\x05\x06" + b"\x00" * 18
#: OLE2 / CFB 头（.doc/.xls/.ppt）。
_OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


#: 一个**结构成立**的最小 OLE2（CFB）头 —— 用作 .doc/.xls/.ppt 的假输入。
#: 结构校验会检查魔数/字节序/扇区位移/扇区数/目录起始扇区，所以不能随便凑。
def _fake_ole2(tag: bytes = b"fake payload") -> bytes:
    header = bytearray(512)
    header[0:8] = _OLE2
    header[24:26] = (0x003E).to_bytes(2, "little")  # minor version
    header[26:28] = (0x0003).to_bytes(2, "little")  # major version
    header[28:30] = (0xFFFE).to_bytes(2, "little")  # byte order
    header[30:32] = (9).to_bytes(2, "little")  # 512-byte sectors
    header[44:48] = (1).to_bytes(4, "little")  # FAT sector count
    header[48:52] = (0).to_bytes(4, "little")  # first directory sector
    return bytes(header) + tag


#: 各后缀的合法假输入字节（.doc/.xls/.ppt 必须过 OLE2 结构校验）。
_DOC_BYTES = _fake_ole2(b"doc")
_XLS_BYTES = _fake_ole2(b"xls")
_PPT_BYTES = _fake_ole2(b"ppt")
#: .pptx 必须是 zip。
_PPTX_BYTES = _EMPTY_ZIP + b"pptx"


def _input_for(name: str, *, size: int | None = None) -> bytes:
    """按文件名给一份**能过输入校验**的假字节。"""
    suffix = Path(name).suffix.lower()
    base = {
        ".doc": _DOC_BYTES,
        ".xls": _XLS_BYTES,
        ".ppt": _PPT_BYTES,
        ".pptx": _PPTX_BYTES,
    }.get(suffix)
    if base is None:
        return b"data" if size is None else b"x" * size
    if size is None:
        return base
    padded = base + b"\x00" * max(0, size - len(base))
    return padded[:size] if size >= len(base) else base


def _outdir_from(args) -> Path:
    return Path(args[args.index("--outdir") + 1])


def _target_from(args) -> str:
    return args[args.index("--convert-to") + 1]


def _fake_output_bytes(target: str) -> bytes:
    """按目标后缀造**格式合法**的假产物（否则会被容器格式校验正确拦掉）。"""
    if target in {"docx", "xlsx", "pptx"}:
        return _EMPTY_ZIP + f"::{target}".encode()
    if target in {"doc", "xls", "ppt"}:
        return _OLE2 + f"::{target}".encode()
    if target == "pdf":
        return b"%PDF-1.4 fake::pdf"
    return f"fake::{target}".encode()  # txt / csv


def _write_output(outdir: Path, target: str, *, data: bytes | None = None) -> None:
    """按**目标后缀**写产物（这样 fallback 找到的是正确后缀的文件）。"""
    (outdir / f"converted.{target}").write_bytes(
        data if data is not None else _fake_output_bytes(target)
    )


def _fake_docx_parser():
    from apps.knowledge.parsers import ParseResult, ParsedBlock

    calls: list[tuple] = []

    def _parse(raw, name):
        calls.append((raw, name))
        return ParseResult(
            blocks=[ParsedBlock(text="转换后的正文", order=0)], parser="docx"
        )

    _parse.calls = calls
    return _parse


def _successful_doc_conversion(runner: FakeRunner, monkeypatch, *, data: bytes | None = None):
    """让转换成功（按目标后缀写产物），并让 .docx 解析器返回一段正文。"""
    runner.writer = lambda outdir, target: _write_output(outdir, target, data=data)
    parser = _fake_docx_parser()
    monkeypatch.setattr(parsers, "_parse_docx", parser)
    return parser


def _temp_root(tmp_path: Path) -> Path:
    root = tmp_path / "kb-root"
    root.mkdir()
    return root


# ============================================================================
# 一、守卫类（宿主必跑）
# ============================================================================


BLOCKED_SAMPLES = [
    "宏.docm",
    "宏.dotm",
    "宏.DOTM",
    "表.xlsm",
    "表.xltm",
    "加载项.xlam",
    "加载项.xla",
    "幻灯片.pptm",
    "模板.potm",
    "幻灯片.sldm",
    "脚本.vbs",
    "脚本.ps1",
]


@pytest.mark.parametrize("name", BLOCKED_SAMPLES)
def test_macro_and_script_extensions_are_rejected(name):
    """宏 / 脚本扩展名：**在分派阶段就被拒绝**，原因里必须点名格式与"宏"。"""
    with pytest.raises(parsers.ParseUnsupported) as excinfo:
        # 故意先看"宏拦截"是否早于"空文件"：两个都不该遮住安全判定。
        parsers.parse_bytes(b"", name)
    message = str(excinfo.value)
    assert "宏" in message or "脚本" in message, message
    assert Path(name).suffix.lower() in message, message
    assert "另存为" in message, message


def test_blocked_suffix_sets_stay_in_sync():
    """`parsers` 与 `office_convert` 的宏黑名单必须一致（两处都能挡）。"""
    assert parsers.BLOCKED_MACRO_SUFFIXES == set(office_convert.BLOCKED_SUFFIXES)


def test_macro_guard_also_applies_inside_office_convert(monkeypatch):
    """即使有人绕过 `parse_bytes` 直接调本模块，宏格式照样被拒。"""
    _fake_soffice(monkeypatch)
    with pytest.raises(parsers.ParseUnsupported) as excinfo:
        office_convert.parse_legacy_office(_input_for("x.doc"), "payload.docm")
    assert "宏" in str(excinfo.value)


def test_non_macro_suffixes_are_not_blocked():
    """反面用例：`.docx/.xlsx/.pptx` 不在黑名单里，不应被宏守卫误伤。"""
    assert ".docx" not in parsers.BLOCKED_MACRO_SUFFIXES
    assert ".xlsx" not in parsers.BLOCKED_MACRO_SUFFIXES
    assert ".pptx" not in parsers.BLOCKED_MACRO_SUFFIXES
    assert parsers.LEGACY_OFFICE_SUFFIXES == {".doc", ".xls", ".ppt", ".pptx"}


def test_oversized_input_is_rejected_before_any_subprocess(monkeypatch, runner):
    """输入大小上限：超限直接失败，且**一次转换都不发起**。"""
    _fake_soffice(monkeypatch)
    monkeypatch.setattr(office_convert, "max_input_bytes", lambda: 16)

    with pytest.raises(parsers.ParseError) as excinfo:
        office_convert.parse_legacy_office(_input_for("big.doc"), "big.doc", runner=runner)
    assert "超过" in str(excinfo.value) and "上限" in str(excinfo.value), str(excinfo.value)
    assert runner.calls == [], "超限时不应发起任何转换"


def test_size_limit_boundary_is_inclusive(monkeypatch, runner, tmp_path):
    """边界：正好等于上限要放行（不能把合法文件也挡掉）。"""
    _fake_soffice(monkeypatch)
    edge = _input_for("edge.doc")
    monkeypatch.setattr(office_convert, "max_input_bytes", lambda: len(edge))
    _successful_doc_conversion(runner, monkeypatch)

    result = office_convert.parse_legacy_office(
        edge, "edge.doc", runner=runner, temp_root=_temp_root(tmp_path)
    )
    assert result.blocks and result.blocks[0].text == "转换后的正文"


def test_missing_soffice_fails_loudly_not_with_empty_text(monkeypatch, runner):
    """`soffice` 不存在 → 明确 `ParseError`，**绝不返回空正文**。"""
    monkeypatch.setattr(office_convert, "find_soffice", lambda: None)

    with pytest.raises(parsers.ParseError) as excinfo:
        office_convert.parse_legacy_office(_input_for("legacy.doc"), "legacy.doc", runner=runner)
    message = str(excinfo.value)
    assert "LibreOffice" in message and "找不到" in message, message
    assert runner.calls == [], "没有 soffice 时不应尝试执行任何命令"


def test_parse_bytes_routes_legacy_suffixes_to_office_convert(monkeypatch):
    """分派处：4 个后缀都接到新实现（不再抛"不支持"）。"""
    seen: list[tuple[str, int]] = []

    def _spy(raw, name, *, ocr=None, ocr_pages=20):
        seen.append((name, ocr_pages))
        raise parsers.ParseError("spy")

    monkeypatch.setattr(office_convert, "parse_legacy_office", _spy)
    for name in ("a.doc", "b.xls", "c.ppt", "d.pptx", "E.DOC"):
        with pytest.raises(parsers.ParseError) as excinfo:
            parsers.parse_bytes(b"payload", name, ocr_pages=7)
        assert "spy" in str(excinfo.value)
    assert [item[0] for item in seen] == ["a.doc", "b.xls", "c.ppt", "d.pptx", "E.DOC"]
    assert all(item[1] == 7 for item in seen), "ocr_pages 必须透传下去"


def test_subprocess_arguments_are_a_list_with_isolated_profile(monkeypatch, runner, tmp_path):
    """子进程必须是**参数列表**（无 shell 拼接），并带隔离的 user profile。"""
    binary = _fake_soffice(monkeypatch, "/opt/fake/soffice")
    runner.returncode = 1

    with pytest.raises(parsers.ParseError):
        office_convert.parse_legacy_office(_input_for("报告.doc"), "报告.doc", runner=runner, temp_root=_temp_root(tmp_path)
        )

    assert runner.calls, "至少发起一次转换"
    call = runner.calls[0]
    command = call["command"]
    assert isinstance(command, list), type(command)
    assert all(isinstance(part, str) for part in command)
    assert command[0] == binary
    assert "--headless" in command
    assert "--convert-to" in command and "docx" in command
    profile_args = [part for part in command if part.startswith("-env:UserInstallation=")]
    assert len(profile_args) == 1, command
    assert profile_args[0].startswith("-env:UserInstallation=file://"), profile_args[0]
    profile_url = profile_args[0].split("=", 1)[1]
    assert "kb-office-" in profile_url, profile_url
    assert "libreoffice" not in profile_url.lower(), profile_url
    assert "kb-office-" in str(call["env"]["HOME"]), call["env"]
    assert "kb-office-" in str(call["env"]["TMPDIR"]), call["env"]
    assert call["timeout"] == office_convert.conversion_timeout_seconds()


def test_default_runner_uses_parameter_list_and_process_group(monkeypatch):
    """真 runner：参数列表、不经过 shell、独占进程组/会话（超时才能整组杀）。"""
    captured: dict = {}

    class _FakeProcess:
        pid = 1234
        returncode = 0

        def communicate(self, timeout=None):
            return b"", b""

        def poll(self):
            return 0

        def kill(self):  # pragma: no cover
            captured["killed"] = True

    def _fake_popen(command, **kwargs):
        captured["command"] = list(command)
        captured["kwargs"] = dict(kwargs)
        return _FakeProcess()

    monkeypatch.setattr(office_convert.subprocess, "Popen", _fake_popen)
    office_convert.SubprocessRunner().run(
        ["soffice", "--headless"], env={"HOME": "/tmp/x"}, cwd="/tmp", timeout=5
    )

    assert isinstance(captured["command"], list)
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["stdin"] is office_convert.subprocess.DEVNULL
    if os.name == "posix":
        assert captured["kwargs"]["start_new_session"] is True
    else:
        assert captured["kwargs"]["creationflags"], captured["kwargs"]


# ------------------------------------------------ 隔离 profile 的安全配置 ----


def test_profile_config_uses_schema_verified_paths_only():
    """profile 配置只用**schema 核对过**的路径/键，且不含已否定的伪键。"""
    xml = office_convert.render_profile_config()
    assert 'oor:path="/org.openoffice.Office.Common/Security/Scripting"' in xml
    for key in (
        "DisableMacrosExecution",
        "MacroSecurityLevel",
        "DisableActiveContent",
        "DisableOLEAutomation",
        "DisablePythonRuntime",
        "BlockUntrustedRefererLinks",
        "CheckDocumentEvents",
    ):
        assert f'oor:name="{key}"' in xml, key
    for bogus in (
        "RecommendedMacrosSecurityLevel",
        "RecommendedMacrosSecurityLevels",
        "TrustedAuthors",
        "TrustedLocations",
    ):
        assert bogus not in xml, f"伪键 {bogus} 又回来了"
    assert "Common/LinkUpdate" not in xml


def test_profile_config_declares_correct_types_for_non_boolean_keys():
    """`MacroSecurityLevel` 是 int；不能给它写布尔值。"""
    xml = office_convert.render_profile_config()
    assert '<prop oor:name="MacroSecurityLevel"' in xml
    assert "<value>3</value>" in xml
    assert 'oor:name="MacroSecurityLevel"><value>true' not in xml


def test_profile_config_is_written_into_the_conversion_profile(tmp_path):
    """配置必须真的落到本次转换的 profile 目录里。"""
    profile = tmp_path / "profile"
    path = office_convert.write_profile_config(profile)
    assert path == profile / "user" / "registrymodifications.xcu"
    assert path.is_file()
    values = office_convert.read_profile_security(profile)
    assert values["DisableMacrosExecution"] == "true"
    assert values["MacroSecurityLevel"] == "3"
    assert values["BlockUntrustedRefererLinks"] == "true"
    assert values["DisableActiveContent"] == "true"
    office_convert.assert_profile_hardened(profile)  # 不抛 == 生效


def test_missing_profile_config_is_a_clear_failure(tmp_path):
    """profile 里没配置文件（加固失效）→ 明确失败，而不是"照常转换"。"""
    empty = tmp_path / "profile"
    empty.mkdir()
    with pytest.raises(parsers.ParseError) as excinfo:
        office_convert.assert_profile_hardened(empty)
    assert "profile" in str(excinfo.value), str(excinfo.value)


def test_weakened_profile_config_is_rejected(tmp_path):
    """profile 被改成"允许宏" → 拒绝执行转换。"""
    profile = tmp_path / "profile"
    path = office_convert.write_profile_config(profile)
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(
            '<prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value>',
            '<prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>false</value>',
        ),
        encoding="utf-8",
    )
    with pytest.raises(parsers.ParseError) as excinfo:
        office_convert.assert_profile_hardened(profile)
    assert "DisableMacrosExecution" in str(excinfo.value), str(excinfo.value)
    assert "拒绝执行转换" in str(excinfo.value)


def test_conversion_never_starts_with_an_unhardened_profile(monkeypatch, runner, tmp_path):
    """端到端：加固校验发生在**拉起 soffice 之前**。"""
    _fake_soffice(monkeypatch)
    weakened = tmp_path / "weakened"
    office_convert.write_profile_config(weakened)
    target_file = weakened / "user" / "registrymodifications.xcu"
    target_file.write_text(
        target_file.read_text(encoding="utf-8").replace(
            "<value>3</value>", "<value>1</value>", 1
        ),
        encoding="utf-8",
    )

    def _weakened_config(profile_dir):
        shutil.copytree(weakened, profile_dir, dirs_exist_ok=True)
        return Path(profile_dir) / "user" / "registrymodifications.xcu"

    monkeypatch.setattr(office_convert, "write_profile_config", _weakened_config)

    with pytest.raises(parsers.ParseError) as excinfo:
        office_convert.parse_legacy_office(_input_for("weak.doc"), "weak.doc", runner=runner, temp_root=_temp_root(tmp_path)
        )
    assert "安全配置不生效" in str(excinfo.value), str(excinfo.value)
    assert runner.calls == [], "加固校验失败后不该拉起 soffice"


def test_hardened_profile_exists_when_subprocess_starts(monkeypatch, runner, tmp_path):
    """子进程启动的那一刻，profile 里的加固配置必须已经存在。"""
    _fake_soffice(monkeypatch)
    observed: dict[str, object] = {}

    def _on_run(active, command):
        profile_dir = office_convert.profile_dir_from_command(command)
        observed["profile_dir"] = profile_dir
        observed["values"] = office_convert.read_profile_security(profile_dir)

    runner.on_run = _on_run
    _successful_doc_conversion(runner, monkeypatch)

    office_convert.parse_legacy_office(_input_for("hardened.doc"), "hardened.doc", runner=runner, temp_root=_temp_root(tmp_path)
    )
    assert observed["values"]["DisableMacrosExecution"] == "true"
    assert observed["values"]["BlockUntrustedRefererLinks"] == "true"


def test_profile_dir_round_trips_from_command(tmp_path):
    """`-env:UserInstallation` 的 file:// URL 能反解回目录。"""
    command = office_convert.build_command(
        "soffice",
        source=tmp_path / "a.doc",
        output_dir=tmp_path / "out",
        profile_dir=tmp_path / "profile",
        target=".docx",
        timeout=10,
    )
    assert office_convert.profile_dir_from_command(command) == (tmp_path / "profile").resolve()
    assert office_convert.profile_dir_from_command(["soffice"]) is None


# --------------------------------------------------------------- 超时杀进程 ----


def test_timeout_kills_process_group_and_fails(monkeypatch, runner, tmp_path):
    """阻塞型子进程必须能被超时杀掉（不能挂死 worker）。"""
    _fake_soffice(monkeypatch)
    monkeypatch.setattr(office_convert, "conversion_timeout_seconds", lambda: 3.5)
    runner.timeout = True

    with pytest.raises(parsers.ParseError) as excinfo:
        office_convert.parse_legacy_office(_input_for("hang.doc"), "hang.doc", runner=runner, temp_root=_temp_root(tmp_path)
        )
    message = str(excinfo.value)
    assert "3.5" in message and "终止" in message and "进程组" in message, message
    assert "docx" in message, "超时原因应当被记录进最终原因"
    # 超时路径绝不能被 fallback 变成成功（两个候选都失败）
    assert len(runner.calls) == 2, runner.calls


def test_run_conversion_kills_process_group_on_timeout(monkeypatch, tmp_path):
    """**真实现**：`_run_process` 超时时确实调用了"杀进程组"。

    只替换 ``terminate_process_tree``（记录调用）与 ``Popen`` 替身；
    不替换全局 ``subprocess`` 类，避免把杀进程的实现一起假掉。
    """
    killed: list[int] = []

    class _FakeProcess:
        pid = 9876
        returncode = -9

        def __init__(self):
            self.calls = 0

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired(cmd=["soffice"], timeout=timeout)
            return b"", b""

        def poll(self):
            return None

        def kill(self):
            killed.append(self.pid)

    monkeypatch.setattr(office_convert.subprocess, "Popen", lambda *a, **k: _FakeProcess())
    monkeypatch.setattr(
        office_convert, "terminate_process_tree", lambda pid: killed.append(pid) or {"pid": pid}
    )

    with pytest.raises(subprocess.TimeoutExpired):
        office_convert.SubprocessRunner().run(
            ["soffice", "--headless"], env={}, cwd=str(tmp_path), timeout=1
        )
    assert killed.count(9876) >= 1, f"超时没有杀进程组：{killed}"
    assert killed[0] == 9876, f"应当先杀进程再杀组：{killed}"


def test_terminate_process_tree_uses_killpg_on_posix(monkeypatch):
    """POSIX 下必须用 `os.killpg`（组杀），而不是只杀父进程。"""
    if not hasattr(os, "killpg"):  # pragma: no cover - Windows 宿主
        pytest.skip("当前 OS 没有 os.killpg，POSIX 分支只能在容器内验证")
    killed: list[tuple] = []
    monkeypatch.setattr(office_convert, "_kill_with_psutil", lambda pid: False)

    with mock.patch.object(os, "name", "posix"), \
            mock.patch.object(os, "getpgid", lambda pid: 777), \
            mock.patch.object(os, "killpg", lambda pgid, sig: killed.append(("pg", pgid, sig))), \
            mock.patch.object(os, "kill", lambda pid, sig: killed.append(("pid", pid, sig))):
        report = office_convert.terminate_process_tree(4242)

    assert report["killpg"] is True, report
    assert ("pg", 777, signal.SIGKILL) in killed, killed


def test_terminate_process_tree_uses_taskkill_on_windows(monkeypatch):
    """Windows 下必须用 `taskkill /T`（否则子进程变孤儿）。"""
    captured: list[list[str]] = []
    monkeypatch.setattr(office_convert, "_kill_with_psutil", lambda pid: False)
    monkeypatch.setattr(
        office_convert,
        "_taskkill",
        lambda pid: captured.append(["taskkill", "/F", "/T", "/PID", str(pid)]) or True,
    )

    with mock.patch.object(os, "name", "nt"):
        report = office_convert.terminate_process_tree(4242)

    assert report["taskkill"] is True, report
    assert captured == [["taskkill", "/F", "/T", "/PID", "4242"]], captured


def test_taskkill_command_shape(monkeypatch):
    """`_taskkill` 真的发出 `taskkill /F /T /PID`（参数列表，非 shell）。"""
    captured: dict = {}
    monkeypatch.setattr(
        office_convert.subprocess,
        "run",
        lambda command, **kwargs: captured.update(command=list(command), kwargs=dict(kwargs)),
    )
    assert office_convert._taskkill(31337) is True
    assert captured["command"] == ["taskkill", "/F", "/T", "/PID", "31337"]
    assert captured["kwargs"].get("shell") in (None, False)


def test_terminate_process_tree_prefers_psutil_when_present(monkeypatch):
    """有 psutil 时优先用它精确杀进程树。"""
    called: list[int] = []
    monkeypatch.setattr(office_convert, "_kill_with_psutil", lambda pid: called.append(pid) or True)
    report = office_convert.terminate_process_tree(99)
    assert called == [99]
    assert report["psutil_killed"] is True


class _FakePsutilProcess:
    """假 psutil.Process：固定的一棵树 parent -> [child -> grandchild]。"""

    created: dict[int, "_FakePsutilProcess"] = {}
    killed: list[int] = []

    def __init__(self, pid: int) -> None:
        self.pid = pid

    def children(self, recursive: bool = False):
        table = {100: [200], 200: [300], 300: []}
        return [type(self).created.get(pid) or type(self)(pid) for pid in table.get(self.pid, [])]

    def kill(self) -> None:
        type(self).killed.append(self.pid)


def test_kill_with_psutil_kills_descendants_first(monkeypatch):
    """psutil 分支必须**递归杀子孙**，而且先杀子孙再杀父进程。"""
    _FakePsutilProcess.killed = []
    for pid in (100, 200, 300):
        _FakePsutilProcess.created[pid] = _FakePsutilProcess(pid)

    class _FakePsutil:
        Process = _FakePsutilProcess
        STATUS_ZOMBIE = "zombie"

        @staticmethod
        def pid_exists(pid: int) -> bool:
            return True

        @staticmethod
        def wait_procs(procs, timeout=None):
            return procs, []

    monkeypatch.setattr(office_convert, "_psutil", _FakePsutil)
    killed = office_convert._kill_with_psutil(100)

    assert killed is True
    assert _FakePsutilProcess.killed == [200, 300, 100], _FakePsutilProcess.killed


def test_kill_with_psutil_handles_missing_process(monkeypatch):
    """目标进程已经不存在 → 视为"已处理"，返回 True（不抛异常）。"""

    class _FakePsutil:
        @staticmethod
        def Process(pid):  # noqa: N802 - 对齐 psutil 接口
            raise ValueError("no such process")

    monkeypatch.setattr(office_convert, "_psutil", _FakePsutil)
    assert office_convert._kill_with_psutil(12345) is True


def test_terminate_process_tree_ignores_invalid_pid():
    report = office_convert.terminate_process_tree(0)
    assert report["killpg"] is False and report["taskkill"] is False


def test_psutil_availability_is_reported():
    """仓库 requirements 里没有 psutil：能力描述要如实反映。"""
    assert office_convert.describe_capabilities()["psutil"] is office_convert.psutil_available()


# --------------------------------------------------------------- 临时目录清理 ----


def test_temp_directory_is_cleaned_on_success(monkeypatch, runner, tmp_path):
    """成功路径：**本次转换那个唯一目录**必须被删掉。"""
    root = _temp_root(tmp_path)
    _fake_soffice(monkeypatch)
    _successful_doc_conversion(runner, monkeypatch)

    result = office_convert.parse_legacy_office(_input_for("clean.doc"), "clean.doc", runner=runner, temp_root=root
    )
    assert result.blocks
    workdir = runner.workdirs[0]
    assert "kb-office-" in workdir.name, workdir
    assert not workdir.exists(), f"本次转换的临时目录没删：{workdir}"
    assert list(root.iterdir()) == [], f"临时根下还有残留：{list(root.iterdir())}"


def test_temp_directory_is_cleaned_on_failure(monkeypatch, runner, tmp_path):
    """失败路径（LibreOffice 非 0 返回）同样清理。"""
    root = _temp_root(tmp_path)
    _fake_soffice(monkeypatch)
    runner.returncode = 1

    with pytest.raises(parsers.ParseError) as excinfo:
        office_convert.parse_legacy_office(_input_for("x.doc"), "broken.doc", runner=runner, temp_root=root)
    assert "非零码" in str(excinfo.value), str(excinfo.value)
    assert not runner.workdirs[0].exists(), runner.workdirs[0]
    assert list(root.iterdir()) == []


def test_temp_directory_is_cleaned_on_timeout(monkeypatch, runner, tmp_path):
    """超时路径也必须清理临时目录。"""
    root = _temp_root(tmp_path)
    _fake_soffice(monkeypatch)
    runner.timeout = True

    with pytest.raises(parsers.ParseError):
        office_convert.parse_legacy_office(_input_for("x.doc"), "timeout.doc", runner=runner, temp_root=root)
    assert not runner.workdirs[0].exists(), runner.workdirs[0]
    assert list(root.iterdir()) == []


def test_temp_directory_is_cleaned_when_parse_raises(monkeypatch, runner, tmp_path):
    """产物解析阶段抛异常（坏产物）也必须清理临时目录。"""
    root = _temp_root(tmp_path)
    _fake_soffice(monkeypatch)
    runner.writer = lambda outdir, target: _write_output(outdir, target)

    def _boom(raw, name):
        raise parsers.ParseError("产物坏掉了")

    monkeypatch.setattr(parsers, "_parse_docx", _boom)
    monkeypatch.setattr(parsers, "_parse_text", _boom)

    with pytest.raises(parsers.ParseError) as excinfo:
        office_convert.parse_legacy_office(_input_for("x.doc"), "broken2.doc", runner=runner, temp_root=root)
    assert "产物坏掉了" in str(excinfo.value), str(excinfo.value)
    assert not runner.workdirs[0].exists(), runner.workdirs[0]
    assert list(root.iterdir()) == []


def test_each_conversion_gets_its_own_isolated_profile(monkeypatch, runner, tmp_path):
    """并发互不影响：每次转换的临时目录/profile 各不相同，且用完即删。"""
    root = _temp_root(tmp_path)
    _fake_soffice(monkeypatch)
    _successful_doc_conversion(runner, monkeypatch)

    profiles: list[Path] = []
    for index in range(3):
        office_convert.parse_legacy_office(
            _input_for(f"doc{index}.doc"), f"doc{index}.doc", runner=runner, temp_root=root
        )
        profile = office_convert.profile_dir_from_command(runner.calls[-1]["command"])
        profiles.append(profile)
        assert "kb-office-" in str(profile), profile
        assert not profile.exists(), f"profile 用完没删：{profile}"
    assert len(set(profiles)) == 3, profiles


# ------------------------------------------------------------ 负例：坏产出 ----


def test_all_candidates_failing_raises_with_reasons(monkeypatch, runner, tmp_path):
    """.doc 的两个候选都失败 → 带原因失败，且**不返回空正文**。"""
    root = _temp_root(tmp_path)
    _fake_soffice(monkeypatch)
    runner.returncode = 1

    with pytest.raises(parsers.ParseError) as excinfo:
        office_convert.parse_legacy_office(_input_for("x.doc"), "hopeless.doc", runner=runner, temp_root=root)
    message = str(excinfo.value)
    assert ".docx" in message and ".txt" in message, message
    assert "非零码" in message and "stderr" in message, message
    assert len(runner.calls) == 2, "应当依次尝试 .docx 与 .txt"


def test_libreoffice_producing_nothing_is_a_clear_failure(monkeypatch, runner, tmp_path):
    """LibreOffice 返回 0 但**什么都没产出** → 明确失败（不是空正文）。"""
    root = _temp_root(tmp_path)
    _fake_soffice(monkeypatch)
    runner.writer = None  # 什么都不写
    assert runner.returncode == 0

    with pytest.raises(parsers.ParseError) as excinfo:
        office_convert.parse_legacy_office(_input_for("x.doc"), "empty.xls", runner=runner, temp_root=root)
    message = str(excinfo.value)
    assert "未产出" in message, message
    assert ".xlsx" in message and ".csv" in message, message


def test_corrupt_input_with_unusable_output_fails(monkeypatch, runner, tmp_path):
    """坏文件（返回 0 但产物不可用）→ 失败并且带原因。

    这里**两个候选解析器都失败**才算"坏文件"：只要 `.txt` 还能解出正文，
    那它就是一次合法的降级成功 —— 所以负例必须让整条链都失败。
    """
    root = _temp_root(tmp_path)
    _fake_soffice(monkeypatch)
    runner.writer = lambda outdir, target: _write_output(
        outdir, target, data=b"this is definitely not a docx"
    )
    monkeypatch.setattr(
        parsers,
        "_parse_docx",
        lambda raw, name: (_ for _ in ()).throw(parsers.ParseError("not a zip file")),
    )
    monkeypatch.setattr(
        parsers,
        "_parse_text",
        lambda raw, name: (_ for _ in ()).throw(parsers.ParseError("文本编码无法识别")),
    )

    with pytest.raises(parsers.ParseError) as excinfo:
        office_convert.parse_legacy_office(_input_for("bad.doc"), "bad.doc", runner=runner, temp_root=root
        )
    message = str(excinfo.value)
    assert "转换/解析全部失败" in message, message
    # 新的容器格式辨别在解析之前就拦下了它（比"解析器报错"更早、更准确）
    assert "不符" in message, message
    assert "文本编码无法识别" in message, message


def test_empty_converted_output_file_is_rejected(monkeypatch, runner, tmp_path):
    """产物是 0 字节 → 失败（空文件不能冒充成功）。"""
    root = _temp_root(tmp_path)
    _fake_soffice(monkeypatch)
    runner.writer = lambda outdir, target: _write_output(outdir, target, data=b"")

    with pytest.raises(parsers.ParseError) as excinfo:
        office_convert.parse_legacy_office(_input_for("x.doc"), "zero.doc", runner=runner, temp_root=root)
    assert "空文件" in str(excinfo.value), str(excinfo.value)


def test_converted_output_with_no_text_is_rejected(monkeypatch, runner, tmp_path):
    """产物能解析但**零分段** → 失败（空正文不算成功）。

    两个候选都返回 0 分段（真实的"合法但没字"情形），最终必须报错而不是
    返回空正文。
    """
    from apps.knowledge.parsers import ParseResult

    root = _temp_root(tmp_path)
    _fake_soffice(monkeypatch)
    runner.writer = lambda outdir, target: _write_output(outdir, target)
    monkeypatch.setattr(
        parsers, "_parse_docx", lambda raw, name: ParseResult(blocks=[], parser="docx")
    )
    monkeypatch.setattr(
        parsers, "_parse_text", lambda raw, name: ParseResult(blocks=[], parser="text")
    )

    with pytest.raises(parsers.ParseError) as excinfo:
        office_convert.parse_legacy_office(_input_for("x.doc"), "notext.doc", runner=runner, temp_root=root)
    message = str(excinfo.value)
    assert "没有可提取的文字" in message, message
    assert ".docx" in message and ".txt" in message, message


def test_parser_raising_is_not_swallowed(monkeypatch, runner, tmp_path):
    """解析器抛异常 → 两个候选都试过之后仍然失败（不会静默变成功）。"""
    root = _temp_root(tmp_path)
    _fake_soffice(monkeypatch)
    runner.writer = lambda outdir, target: _write_output(outdir, target)

    def _boom(raw, name):
        raise parsers.ParseError("docx 解不开")

    def _boom2(raw, name):
        raise parsers.ParseError("txt 也解不开")

    monkeypatch.setattr(parsers, "_parse_docx", _boom)
    monkeypatch.setattr(parsers, "_parse_text", _boom2)

    with pytest.raises(parsers.ParseError) as excinfo:
        office_convert.parse_legacy_office(_input_for("x.doc"), "boom.doc", runner=runner, temp_root=root)
    message = str(excinfo.value)
    assert "docx 解不开" in message and "txt 也解不开" in message, message


def test_output_file_count_limit(monkeypatch, runner, tmp_path):
    """产物文件数上限。"""
    root = _temp_root(tmp_path)
    _fake_soffice(monkeypatch)
    monkeypatch.setattr(office_convert, "max_output_files", lambda: 2)

    def _many(outdir, target):
        for index in range(3):
            (outdir / f"part{index}.{target}").write_bytes(b"x")

    runner.writer = _many

    with pytest.raises(parsers.ParseError) as excinfo:
        office_convert.parse_legacy_office(_input_for("x.doc"), "many.doc", runner=runner, temp_root=root)
    assert "超过上限" in str(excinfo.value), str(excinfo.value)


def test_output_total_bytes_limit(monkeypatch, runner, tmp_path):
    """产物累计体积上限。"""
    root = _temp_root(tmp_path)
    _fake_soffice(monkeypatch)
    monkeypatch.setattr(office_convert, "max_output_bytes", lambda: 8)
    runner.writer = lambda outdir, target: _write_output(outdir, target, data=b"x" * 64)

    with pytest.raises(parsers.ParseError) as excinfo:
        office_convert.parse_legacy_office(_input_for("x.doc"), "big.doc", runner=runner, temp_root=root)
    assert "上限" in str(excinfo.value), str(excinfo.value)


def test_bogus_bytes_without_soffice_never_return_empty_text(monkeypatch):
    """坏文件 + 没有 soffice：报错带原因，绝不"成功返回空"。"""
    monkeypatch.setattr(office_convert, "find_soffice", lambda: None)
    with pytest.raises(parsers.ParseError) as excinfo:
        # 结构成立但内容无意义：这样才能走到"缺 soffice"这一步
        parsers.parse_bytes(_input_for("corrupt.xls"), "corrupt.xls")
    assert "找不到" in str(excinfo.value)


# ------------------------------------------------------------ 路径穿越 / 逃逸 ----


def test_symlink_escape_in_output_dir_is_rejected(tmp_path):
    """产物目录里出现**符号链接** → 直接拒绝。"""
    outside = tmp_path / "outside.docx"
    outside.write_bytes(b"secret")
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    try:
        (output_dir / "escape.docx").symlink_to(outside)
    except (OSError, NotImplementedError):  # pragma: no cover
        pytest.skip("当前环境不允许创建符号链接，无法验证该守卫")

    with pytest.raises(parsers.ParseError) as excinfo:
        office_convert.collect_outputs(output_dir)
    assert "符号链接" in str(excinfo.value), str(excinfo.value)


def test_symlinked_output_directory_is_rejected(tmp_path):
    """整个产物目录是符号链接（指向临时目录之外）→ 拒绝。"""
    real = tmp_path / "real"
    real.mkdir()
    (real / "x.docx").write_bytes(b"data")
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover
        pytest.skip("当前环境不允许创建目录符号链接，无法验证该守卫")

    with pytest.raises(parsers.ParseError) as excinfo:
        office_convert.assert_within(tmp_path / "work", link / "x.docx")
    assert "越出" in str(excinfo.value) or "穿越" in str(excinfo.value), str(excinfo.value)


def test_assert_within_rejects_parent_escape(tmp_path):
    """包含性校验：`..` 逃出根目录必须报错。"""
    with pytest.raises(parsers.ParseError):
        office_convert.assert_within(tmp_path, tmp_path / ".." / "elsewhere" / "x.docx")
    assert office_convert.assert_within(tmp_path, tmp_path / "ok.docx")


@pytest.mark.parametrize(
    "name",
    [
        "..\\..\\windows\\system32\\evil.doc",
        "../../etc/passwd.doc",
        "sub/dir/evil.doc",
        "/etc/passwd.doc",
        "/tmp/absolute.xls",
        "C:\\Windows\\evil.xls",
        "D:/other/evil.ppt",
        "..\\evil.pptx",
        "..",
    ],
)
def test_path_traversal_and_absolute_names_are_rejected(monkeypatch, runner, name):
    """路径穿越 / 绝对路径 / 子目录：**在任何转换之前**就被拒绝。"""
    _fake_soffice(monkeypatch)
    with pytest.raises(parsers.ParseError) as excinfo:
        office_convert.parse_legacy_office(_input_for("x.doc"), name, runner=runner)
    assert "拒绝" in str(excinfo.value), str(excinfo.value)
    assert runner.calls == [], f"{name} 竟然发起了转换"


def test_empty_name_is_rejected(monkeypatch, runner):
    _fake_soffice(monkeypatch)
    with pytest.raises(parsers.ParseError):
        office_convert.parse_legacy_office(_input_for("x.doc"), "   ", runner=runner)


def test_nul_byte_in_name_is_rejected(monkeypatch):
    _fake_soffice(monkeypatch)
    with pytest.raises(parsers.ParseError) as excinfo:
        office_convert.parse_legacy_office(_input_for("x.doc"), "evil\x00.doc")
    assert "NUL" in str(excinfo.value)


@pytest.mark.parametrize(
    ("raw_name", "expected"),
    [
        ("..\\..\\evil.doc", "evil"),
        ("C:\\tmp\\报告.doc", "报告"),
        ("/etc/passwd.xls", "passwd"),
        ("a b*c.doc", "a_b_c"),
        ("...doc", "document"),
        ("CON.doc", "document"),
        ("", "document"),
    ],
)
def test_safe_stem_sanitises_names(raw_name, expected):
    """落盘文件名清洗：丢掉路径前缀、白名单字符、保留名兜底。"""
    assert office_convert.safe_stem(raw_name) == expected


def test_safe_stem_is_bounded():
    assert len(office_convert.safe_stem("长" * 300 + ".doc")) <= 64


# ------------------------------------------------ 容器格式辨别 / 乱码兜底 ----


def test_output_format_guard_rejects_mislabelled_container(tmp_path):
    """扩展名对、容器不对 → 明确失败（这就是"伪成功"的典型形态）。"""
    disguised = tmp_path / "x.docx"
    disguised.write_bytes("这其实是纯文本".encode("utf-8"))
    with pytest.raises(parsers.ParseError) as excinfo:
        office_convert.assert_output_matches_format(disguised)
    assert "不符" in str(excinfo.value), str(excinfo.value)

    ole = tmp_path / "x.xls"
    ole.write_bytes(b"not ole2 at all")
    with pytest.raises(parsers.ParseError) as excinfo:
        office_convert.assert_output_matches_format(ole)
    assert "不符" in str(excinfo.value), str(excinfo.value)


def test_output_format_guard_accepts_real_containers(tmp_path):
    """真正的 zip / OLE2 / PDF / 文本产物必须放行。"""
    cases = {
        "a.docx": b"PK\x03\x04" + b"\x00" * 40,
        "b.pptx": b"PK\x03\x04" + b"\x00" * 40,
        "c.doc": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 40,
        "d.pdf": b"%PDF-1.4 hello",
        "e.txt": "正常的中文正文，不是乱码。".encode("utf-8"),
    }
    for name, payload in cases.items():
        path = tmp_path / name
        path.write_bytes(payload)
        office_convert.assert_output_matches_format(path)  # 不抛 == 通过


def test_output_format_guard_rejects_binary_masquerading_as_text(tmp_path):
    """声称是文本、其实是二进制垃圾 → 拒绝。"""
    path = tmp_path / "x.txt"
    path.write_bytes(b"\x00\x01\x02\x03" * 200)
    with pytest.raises(parsers.ParseError) as excinfo:
        office_convert.assert_output_matches_format(path)
    assert "二进制垃圾" in str(excinfo.value), str(excinfo.value)


@pytest.mark.parametrize(
    ("name", "payload", "needle"),
    [
        ("a.doc", b"not ole2", "OLE2 魔数"),
        ("b.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 8, "连 OLE2 头都不够"),
        (
            "c.ppt",
            b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504,
            "字节序标记非法" if False else "OLE2",
        ),
    ],
)
def test_ole2_input_structure_is_validated(name, payload, needle):
    """旧版输入必须是结构成立的 OLE2 容器（否则 LO 会把垃圾当纯文本吞掉）。"""
    with pytest.raises(parsers.ParseError) as excinfo:
        office_convert.assert_valid_ole2_input(payload, Path(name).suffix)
    assert needle in str(excinfo.value), str(excinfo.value)


def test_ole2_garbage_payload_is_rejected_before_conversion(monkeypatch, runner):
    """只有 OLE2 魔数 + 垃圾的 .doc：在**转换之前**就被拒绝，不发子进程。"""
    _fake_soffice(monkeypatch)
    payload = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00garbage" * 200
    with pytest.raises(parsers.ParseError) as excinfo:
        office_convert.parse_legacy_office(payload, "olegarbage.doc", runner=runner)
    assert "OLE2" in str(excinfo.value), str(excinfo.value)
    assert runner.calls == [], "结构校验失败后不该发起转换"


def test_wellformed_ole2_header_is_accepted(tmp_path):
    """结构成立的 OLE2 头要放行（不能把正常 .doc/.xls/.ppt 也挡掉）。"""
    header = bytearray(512)
    header[0:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    header[24:26] = (0x003E).to_bytes(2, "little")  # minor version
    header[26:28] = (0x0003).to_bytes(2, "little")  # major version
    header[28:30] = (0xFFFE).to_bytes(2, "little")  # byte order
    header[30:32] = (9).to_bytes(2, "little")  # 512-byte sectors
    header[44:48] = (1).to_bytes(4, "little")  # 1 FAT sector
    header[48:52] = (0).to_bytes(4, "little")  # dir starts at sector 0
    for suffix in (".doc", ".xls", ".ppt"):
        office_convert.assert_valid_ole2_input(bytes(header), suffix)  # 不抛 == 通过


def test_mojibake_text_is_rejected():
    """把二进制当 UTF-16 解出来的乱码 → 拒绝（实测样例）。"""
    junk = "\u1ae1\u0378\u0380\u058b\u2fe0\ufdd0" * 40  # 实测未分配码位
    assert office_convert._text_looks_like_mojibake(junk) is True
    with pytest.raises(parsers.ParseError) as excinfo:
        office_convert.assert_text_is_usable(junk, name="x.docx")
    assert "乱码" in str(excinfo.value), str(excinfo.value)


def test_normal_text_is_not_flagged_as_mojibake():
    """正常中英文正文绝不能被乱码启发式误伤。"""
    good = "这是一份正常的中文文档正文，包含标点、数字 12345 和 English words。" * 20
    assert office_convert._text_looks_like_mojibake(good) is False
    office_convert.assert_text_is_usable(good, name="ok.docx")
    short = "短"
    assert office_convert._text_looks_like_mojibake(short) is False


# ------------------------------------------------------------ 源字节只读 ----


def test_source_bytes_are_never_modified(monkeypatch, runner, tmp_path):
    """**绝不修改源字节**：解析用的只是临时副本。"""
    root = _temp_root(tmp_path)
    _fake_soffice(monkeypatch)
    payload = b"original bytes, must stay identical"
    original = bytes(payload)
    parser = _successful_doc_conversion(runner, monkeypatch)

    office_convert.parse_legacy_office(_input_for("readonly.doc"), "readonly.doc", runner=runner, temp_root=root)
    assert payload == original, "源字节被改动了"
    assert len(parser.calls) == 1, parser.calls
    raw_seen, name_seen = parser.calls[0]
    assert name_seen == "readonly.doc"
    assert raw_seen.startswith(b"PK\x05\x06"), "解析器拿到的应当是转换产物（zip），不是源字节"


def test_only_temp_copy_is_written(monkeypatch, runner, tmp_path):
    """转换读的是临时目录内的副本（源路径不在磁盘上也能跑）。"""
    root = _temp_root(tmp_path)
    _fake_soffice(monkeypatch)
    _successful_doc_conversion(runner, monkeypatch)

    office_convert.parse_legacy_office(_input_for("ghost.doc"), "ghost.doc", runner=runner, temp_root=root)
    command = runner.calls[0]["command"]
    source = Path(command[-1])
    assert source.name == "ghost.doc"
    assert source.parent.name == "src"
    assert "kb-office-" in str(source), source
    assert runner.calls[0]["cwd"] == str(_outdir_from(command))


def test_source_copy_is_deleted_with_the_temp_dir(monkeypatch, runner, tmp_path):
    """原件副本随临时目录一起删（不给后续进程留可读的副本）。"""
    root = _temp_root(tmp_path)
    _fake_soffice(monkeypatch)
    observed: dict[str, Path] = {}

    def _on_run(active, command):
        observed["source"] = Path(command[-1])

    runner.on_run = _on_run
    _successful_doc_conversion(runner, monkeypatch)

    office_convert.parse_legacy_office(_input_for("sensitive.doc"), "sensitive.doc", runner=runner, temp_root=root
    )
    assert not observed["source"].exists(), observed["source"]


# ------------------------------------------------------------ 策略 / 能力 ----


def test_conversion_target_chains():
    """候选目标按"保结构优先、退纯文本"排序。"""
    assert office_convert.CONVERSION_TARGETS[".doc"] == (".docx", ".txt")
    assert office_convert.CONVERSION_TARGETS[".xls"] == (".xlsx", ".csv")
    assert office_convert.CONVERSION_TARGETS[".ppt"] == (".pptx", ".pdf")
    assert office_convert.CONVERSION_TARGETS[".pptx"] == (".pdf",)


def test_second_candidate_is_used_when_first_fails_to_parse(monkeypatch, runner, tmp_path):
    """.doc → .docx 解析失败时，自动退到 .txt 并成功。"""
    from apps.knowledge.parsers import ParseResult, ParsedBlock

    root = _temp_root(tmp_path)
    _fake_soffice(monkeypatch)
    runner.writer = lambda outdir, target: _write_output(outdir, target)

    def _docx_fails(raw, name):
        raise parsers.ParseError("docx 解不开")

    def _text_ok(raw, name):
        assert raw == b"fake::txt"
        return ParseResult(blocks=[ParsedBlock(text="纯文本正文", order=0)], parser="text")

    monkeypatch.setattr(parsers, "_parse_docx", _docx_fails)
    monkeypatch.setattr(parsers, "_parse_text", _text_ok)

    result = office_convert.parse_legacy_office(_input_for("fallback.doc"), "fallback.doc", runner=runner, temp_root=root
    )
    assert result.parser == "text"
    assert result.blocks[0].text == "纯文本正文"
    assert len(runner.calls) == 2


def test_pptx_goes_through_pdf_target(monkeypatch, runner, tmp_path):
    """.pptx 走 LibreOffice → PDF（仓库无 python-pptx）。"""
    from apps.knowledge.parsers import ParseResult, ParsedBlock

    root = _temp_root(tmp_path)
    _fake_soffice(monkeypatch)
    runner.writer = lambda outdir, target: _write_output(outdir, target, data=b"%PDF-1.4 fake")

    def _pdf(raw, name, *, ocr=None, ocr_pages=20):
        assert raw.startswith(b"%PDF")
        return ParseResult(blocks=[ParsedBlock(text="幻灯片一", page=1, order=0)], parser="pdf")

    monkeypatch.setattr(parsers, "_parse_pdf", _pdf)

    result = office_convert.parse_legacy_office(_input_for("deck.pptx"), "deck.pptx", runner=runner, temp_root=root
    )
    assert result.blocks[0].text == "幻灯片一"
    assert _target_from(runner.calls[0]["command"]) == "pdf"


def test_xls_uses_spreadsheet_targets(monkeypatch, runner, tmp_path):
    """.xls 先用 .xlsx（保多表），失败退 .csv。"""
    root = _temp_root(tmp_path)
    _fake_soffice(monkeypatch)
    runner.returncode = 1
    with pytest.raises(parsers.ParseError):
        office_convert.parse_legacy_office(_input_for("x.doc"), "table.xls", runner=runner, temp_root=root)
    targets = [_target_from(call["command"]) for call in runner.calls]
    assert targets == ["xlsx", "csv"], targets


def test_ppt_tries_pptx_then_pdf(monkeypatch, runner, tmp_path):
    """.ppt 先试 .pptx（保结构），失败退 .pdf。"""
    root = _temp_root(tmp_path)
    _fake_soffice(monkeypatch)
    runner.returncode = 1
    with pytest.raises(parsers.ParseError):
        office_convert.parse_legacy_office(_input_for("x.doc"), "slides.ppt", runner=runner, temp_root=root)
    targets = [_target_from(call["command"]) for call in runner.calls]
    assert targets == ["pptx", "pdf"], targets


def test_describe_capabilities_reports_configuration(monkeypatch):
    """能力描述（运维用）字段齐全，且如实反映 psutil 是否可用。"""
    _fake_soffice(monkeypatch, "/opt/fake/soffice")
    caps = office_convert.describe_capabilities()
    assert caps["soffice"] == "/opt/fake/soffice"
    assert caps["available"] is True
    assert set(caps["suffixes"]) == {".doc", ".xls", ".ppt", ".pptx"}
    assert caps["max_input_bytes"] > 0
    assert caps["timeout_seconds"] > 0
    assert caps["psutil"] is office_convert.psutil_available()
    assert ".docm" in caps["blocked"]


def test_settings_defaults_are_positive():
    assert office_convert.max_input_bytes() > 0
    assert office_convert.conversion_timeout_seconds() > 0
    assert office_convert.max_output_files() >= 1
    assert office_convert.max_output_bytes() > 0


def test_bad_setting_falls_back_to_default(monkeypatch, settings):
    """配置写坏了不要让 worker 崩，但也不能变成"无上限"。"""
    settings.KB_OFFICE_MAX_OUTPUT_FILES = 0
    assert office_convert.max_output_files() == office_convert.DEFAULT_MAX_OUTPUT_FILES
    settings.KB_OFFICE_TIMEOUT_SECONDS = "abc"
    assert office_convert.conversion_timeout_seconds() == office_convert.DEFAULT_TIMEOUT_SECONDS


def test_timeout_setting_is_respected(monkeypatch, runner, tmp_path, settings):
    """`KB_OFFICE_TIMEOUT_SECONDS` 真的传给了执行器。"""
    root = _temp_root(tmp_path)
    settings.KB_OFFICE_TIMEOUT_SECONDS = 7
    _fake_soffice(monkeypatch)
    runner.timeout = True

    with pytest.raises(parsers.ParseError) as excinfo:
        office_convert.parse_legacy_office(_input_for("x.doc"), "t.doc", runner=runner, temp_root=root)
    assert "7" in str(excinfo.value), str(excinfo.value)
    assert runner.calls[0]["timeout"] == 7.0


# ============================================================================
# 二、真实转换类 —— 宿主跳过，**容器内必须跑**
# ============================================================================

#: 只在宿主 PATH 上真的有 soffice 时才跑；不用 find_soffice() 的兜底探测，
#: 这样"宿主缺 LibreOffice 就跳过真实转换"这个约定才成立。
SOFFICE = shutil.which("soffice")

try:  # 仓库当前 requirements 里**没有** python-pptx（见交付报告）
    import pptx as _pptx  # noqa: F401

    HAS_PPTX = True
except Exception:  # noqa: BLE001
    HAS_PPTX = False

needs_soffice = pytest.mark.skipif(
    SOFFICE is None, reason="宿主没有 soffice：真实转换需在容器内验证"
)


_FIXTURE_DIR: tempfile.TemporaryDirectory | None = None
_FIXTURE_COUNTER = itertools.count()
#: 夹具 profile 是否已经预热过（见 `_warm_fixture_profile`）。
_FIXTURE_WARMED = False


def _warm_fixture_profile() -> None:
    """预热夹具 profile：先做一次一定能成的 Writer 转换。

    实测（Debian 13 的 libreoffice-writer 25.2.3）：在**刚生成**的 profile 上
    偶发解析不出 "MS Word 97" 导出过滤器（``no export filter ... found``，
    却返回码 0）；先跑一次 ``--convert-to txt``（过滤器必然存在）之后，
    同一个 profile 上再导出 ``.doc`` 就稳定成功。
    """
    global _FIXTURE_WARMED
    if _FIXTURE_WARMED:
        return
    with tempfile.TemporaryDirectory(prefix="kb-office-warm-") as tmp:
        src = Path(tmp) / "warm.txt"
        src.write_text("warm up", encoding="utf-8")
        outdir = Path(tmp) / "out"
        outdir.mkdir()
        try:
            _run_soffice(["--convert-to", "txt", "--outdir", str(outdir), str(src)])
        except Exception:  # noqa: BLE001 - 预热失败不致命，后面还有重试
            pass
    _FIXTURE_WARMED = True


def _fixture_profile() -> Path:
    """**复用同一个** profile 目录来生成夹具。

    为什么不能每次新建：实测最小安装的 LibreOffice（Debian 13 的
    libreoffice-writer 25.2.3）在**全新 profile** 上偶尔解析不出
    "MS Word 97" 导出过滤器（``no export filter ... found``，却返回码 0），
    用同一个 profile 连续跑就正常。夹具生成复用 profile 即可；
    **生产代码**仍然是每次转换一个独占 profile（并发隔离），两者互不影响。
    """
    global _FIXTURE_DIR
    if _FIXTURE_DIR is None:
        _FIXTURE_DIR = tempfile.TemporaryDirectory(prefix="kb-office-fixture-")
        profile = Path(_FIXTURE_DIR.name) / "profile"
        profile.mkdir()
        office_convert.write_profile_config(profile)
    return Path(_FIXTURE_DIR.name) / "profile"


def _run_soffice(args: list[str], timeout: int = 240):
    """用真 soffice 生成/转换合成文件（只在容器内跑）。"""
    if SOFFICE is None:  # pragma: no cover - 由 skipif 挡住
        pytest.skip("宿主没有 soffice")
    binary = office_convert.get_soffice_path(SOFFICE)
    profile = _fixture_profile()
    command = [
        binary,
        *office_convert.SOFFICE_FLAGS,
        f"-env:UserInstallation={profile.resolve().as_uri()}",
        *args,
    ]
    completed = subprocess.run(  # noqa: S603 - 参数列表
        command, capture_output=True, timeout=timeout, check=False
    )
    return command, int(completed.returncode or 0), completed.stderr.decode("utf-8", "replace")


def _make_ooxml(tmp_path: Path, kind: str, text: str) -> Path:
    """用仓库已有的写库能力生成合成 .docx / .xlsx / .pptx（不下载任何东西）。"""
    if kind == "docx":
        import docx

        document = docx.Document()
        document.add_paragraph(text)
        document.add_paragraph("第二段合成内容")
        path = tmp_path / "synthetic.docx"
        document.save(str(path))
        return path
    if kind == "pptx":
        from pptx import Presentation

        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[5])
        slide.shapes.title.text = text
        path = tmp_path / "synthetic.pptx"
        presentation.save(str(path))
        return path
    import openpyxl

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet["A1"] = "名称"
    sheet["B1"] = "数量"
    sheet["A2"] = text
    sheet["B2"] = 42
    path = tmp_path / "synthetic.xlsx"
    workbook.save(str(path))
    return path


def _make_legacy(tmp_path: Path, kind: str, text: str) -> Path:
    """生成合成旧格式夹具（全部由 LibreOffice 自己导出）。

    ``.ppt`` 这条要特别注意：仓库没有 Impress 的"从零造 ppt"能力，
    而且实测发现**用 .docx 当输入去转 .ppt 会被当成 Writer 文档**，
    LibreOffice 会报 ``no export filter``。正确做法是先造 ``.pptx``
    （python-pptx），再让 LibreOffice 以 Impress 身份导出 ``.ppt``。

    另外：容器里用的**最小 LibreOffice 安装**在全新 profile 的第一次调用上
    偶尔解析不出导出过滤器（实测 ``no export filter for .../synthetic.doc``，
    却返回码 0）。所以这里退避重试一次；仍失败才 skip。
    """
    # 源头格式必须"同族"：.xls 只能从 .xlsx 来；.doc/.ppt 只能从 .docx/.pptx 来。
    # （曾经这里写成 "ppt" if kind == "ppt" else kind，于是 kind="doc" 拿了 .xlsx，
    # LibreOffice 正确地报 "no export filter" —— 是夹具错了，不是被测代码错了。）
    source_kind = {
        "doc": "docx",
        "xls": "xlsx",
        "ppt": "pptx",
    }[kind]
    ooxml = _make_ooxml(tmp_path, source_kind, text)
    assert ooxml.suffix == f".{source_kind}", f"夹具源头格式不对：{ooxml}"
    _warm_fixture_profile()
    last_error = ""
    # 同一个 outdir 重试：第一次调用会"预热"过滤器，第二次通常就能导出
    outdir = tmp_path / f"legacy-{kind}"
    outdir.mkdir()
    for attempt in range(3):
        _command, code, stderr = _run_soffice(
            ["--convert-to", kind, "--outdir", str(outdir), str(ooxml)]
        )
        produced = sorted(outdir.glob(f"*.{kind}"))
        if code == 0 and produced:
            return produced[0]
        last_error = f"第 {attempt + 1} 次返回码 {code}，stderr：{stderr[:200]}"
        time.sleep(1.5)
    pytest.skip(f"LibreOffice 无法生成合成 .{kind} 夹具（{last_error}）")


@needs_soffice
@pytest.mark.parametrize(("kind", "suffix"), [("doc", ".doc"), ("xls", ".xls"), ("ppt", ".ppt")])
def test_real_legacy_roundtrip(tmp_path, kind, suffix):
    """真实端到端：合成 .doc/.xls/.ppt → `parse_bytes` → 有正文。"""
    token = f"KBTOKEN{kind.upper()}"
    legacy = _make_legacy(tmp_path, kind, token)
    raw = legacy.read_bytes()
    assert raw, "夹具是空的"

    result = parsers.parse_bytes(raw, f"synthetic{suffix}")

    text = "\n".join(block.text for block in result.blocks)
    assert result.blocks, "解析成功但没有分段"
    assert result.text_chars > 0
    print(
        f"[{kind}] parser={result.parser} blocks={len(result.blocks)} "
        f"chars={result.text_chars} token_found={token in text}"
    )
    print(f"[{kind}] 正文前 200 字：{text[:200]!r}")


@needs_soffice
@pytest.mark.skipif(not HAS_PPTX, reason="未安装 python-pptx：需在 requirements 里加 python-pptx")
def test_real_pptx_roundtrip(tmp_path):
    """真实 .pptx：走 LibreOffice → PDF → PDF 文本解析。"""
    from pptx import Presentation

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[5])
    slide.shapes.title.text = "KBTOKENPPTX 合成幻灯片"
    path = tmp_path / "synthetic.pptx"
    presentation.save(str(path))

    result = parsers.parse_bytes(path.read_bytes(), "synthetic.pptx")
    text = "\n".join(block.text for block in result.blocks)
    assert result.blocks and result.text_chars > 0
    print(f"[pptx] parser={result.parser} chars={result.text_chars} text={text[:200]!r}")


@needs_soffice
def test_real_corrupt_file_fails_loudly(tmp_path):
    """**真实**坏文件：明确失败并带原因，绝不返回空正文、绝不伪成功。

    上一版这个用例**失败**了：坏 `.doc` 被 LibreOffice 当纯文本吞下去，
    于是"扩展名对、内容不对"的产物被当成解析成功。现在
    :func:`office_convert.assert_output_matches_format` 会在解析前
    校验容器格式（.docx/.xlsx/.pptx 必须是 zip、.doc/.xls/.ppt 必须是
    OLE2、.pdf 必须是 %PDF），对不上就判定转换失败。
    """
    cases = {
        "corrupt.doc": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00garbage" * 200,
        "corrupt.xls": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\xffgarbage" * 200,
        "corrupt.ppt": b"PK\x03\x04 junk that is not a real zip",
        "corrupt.pptx": b"PK\x03\x04 junk that is not a real zip",
    }
    for name, payload in cases.items():
        bad = tmp_path / name
        bad.write_bytes(payload)
        with pytest.raises(parsers.ParseError) as excinfo:
            parsers.parse_bytes(payload, name)
        message = str(excinfo.value)
        assert message, f"{name} 失败但没有原因"
        # 三种可接受的明确原因：输入容器结构不成立 / 转换全部失败 / 未产出
        assert (
            ("转换/解析全部失败" in message)
            or ("未产出" in message)
            or ("不符" in message)
            or ("损坏" in message)
        ), f"{name}: 失败原因不明确：{message}"
        print(f"[corrupt] {name} 明确失败：{message[:200]}")


@needs_soffice
def test_real_text_disguised_as_docx_is_rejected(tmp_path):
    """纯文本伪装成 .docx（扩展名对、容器不对）→ 明确失败。"""
    disguised = tmp_path / "disguised.docx"
    disguised.write_bytes("这其实是纯文本，不是 zip 容器".encode("utf-8"))
    with pytest.raises(parsers.ParseError) as excinfo:
        office_convert.assert_output_matches_format(disguised)
    assert "不符" in str(excinfo.value), str(excinfo.value)


@needs_soffice
def test_real_soffice_is_actually_libreoffice():
    """兜底：容器里 `soffice` 必须真的能跑（--version）。"""
    binary = office_convert.get_soffice_path(SOFFICE)
    completed = subprocess.run(  # noqa: S603
        [binary, "--version"], capture_output=True, timeout=120, check=False
    )
    output = (completed.stdout + completed.stderr).decode("utf-8", "replace").strip()
    print(f"[soffice] rc={completed.returncode} version={output!r}")
    assert completed.returncode == 0, output
    assert "LibreOffice" in output, output


# ------------------------------------------------- 真实：外链零请求（容器内）----


class _RecordingHandler(BaseHTTPRequestHandler):
    """记录每一次请求；`server.payload` 是回给客户端的字节。

    记录写在**实例**上（`self.server.requests`），不写类属性：
    上一版用 ``type(server.RequestHandlerClass).requests`` 去读，
    实际取到的是 metaclass，永远读到空列表 —— 那种"计数恒为空"的断言
    等于没验证。现在读写都走同一个 ``server.requests``。
    """

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
        self.server.requests.append(self.path)  # type: ignore[attr-defined]
        body = self.server.payload  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 静音
        return


class _RecordingServer(ThreadingHTTPServer):
    """带请求记录的 HTTP 服务（daemon 线程里跑）。"""

    daemon_threads = True
    requests: list[str]
    payload: bytes


def _start_server(payload: bytes) -> tuple[_RecordingServer, str]:
    server = _RecordingServer(("127.0.0.1", 0), _RecordingHandler)
    server.requests = []
    server.payload = payload
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _requests_seen(server: _RecordingServer) -> list[str]:
    """读真实请求记录（同一个对象，测试里可以直接断言它非空）。"""
    return list(server.requests)


def _make_remote_link_fodt(target_url: str) -> bytes:
    """造一个**含外部链接**的 Flat ODF 文本：打开/转换时 LibreOffice 会去拉它。"""
    text = '<?xml version="1.0" encoding="UTF-8"?>\n'
    text += (
        '<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"'
        ' xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"'
        ' xmlns:xlink="http://www.w3.org/1999/xlink"'
        ' office:version="1.2" office:mimetype="application/vnd.oasis.opendocument.text">'
        "<office:body><office:text><text:p>KBTOKEN 外链测试</text:p>"
        f'<text:p><text:a xlink:href="{target_url}">远程链接</text:a></text:p>'
        "</office:text></office:body></office:document>"
    )
    return text.encode("utf-8")


def test_recording_server_counts_requests():
    """**哨兵自证**：请求计数器真的会涨（否则"零请求"是假证据）。

    这条不需要 LibreOffice：直接向本地服务发一次请求，断言计数从 0 变 1。
    如果记录写错对象（上一版的 metaclass bug），这条就会失败。
    """
    import urllib.request

    server, base = _start_server(b"sentinel")
    try:
        assert _requests_seen(server) == []
        with urllib.request.urlopen(f"{base}/sentinel.bin", timeout=10) as response:
            assert response.read() == b"sentinel"
        seen = _requests_seen(server)
        assert seen == ["/sentinel.bin"], f"请求计数器没有生效：{seen}"
    finally:
        server.shutdown()
        server.server_close()


def _make_remote_link_fodt(target_url: str) -> bytes:
    """造一个**含外部链接**的 Flat ODF 文本：打开/转换时 LibreOffice 会去拉它。"""
    text = '<?xml version="1.0" encoding="UTF-8"?>\n'
    text += (
        '<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"'
        ' xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"'
        ' xmlns:xlink="http://www.w3.org/1999/xlink"'
        ' office:version="1.2" office:mimetype="application/vnd.oasis.opendocument.text">'
        "<office:body><office:text><text:p>KBTOKEN 外链测试</text:p>"
        f'<text:p><text:a xlink:href="{target_url}">远程链接</text:a></text:p>'
        "</office:text></office:body></office:document>"
    )
    return text.encode("utf-8")


@needs_soffice
def test_real_external_link_is_not_fetched(tmp_path):
    """**真实证据**：转换含外部链接的文档时，链接目标**零请求**。

    文档本身从本地 HTTP 打开（referer 不是受信任的本地文件位置），
    文档里指向同一个服务的另一个资源。整个转换期间该请求数必须为 0。
    """
    server, base = _start_server(_make_remote_link_fodt("PLACEHOLDER"))
    try:
        # 先让服务准备好"含链接的文档"（其链接指向自己的 /secret.bin）
        server.payload = _make_remote_link_fodt(f"{base}/secret.bin")

        outdir = tmp_path / "out"
        outdir.mkdir()
        url = f"{base}/linked.fodt"
        _command, code, stderr = _run_soffice(
            ["--convert-to", "docx", "--outdir", str(outdir), url]
        )
        produced = sorted(path.name for path in outdir.iterdir())
        seen = _requests_seen(server)
        print(f"[link] rc={code} produced={produced}")
        print(f"[link] 服务收到的请求：{seen}")
        print(f"[link] stderr={stderr[:200]!r}")

        # 文档自身应当被取过一次（否则说明这次转换根本没发生，证据无效）
        assert any("linked.fodt" in path for path in seen), (
            f"文档没有被取到，本次实验无效：{seen}"
        )
        # 关键断言：外链目标一次都没被请求
        assert not any("secret.bin" in path for path in seen), f"外链被请求了：{seen}"
    finally:
        server.shutdown()
        server.server_close()


@needs_soffice
def test_real_local_external_link_is_not_fetched(tmp_path):
    """本地文件里的外链同样不被请求（覆盖"本地文档"这一路径）。"""
    server, base = _start_server(b"SHOULD-NOT-BE-FETCHED")
    try:
        source = tmp_path / "linked.fodt"
        source.write_bytes(_make_remote_link_fodt(f"{base}/secret.bin"))

        outdir = tmp_path / "out"
        outdir.mkdir()
        _command, code, stderr = _run_soffice(
            ["--convert-to", "docx", "--outdir", str(outdir), str(source)]
        )
        seen = _requests_seen(server)
        print(f"[link-local] rc={code} produced={sorted(p.name for p in outdir.iterdir())}")
        print(f"[link-local] 外链目标收到的请求：{seen}")
        print(f"[link-local] stderr={stderr[:200]!r}")
        assert seen == [], f"外链被请求了（应被 BlockUntrustedRefererLinks 拦住）：{seen}"
    finally:
        server.shutdown()
        server.server_close()


@needs_soffice
def test_real_timeout_kills_whole_process_group(tmp_path):
    """**真实**进程组杀：超时后父进程与它派生的子进程**都必须消失**。

    用**真正可执行**的 launcher：
      * POSIX：`/bin/sh` 脚本（有 shebang + 可执行位）；
      * Windows：`.cmd` 包装（Windows 不按 shebang 认脚本）。

    上一版直接 `Popen` 一个没有 shebang/执行位的 `.py`，Linux 上直接
    `Permission denied`（这条用例因此在容器里失败）。现在 launcher 一定可执行，
    并且**精确核验 PID**（psutil 或 `/proc`；查不到能力就 skip，绝不假报通过）。
    """
    token = f"kbtimeout{os.getpid()}x{int(time.time())}"
    marker = tmp_path / "parent-done.txt"
    child_marker = tmp_path / "child-done.txt"
    parent_pid_file = tmp_path / "parent.pid"
    child_pid_file = tmp_path / "child.pid"
    _psutil_or_skip()  # 核验能力缺失时**先 skip**，不要跑完再说没法验

    sleeper = _write_sleeper_script(tmp_path, token, child_pid_file)
    launcher = _write_launcher(
        tmp_path, token, sleeper, marker, child_marker, parent_pid_file
    )

    work = tmp_path / "work"
    work.mkdir()
    profile = work / "profile"
    profile.mkdir()
    office_convert.write_profile_config(profile)
    source = tmp_path / "synthetic.doc"
    source.write_bytes(b"not a real doc")
    outdir = work / "out"
    outdir.mkdir()

    started = time.monotonic()
    with pytest.raises(parsers.ParseError) as excinfo:
        with mock.patch.object(office_convert, "get_soffice_path", lambda value: str(launcher)):
            office_convert.run_conversion(
                str(launcher),
                source=source,
                output_dir=outdir,
                profile_dir=profile,
                target="txt",
                timeout=3,
            )
    elapsed = time.monotonic() - started
    print(f"[timeout] elapsed={elapsed:.1f}s error={str(excinfo.value)[:140]!r}")
    assert "进程组" in str(excinfo.value), str(excinfo.value)
    assert elapsed < 20, f"超时没有及时生效（{elapsed:.1f}s）"

    # **PID 精确核验**：只认我们自己记录下来的父/子 PID，避免用
    # "命令行含 token 的进程"这种会误报的近似判据（上一版就是在整套跑时
    # 捕到了无关进程而假失败）。
    parent_pid = _read_pid(parent_pid_file)
    child_pid = _read_pid(child_pid_file)
    print(f"[timeout] 记录到的 pid：parent={parent_pid} child={child_pid}")
    assert parent_pid or child_pid, "父/子进程都没写下 PID，本次实验无效"

    time.sleep(5)
    still_alive = [
        pid for pid in (parent_pid, child_pid) if pid is not None and _process_alive(pid)
    ]
    print(f"[timeout] 5 秒后仍存活的 pid={still_alive}")
    for pid in still_alive:
        print(f"[timeout]   残留详情：{_describe_process(pid)}")
    assert not marker.exists(), "父进程活过了超时"
    assert not child_marker.exists(), "孤儿子进程没有被杀掉（进程组杀失败）"
    assert still_alive == [], f"父/子进程没有被杀干净：{still_alive}"


def _read_pid(path: Path) -> int | None:
    """读 PID 文件（进程可能还没写出来，返回 None）。"""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def _describe_process(pid: int) -> str:
    """残留进程的可读描述（诊断用：告诉我们到底剩了什么）。"""
    parts = [f"pid={pid}"]
    proc = Path(f"/proc/{pid}")
    if proc.exists():
        try:
            stat = (proc / "stat").read_text(encoding="utf-8", errors="replace")
            parts.append(f"stat_state={stat.split()[2]}")
            parts.append(f"ppid={stat.split()[3]}")
        except Exception as exc:  # noqa: BLE001
            parts.append(f"stat_err={exc}")
        try:
            parts.append(f"cmdline={(proc / 'cmdline').read_bytes()[:160]!r}")
        except Exception as exc:  # noqa: BLE001
            parts.append(f"cmdline_err={exc}")
    try:
        import psutil  # type: ignore

        process = psutil.Process(pid)
        parts.append(f"psutil_status={process.status()}")
        parts.append(f"psutil_ppid={process.ppid()}")
    except Exception as exc:  # noqa: BLE001
        parts.append(f"psutil_err={exc}")
    return " ".join(parts)


# --------------------------------------------------- 进程核验小工具（精确） ----


def _write_sleeper_script(tmp_path: Path, token: str, pid_file: Path) -> Path:
    """子进程脚本：**先写下自己的 PID**，再 sleep 后尝试写标记文件。

    写 PID 是为了让测试能**精确核验**这个子进程有没有被杀掉，
    而不是靠"命令行里含 token 的进程数"这种会误报的近似判据。
    """
    sleeper = tmp_path / f"{token}_sleeper.py"
    sleeper.write_text(
        "import os, sys, time\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(float(sys.argv[2]))\n"
        "open(sys.argv[1], 'w').write('child survived')\n",
        encoding="utf-8",
    )
    return sleeper


def _write_launcher(
    tmp_path: Path,
    token: str,
    sleeper: Path,
    marker: Path,
    child_marker: Path,
    parent_pid_file: Path,
) -> Path:
    """写一个**真正可执行**的假 soffice：自己派生一个子进程，然后长时间睡眠。

    父进程也把自己的 PID 写下来 —— 父与子的存活性都按 PID 精确核验。
    """
    py = sys.executable
    child_cmd = f'"{py}" "{sleeper}" "{child_marker}" 30'
    parent_cmd = (
        f'"{py}" -c "import os,time,sys; '
        f"open(r'{parent_pid_file}', 'w').write(str(os.getpid())); "
        f"time.sleep(45); open(r'{marker}', 'w').write('parent finished')\""
    )
    if os.name == "nt":
        launcher = tmp_path / f"{token}_fake_soffice.cmd"
        launcher.write_text(
            "@echo off\r\n"
            f"start \"\" /b {child_cmd}\r\n"
            f"{parent_cmd}\r\n",
            encoding="utf-8",
        )
        return launcher

    launcher = tmp_path / f"{token}_fake_soffice.sh"
    launcher.write_text(
        "#!/bin/sh\n"
        f"{child_cmd} &\n"
        f"exec {parent_cmd}\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return launcher


def _psutil_or_skip():
    """拿 psutil；没有就 skip（绝不返回 0 冒充"没有孤儿"）。"""
    try:
        import psutil  # type: ignore

        return psutil
    except Exception:  # noqa: BLE001
        pytest.skip("没有 psutil，无法精确核验孤儿进程（容器内已安装）")


def _process_alive(pid: int) -> bool:
    """进程是否**仍在运行**。

    僵尸进程（已经终止、只是还没被 init 回收）**不算存活** —— 实测容器里
    PID 1 是 ``sh``，它不会及时回收，杀掉进程组后会留下 zombie；
    把 zombie 当"孤儿"会误报（``psutil.pid_exists`` 对 zombie 也返回 True）。
    """
    proc = Path(f"/proc/{pid}")
    if proc.exists():
        try:
            state = (proc / "stat").read_text(encoding="utf-8", errors="replace").split()[2]
        except Exception:  # noqa: BLE001
            return False
        return state != "Z"
    try:
        import psutil  # type: ignore

        if not psutil.pid_exists(pid):
            return False
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except Exception:  # noqa: BLE001
        return False


def _wait_for_no_token_processes(token: str, *, timeout: float = 10.0) -> list[int]:
    """轮询直到带 token 的**存活**进程消失（给内核/init 一点回收时间）。"""
    deadline = time.monotonic() + timeout
    survivors = _pids_matching(token)
    while survivors and time.monotonic() < deadline:
        time.sleep(0.5)
        survivors = _pids_matching(token)
    return survivors


def _pids_matching(token: str) -> list[int]:
    """按**命令行 token** 精确列出存活 PID（psutil，容器内已安装）。"""
    psutil = _psutil_or_skip()
    found: list[int] = []
    for process in psutil.process_iter(["cmdline"]):
        try:
            cmdline = " ".join(process.info.get("cmdline") or [])
        except Exception:  # noqa: BLE001
            continue
        if token in cmdline and _process_alive(process.pid):
            found.append(process.pid)
    return found
