"""真实 LibreOffice 是否**采纳**隔离 profile 的安全键（容器内闭环，防回退）。

为什么需要这条：`office_convert.assert_profile_hardened` 只能证明"我们写出去的
XCU 内容是对的"，**不能**证明 LibreOffice 真的收下了它。2026-10-16 用真实转换
闭环过一次（读 LO 退出时写回的 `registrymodifications.xcu`），本用例把结论**固化**：

  1. 用产品的 `write_profile_config()` 写 profile（与生产完全同形）；
  2. 用产品的 `SOFFICE_FLAGS` + `-env:UserInstallation` 跑一次**真实转换**；
  3. 解析 **LibreOffice 写回**的那份 XCU，要求 7 个键都以我们的**非默认值**出现。

判定口径（非默认值很关键）：`DisableMacrosExecution=true`（默认 false）、
`MacroSecurityLevel=3`（默认 2）、`CheckDocumentEvents=false`（默认 true）……
若 LO 丢弃我们的键、或写回它自己的默认值，断言就会失败。

宿主（Windows）上本机 LibreOffice 连 `--version` 都会超时（已记录），所以这里
**如实跳过并写明原因**；在 Linux 交付镜像内它是真实断言。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from apps.knowledge import office_convert as oc

#: 我们设的值必须是"非默认值"，否则"写回默认值"也会看起来正确
EXPECTED = {
    "DisableMacrosExecution": "true",
    "MacroSecurityLevel": "3",
    "DisableActiveContent": "true",
    "DisableOLEAutomation": "true",
    "DisablePythonRuntime": "true",
    "BlockUntrustedRefererLinks": "true",
    "CheckDocumentEvents": "false",
}
SECURITY_PATH = "org.openoffice.Office.Common/Security/Scripting"


def _working_soffice() -> str | None:
    """返回**能启动**的 soffice；宿主上那个连 --version 都超时的会被判为不可用。"""
    binary = oc.find_soffice() or shutil.which("soffice")
    if not binary:
        return None
    try:
        done = subprocess.run([binary, "--version"], capture_output=True, timeout=15, check=False)
    except Exception:  # noqa: BLE001 - 超时/无法执行都算"本机不可用"
        return None
    return binary if done.returncode == 0 else None


def _read_lo_written(xcu: Path) -> dict[tuple[str, str], str]:
    """解析 LibreOffice 写回的 XCU；**路径归一化**去掉前导斜杠（LO 会带）。"""
    out: dict[tuple[str, str], str] = {}
    for line in xcu.read_text(encoding="utf-8", errors="replace").splitlines():
        item = re.search(r'<item oor:path="([^"]+)">', line)
        if not item:
            continue
        for prop in re.finditer(
            r'<prop oor:name="([^"]+)"[^>]*>(?:<value>([^<]*)</value>)?', line
        ):
            out[(item.group(1).lstrip("/"), prop.group(1))] = prop.group(2) or ""
    return out


def test_libreoffice_adopts_profile_security_keys():
    binary = _working_soffice()
    if not binary:
        pytest.skip(
            "本机 LibreOffice 不可用（宿主实测连 --version 都超时）："
            "该闭环需在 Linux 交付镜像内运行（kb-app:delivery 内已实测通过）"
        )

    with tempfile.TemporaryDirectory(prefix="kb-adopt-test-") as tmp:
        work = Path(tmp)
        profile = work / "profile"
        # 与生产完全同形：只写 schema 里真实存在的键
        oc.write_profile_config(profile)
        source = work / "in.txt"
        source.write_text("adoption probe", encoding="utf-8")
        outdir = work / "out"
        outdir.mkdir()
        done = subprocess.run(
            [
                binary,
                *oc.SOFFICE_FLAGS,
                f"-env:UserInstallation={profile.resolve().as_uri()}",
                "--convert-to",
                "docx",
                "--outdir",
                str(outdir),
                str(source),
            ],
            capture_output=True,
            timeout=300,
            check=False,
        )
        assert done.returncode == 0, done.stderr.decode("utf-8", "replace")[:400]
        assert list(outdir.glob("*.docx")), "LibreOffice 没有产出任何文件"

        xcu = profile / "user" / "registrymodifications.xcu"
        assert xcu.is_file(), "LibreOffice 没有写回 registrymodifications.xcu"
        observed = _read_lo_written(xcu)

    missing = {
        name: observed.get((SECURITY_PATH, name))
        for name, expected in EXPECTED.items()
        if observed.get((SECURITY_PATH, name)) != expected
    }
    assert not missing, (
        "LibreOffice 没有采纳这些安全键（加固可能失效）："
        f"{missing}；解析到的条目数={len(observed)}"
    )
    # 反例控制：schema 里不存在的键不应出现（证明解析不是"什么都算采纳"）
    assert observed.get((SECURITY_PATH, "RecommendedMacrosSecurityLevel")) is None
    assert observed.get(("org.openoffice.Office.Common/LinkUpdate", "BlockUntrustedRefererLinks")) is None


if __name__ == "__main__":  # pragma: no cover - 手工跑
    sys.exit(pytest.main([__file__, "-q"]))
