"""进程探活的**安全性**与**身份**用例（隔离、无害、不涉业务服务）。

背景：我曾用 `os.kill(pid, 0)` 做"存在性探测" —— 在 Windows 上这会
**终止进程**（Python 3.13 除 CTRL_C/BREAK 外任何信号都调 TerminateProcess）。
已改为 `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` 只读查询。

这些用例锁死三件事：
  * 探活**绝不终止**目标进程（调用前后都存活）；
  * 身份令牌（创建时间）能识别 PID 复用；
  * 未知/非法输入**保守视为存活**（宁可拒绝清理，不可误删）。
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_resources as rr  # noqa: E402


@pytest.fixture
def harmless_process():
    """本次创建的无害子进程（隔离验证用，绝不用业务服务）。"""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    time.sleep(1.0)
    yield proc
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=15)


def test_probe_does_not_kill_the_target(harmless_process):
    """**核心安全用例**：探活调用前后，目标进程必须仍然存活。"""
    pid = harmless_process.pid
    before = harmless_process.poll() is None
    alive = rr.pid_is_alive_readonly(pid)
    after = harmless_process.poll() is None

    assert before is True
    assert alive is True, "存活进程被误判为死"
    assert after is True, "*** 探活终止了目标进程（危险行为回归）***"
    time.sleep(0.5)
    assert harmless_process.poll() is None, "探活后进程消失"


def test_probe_reports_dead_after_exit(harmless_process):
    pid = harmless_process.pid
    harmless_process.kill()
    harmless_process.wait(timeout=15)
    time.sleep(0.5)
    assert rr.pid_is_alive_readonly(pid) is False


def test_start_token_is_stable_and_distinguishes_pids(harmless_process):
    """创建时间令牌：同一进程稳定；不同进程可区分（PID 复用防护的基础）。"""
    pid = harmless_process.pid
    first = rr.pid_start_token(pid)
    second = rr.pid_start_token(pid)
    if first is None:      # 非 Windows 或查询不可用
        pytest.skip("该平台不支持创建时间令牌")
    assert second is not None
    assert abs(first - second) < 0.001, "同一进程的令牌应稳定"
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        time.sleep(1.0)
        token_other = rr.pid_start_token(other.pid)
        assert token_other is not None
        assert abs(token_other - first) > 0.001, "不同进程的令牌应可区分"
    finally:
        other.kill()
        other.wait(timeout=15)


def test_unknown_and_invalid_inputs_are_treated_as_alive():
    """保守原则：未知/非法一律视为存活（宁可拒绝清理，不可误删）。"""
    assert rr.pid_is_alive_readonly(-1) is True
    assert rr.pid_is_alive_readonly("not-a-pid") is True  # pyright: ignore[reportArgumentType]
    assert rr.pid_is_alive_readonly(0) is True


def test_recover_refuses_when_recorded_pid_is_alive(runs_dir, harmless_process):
    """身份校验：清单记录的服务 PID 仍存活 → **拒绝清理**（force 也不绕过）。"""
    res = rr.RunResources.new(
        endpoint="http://127.0.0.1:56339", identity="probe-check", runs_dir=runs_dir
    )
    res.set_meta(service_pid=harmless_process.pid,
                 service_pid_start=rr.pid_start_token(harmless_process.pid))
    res.release()

    for force in (False, True):
        with pytest.raises(rr.CleanupError) as exc:
            rr.recover(res.run_uuid, runs_dir=runs_dir, force=force)
        assert "仍存活" in str(exc.value) or "复用" in str(exc.value)
    assert harmless_process.poll() is None, "*** 拒绝路径也不得终止目标进程 ***"


def test_recover_refuses_when_pid_was_reused(runs_dir, harmless_process, monkeypatch):
    """PID 复用：登记令牌与当前不符 → 拒绝清理（保守，不误判成我们的服务）。

    **断言语义核对**：本用例要证明的是 ``recover`` 在"这个 PID 现在被**别的**
    进程占用"时的行为，而不是"PID 还活着"。旧写法（真实进程 + 登记令牌
    ``1.0``）在 Linux 上根本进不了这条分支：``pid_start_token()`` 在非 Windows
    上**恒返回 None**，于是跳过令牌比对、落到兜底的"仍存活"分支 —— 实测报错
    正是这句"服务 PID ... 仍存活"，不是产品缺陷，而是用例没能确定命中目标分支。

    **本用例构造的场景为什么能确定命中"复用"分支**：
    "PID 被复用"的定义就是"该 PID 现在被占用（存活为真）+ 占用者的创建时间与
    登记值不同"。真实系统里无法可控地制造 PID 回收再分配，所以这里把
    ``recover()`` 真正依赖的两个**只读探针**直接喂成这组确定值：
      * ``pid_is_alive_readonly`` → True（PID 确实被占着）；
      * ``pid_start_token`` → 登记值 + 3600s（占用者不是当初那个进程）。
    两者都为真 ⇒ ``recover`` 必然走 `pid_alive and recorded` 且令牌不符的
    分支。断言盯住**行为**：拒绝、报"复用"、不进清理、不动那个进程/目录。
    """
    recorded_start = 1.0
    res = rr.RunResources.new(
        endpoint="http://127.0.0.1:56339", identity="probe-check", runs_dir=runs_dir
    )
    # 故意登记一个**错误**的创建时间令牌（模拟"这个 PID 现在是别的进程"）
    res.set_meta(service_pid=harmless_process.pid, service_pid_start=recorded_start)
    manifest = res.manifest
    res.release()

    monkeypatch.setattr(rr, "pid_is_alive_readonly", lambda pid: True)
    monkeypatch.setattr(
        rr, "pid_start_token", lambda pid: float(recorded_start) + 3600.0
    )

    cleaned: list[str] = []

    def spy_cleanup(*_a, **_k):
        cleaned.append("cleanup")
        raise AssertionError("命中「复用」分支时不得继续执行清理")

    monkeypatch.setattr(rr.RunResources, "cleanup", spy_cleanup)

    with pytest.raises(rr.CleanupError) as exc:
        rr.recover(res.run_uuid, runs_dir=runs_dir)

    message = str(exc.value)
    assert "复用" in message, message
    # 命中的必须**是**复用分支，不是兜底的"仍存活"分支（旧用例的假通过点）
    assert "仍存活" not in message, f"命中的是兜底存活分支，不是复用分支：{message}"
    # 行为：确实拒绝清理（没走到 cleanup、清单仍留痕在原地）
    assert cleaned == [], "拒绝路径竟然继续执行了清理"
    assert manifest.exists(), "拒绝清理时不得删除 run 清单"
    # 保护不变：拒绝路径不得终止那个 PID 上的进程
    assert harmless_process.poll() is None, "*** 拒绝路径也不得终止目标进程 ***"


@pytest.fixture
def runs_dir():
    """会话唯一 + 每次唯一（与其他清理用例同一约定）。"""
    import os
    import shutil
    import uuid

    allowed = (ROOT / "artifacts" / "test-runs"
               / os.environ.get("KB_TEST_RUN_UUID", "nosession") / "fixtures")
    d = allowed / f"probe-{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    real, root = Path(os.path.realpath(d)), Path(os.path.realpath(allowed))
    if real == root or os.path.commonpath([str(real), str(root)]) != str(root):
        raise AssertionError(f"拒绝删除越界路径：{real}")
    shutil.rmtree(real)
