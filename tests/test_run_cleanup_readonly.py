"""专项：`storage.store_bytes` 写的**只读**原件必须能被清理掉。

背景（独立复验发现的真实 bug）：`store_bytes` 按不可变语义把原件写成**只读**
文件；Windows 上 `shutil.rmtree` 删只读文件会 `PermissionError [WinError 5]`，
导致 session teardown 失败（业务断言其实全过）。

这里用**真实** `storage.store_bytes` 写出只读原件（不是手造假文件），
把 raw 根指向本 run 目录（沿用仓内既有技法 `monkeypatch.setattr(paths, ...)`），
再走 `RunResources.cleanup` 的真实删除路径，断言删干净且 `failed == []`。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_resources as rr  # noqa: E402


@pytest.fixture
def runs_dir():
    """会话唯一 + 每次唯一（避免跨会话复用/互删）；清理带真实包含校验。"""
    import shutil as _sh
    import uuid as _uuid

    allowed = ROOT / "artifacts" / "test-runs" / os.environ.get("KB_TEST_RUN_UUID", "nosession") / "fixtures"
    d = allowed / f"ro-{_uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    real, root = Path(os.path.realpath(d)), Path(os.path.realpath(allowed))
    if real == root or os.path.commonpath([str(real), str(root)]) != str(root):
        raise AssertionError(f"拒绝删除越界路径：{real}")
    # **不再用重试遮盖**：所有 owner 由 autouse `_auto_release_runs` 在 teardown
    # 无条件 release，锁句柄理应已释放。若仍失败，就让它**直接暴露**。
    _sh.rmtree(real)


def test_readonly_immutable_files_are_cleaned(runs_dir, monkeypatch):
    """真实 `store_bytes` 写只读原件 → cleanup 必须删干净、不报失败。"""
    from config import paths

    from apps.knowledge import storage

    # **先建 run**：raw 必须落在 run 目录**之内**（allowed_roots 就是 run 自身），
    # 否则会被包含校验正当拒绝（上一版就是这样错的）。
    res = rr.RunResources.new(endpoint="", identity="readonly-test", runs_dir=runs_dir)
    raw_dir = res.dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    # 把 raw 根指到本 run 目录（仓内既有技法，避免写主 media/raw）
    monkeypatch.setattr(paths, "raw_store_dir", lambda: raw_dir)

    blob = storage.store_bytes(b"immutable-original-bytes" * 64)
    sha = getattr(blob, "sha256", None) or str(blob)
    path = raw_dir / sha[:2] / sha
    assert path.exists(), f"store_bytes 应写出 {path}（实际 {blob}）"
    # 确认它**确实是只读**的，否则这个用例失去意义
    mode = os.stat(path).st_mode
    assert not (mode & 0o200), f"store_bytes 应写只读文件，当前 mode={oct(mode)}"

    res.declare("dir", str(raw_dir))
    res.confirm("dir", str(raw_dir))

    try:
        report = res.cleanup()      # 真实删除路径（含清只读位）
    finally:
        # **必须 release**：Windows 上未释放的锁句柄会让父夹具的 rmtree 悄悄失败
        res.release()
    assert report["failed"] == [], f"清理不该失败：{report['failed']}"
    assert not raw_dir.exists(), "只读原件所在目录应被完全删除"


def test_readonly_cleanup_failure_still_raises(runs_dir, monkeypatch):
    """清理真失败时仍必须抛错（**不可** ignore_errors）。

    注意：patch 的是本模块的 `_remove_tree_owned`，**不要** patch 全局
    `shutil.rmtree` —— 那会连 fixture 自己的清理一起打坏（上轮踩过）。
    """
    res = rr.RunResources.new(endpoint="", identity="readonly-test", runs_dir=runs_dir)
    raw_dir = res.dir / "raw2"      # 必须在 run 目录**之内**
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "x.bin").write_bytes(b"x")

    res.declare("dir", str(raw_dir))
    res.confirm("dir", str(raw_dir))

    def boom(self, target):  # noqa: ANN001
        raise PermissionError("模拟无法删除")

    monkeypatch.setattr(rr.RunResources, "_remove_tree_owned", boom)
    try:
        with pytest.raises(rr.CleanupError) as exc:
            res.cleanup()
    finally:
        res.release()               # 同上：一定释放锁
    assert "PermissionError" in str(exc.value) or "无法删除" in str(exc.value)
