"""清理只读目录树的**跨平台**回归。

真实缺陷（Linux uid 10001 的临时容器复现）：
`_remove_tree_owned` 用 `chmod(S_IWRITE | S_IREAD)`（**赋值**，等于 0600）
给**所有**条目降权 —— 目录因此丢掉 **x（进入）位**，Linux 上连枚举/删除都做不了，
`raw/.tmp` 与 `vector_data/embedded` 直接 `PermissionError`。
Windows 上碰巧能过，所以这个 bug 只会在 Linux 暴露。

修好的口径（本用例锁死）：
  * **目录**：保留并补上 owner 的 **rwx**；
  * **文件**：保留并补上 owner 的 **rw**；
  * **只加位、绝不清位**（组/其他/粘滞位保持原样）；
  * 不跟随符号链接（链接一律拒绝删除）。
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import run_resources as rr  # noqa: E402


class _FakeRun:
    """只为拿到 `_deletable_mode` / `_remove_tree_owned`（不建真实 run）。"""

    _deletable_mode = staticmethod(rr.RunResources._deletable_mode)
    _remove_tree_owned = rr.RunResources._remove_tree_owned


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path, follow_symlinks=False).st_mode)


# --------------------------------------------------- 纯函数：权限位计算 ----


def test_directory_keeps_execute_bit(tmp_path):
    """**核心**：目录必须保留/获得 owner 的 x 位，否则 Linux 上无法进入与删除。"""
    directory = tmp_path / "readonly-dir"
    directory.mkdir()
    os.chmod(directory, 0o500)          # r-x（有 x，但不可写）
    new_mode = _FakeRun._deletable_mode(directory)
    assert new_mode & stat.S_IXUSR, "目录丢掉了 owner 执行位（Linux 会 PermissionError）"
    assert new_mode & stat.S_IWUSR, "目录未获得 owner 写位"
    assert new_mode & stat.S_IRUSR


def test_directory_mode_preserves_other_bits(tmp_path):
    """**只加位、不清位**：组/其他的读位不能被清掉。"""
    directory = tmp_path / "shared-dir"
    directory.mkdir()
    os.chmod(directory, 0o555)          # r-xr-xr-x
    new_mode = _FakeRun._deletable_mode(directory)
    assert new_mode & stat.S_IRGRP, "组读位被清掉了（不应破坏性清零）"
    assert new_mode & stat.S_IROTH, "其他读位被清掉了"
    assert new_mode & 0o700 == 0o700


def test_file_gains_owner_rw_and_keeps_others(tmp_path):
    target = tmp_path / "readonly.bin"
    target.write_bytes(b"x")
    os.chmod(target, 0o444)             # r--r--r--
    new_mode = _FakeRun._deletable_mode(target)
    assert new_mode & stat.S_IWUSR and new_mode & stat.S_IRUSR
    assert new_mode & stat.S_IRGRP and new_mode & stat.S_IROTH, "其他权限位被清掉"
    assert not (new_mode & stat.S_IXUSR), "文件不该被加上执行位"


# ----------------------------------------------- 集成：真实只读目录树 ----

@pytest.mark.skipif(os.name != "posix", reason="Linux 权限模型（Windows 无 POSIX 权限位）")
def test_remove_readonly_tree_on_linux(tmp_path):
    """Linux：整棵只读目录树（目录 0500 / 文件 0400）必须能被清掉。"""
    root = tmp_path / "vector_data"
    embedded = root / "embedded"
    embedded.mkdir(parents=True)
    (embedded / "blob.bin").write_bytes(b"immutable")
    (root / ".tmp").mkdir()
    (root / ".tmp" / "part.bin").write_bytes(b"partial")

    os.chmod(embedded / "blob.bin", 0o400)
    os.chmod(root / ".tmp" / "part.bin", 0o400)
    os.chmod(embedded, 0o500)           # 不可写、可进入
    os.chmod(root / ".tmp", 0o500)
    os.chmod(root, 0o500)

    _FakeRun()._remove_tree_owned(root)
    assert not root.exists(), "只读目录树未能删除"


@pytest.mark.skipif(os.name != "posix", reason="Linux 权限模型")
def test_remove_tree_rejects_symlink_and_does_not_follow(tmp_path):
    """树下含符号链接 → **拒绝删除**，且绝不跟随到外部目标。"""
    outside = tmp_path / "outside.txt"
    outside.write_text("SECRET", encoding="utf-8")
    root = tmp_path / "run-dir"
    root.mkdir()
    link = root / "evil-link"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("本环境无法创建符号链接")

    with pytest.raises(rr.CleanupError) as exc:
        _FakeRun()._remove_tree_owned(root)
    assert "链接" in str(exc.value)
    assert outside.is_file() and outside.read_text(encoding="utf-8") == "SECRET", (
        "外部目标被跟随/破坏了"
    )


def test_windows_readonly_file_is_deletable():
    """Windows：只读文件同样要能被删除（原缺陷的动机场景）。"""
    if os.name != "nt":
        pytest.skip("仅 Windows 关注只读位")
    import tempfile

    with tempfile.TemporaryDirectory() as base:
        root = Path(base) / "tree"
        root.mkdir()
        target = root / "readonly.bin"
        target.write_bytes(b"x")
        os.chmod(target, stat.S_IREAD)      # 只读
        _FakeRun()._remove_tree_owned(root)
        assert not root.exists()
