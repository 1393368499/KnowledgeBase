"""跨进程存活判定的**真实证据**（不是同进程自证）。

流程：
  1 起一个**子进程**持有该 run 的 OS 锁；
  2 主进程 `is_alive()` 必须为 True（锁被别人拿着）；
  3 `recover(force=True)` **必须拒绝** —— force 不能绕过真正在跑的 run；
  4 杀掉子进程（OS 自动释放锁）；
  5 主进程 `is_alive()` 必须转为 False；
  6 `recover()` 成功，且**只**删本 run 登记的那个集合。

没有真实 Qdrant 时，第 6 步用假客户端（只删本 run 的 key），其余步骤全部真实。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_resources as rr  # noqa: E402

HOLDER = ROOT / "scripts" / "lock_holder.py"


def _utf8_env() -> dict:
    """子进程强制 UTF-8（Windows 默认 GBK 会让中文断言误判）。"""
    env = dict(os.environ)
    env["KB_SCRIPTS_DIR"] = str(ROOT / "scripts")
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


@pytest.fixture
def runs_dir():
    """本文件自己的 runs 目录：**会话唯一 + 每次唯一**（子进程也要能访问，故用真实路径）。

    以前是 `artifacts/test-runs/xproc-<test_name>` —— 跨 pytest 会话同名会复用/
    互删。现在放本 session 的 fixtures 之下再叠加 UUID，并用**真实包含校验**清理
    （不用 `ignore_errors` 吞错）。

    注意：**不能 rom conftest import ...** —— pytest 的 conftest 不是可导入模块
    （实测 ModuleNotFoundError）。所以这里内联，不跨模块依赖。
    """
    import shutil as _sh
    import uuid as _uuid

    allowed = ROOT / "artifacts" / "test-runs" / os.environ.get("KB_TEST_RUN_UUID", "nosession") / "fixtures"
    d = allowed / f"xproc-{_uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    real, root = Path(os.path.realpath(d)), Path(os.path.realpath(allowed))
    if real == root or os.path.commonpath([str(real), str(root)]) != str(root):
        raise AssertionError(f"拒绝删除越界路径：{real}")
    # **显式等待 owner 释放**（真实缺陷：CLI 的 `recover` 在同一进程里打开过 `run.lock`，
    # Windows 上文件句柄是**异步**释放的 —— teardown 立刻 rmtree 会撞 WinError 32）。
    # 这里**不是** `ignore_errors`，也**不放宽断言**：只是把"等 owner 真正放开"写成
    # 显式的有界等待；若始终无法释放，仍然**失败并给出明确原因**。
    import gc as _gc
    import time as _time

    _gc.collect()  # 先回收本进程里可能仍持有句柄的对象
    deadline = _time.time() + 15
    while True:
        try:
            _sh.rmtree(real)
            break
        except PermissionError as exc:
            if _time.time() >= deadline:
                raise AssertionError(
                    f"runs 目录在 15s 内仍被占用（owner 未释放锁）：{real} —— {exc}"
                ) from exc
            _gc.collect()
            _time.sleep(0.25)


class FakeQdrant:
    def __init__(self, existing: set[str],
                 endpoint: str = "http://127.0.0.1:56339") -> None:
        self.existing = set(existing)
        self.deleted: list[str] = []
        self._client = type("C", (), {"rest_uri": endpoint})()

    def collection_exists(self, name: str) -> bool:
        return name in self.existing

    def get_collections(self):
        """补齐接口：pending 协调/前缀清点需要它（不可为测试绿而跳过）。"""
        item = type("C", (), {})
        return type("R", (), {"collections": [
            type("I", (), {"name": n})() for n in sorted(self.existing)
        ]})()

    def delete_collection(self, collection_name: str) -> None:
        self.existing.discard(collection_name)
        self.deleted.append(collection_name)


@pytest.fixture
def holder(runs_dir):
    """起子进程持锁，返回 (proc, run_uuid, 集合名)；测试结束确保杀干净。"""
    run_uuid = uuid.uuid4().hex[:12]
    marker = runs_dir / f"{run_uuid}.ready"
    runs_dir.mkdir(parents=True, exist_ok=True)
    # 子进程统一 UTF-8：Windows 默认 GBK 会让中文断言误判（编码误差 ≠ 产品缺陷）
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(HOLDER), run_uuid, str(runs_dir), str(marker)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_utf8_env(),
    )
    deadline = time.time() + 60
    while time.time() < deadline:
        if marker.exists():
            break
        if proc.poll() is not None:
            out, err = proc.communicate()
            pytest.fail(f"持锁子进程提前退出：{err.decode('utf-8', 'replace')[-400:]}")
        time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail("持锁子进程未在 60s 内就绪")
    info = json.loads(marker.read_text(encoding="utf-8"))
    try:
        yield proc, run_uuid, info["collection"], runs_dir
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=20)


def test_cross_process_lock_marks_run_alive(holder):
    """**核心**：另一个进程持锁时，本进程必须判为存活。"""
    _proc, run_uuid, _name, runs_dir = holder
    res = rr.RunResources.open(run_uuid, runs_dir=runs_dir)
    assert res.is_alive() is True, "子进程仍持有锁，却被判为死亡"


def test_force_cannot_bypass_cross_process_alive_run(holder):
    """**核心**：force 不得绕过真正在跑的 run，且不得删它的集合。"""
    _proc, run_uuid, name, runs_dir = holder
    fake = FakeQdrant({name})
    with pytest.raises(rr.CleanupError) as exc:
        rr.recover(run_uuid, runs_dir=runs_dir, force=True, qdrant_client=fake)
    assert "跨进程锁" in str(exc.value) or "存活" in str(exc.value)
    assert fake.deleted == [], "活跃 run 的集合绝不能被 force 删掉"


def test_killed_process_releases_lock_and_allows_recovery(holder):
    """杀掉持有者后：判活转 False，且 recover 只删**本 run**的集合。"""
    proc, run_uuid, name, runs_dir = holder
    other = "kb-someone-else-keepme"

    proc.kill()
    proc.wait(timeout=20)
    # 等 OS 释放锁（进程退出即释放，这里给一点余量）
    deadline = time.time() + 30
    res = rr.RunResources.open(run_uuid, runs_dir=runs_dir)
    while time.time() < deadline and res.is_alive():
        time.sleep(0.3)
    assert res.is_alive() is False, "持有者已被杀，仍判为存活（OS 锁未释放？）"

    fake = FakeQdrant({name, other})
    report = rr.recover(run_uuid, runs_dir=runs_dir, qdrant_client=fake)
    assert fake.deleted == [name], f"应只删本 run 的集合，实际 {fake.deleted}"
    assert other in fake.existing, "别人的集合不能被碰"
    assert report["failed"] == []


def test_two_holders_cannot_own_same_run(runs_dir):
    """同一 run_uuid 不允许被两个进程同时占用（防止误判/双写清单）。"""
    run_uuid = uuid.uuid4().hex[:12]
    marker1 = runs_dir / f"{run_uuid}.h1"
    marker2 = runs_dir / f"{run_uuid}.h2"
    runs_dir.mkdir(parents=True, exist_ok=True)
    p1 = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(HOLDER), run_uuid, str(runs_dir), str(marker1)],
        env=_utf8_env(),
    )
    try:
        deadline = time.time() + 60
        while time.time() < deadline and not marker1.exists():
            if p1.poll() is not None:
                pytest.fail("第一个持锁进程提前退出")
            time.sleep(0.2)
        assert marker1.exists(), "第一个持锁进程未就绪"

        p2 = subprocess.Popen(
            [sys.executable, "-X", "utf8", str(HOLDER), run_uuid, str(runs_dir), str(marker2)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_utf8_env(),
        )
        _out, err = p2.communicate(timeout=60)
        assert p2.returncode != 0, "同一 run_uuid 竟被第二个进程占用成功"
        # 子进程已强制 UTF-8；这里再显式 utf-8 解码
        assert "已被另一个进程持有" in err.decode("utf-8", "replace")
        assert not marker2.exists(), "第二个进程不该走到就绪"
    finally:
        if p1.poll() is None:
            p1.kill()
            p1.wait(timeout=20)


# ------------------------------------- 活跃 run 保护：CLI 也不得绕过 --
RECOVER_ONCE = ROOT / "scripts" / "_recover_once.py"


def test_cli_cleanup_is_refused_while_lock_held(holder, monkeypatch, capsys):
    """**核心**：子进程持锁时，CLI `cleanup` 也必须被拒（不得绕过活跃保护）。"""
    _proc, run_uuid, name, runs_dir = holder
    fake = FakeQdrant({name})

    # CLI 会自建 client → 这里注入替身，保证密闭（不出网）
    real_cleanup = rr.RunResources._cleanup_locked

    def fake_cleanup(self, QdrantClient, qdrant_client, redis_client, sweep):  # noqa: ANN001
        return real_cleanup(self, lambda **kw: fake, qdrant_client, redis_client, sweep)

    monkeypatch.setattr(rr.RunResources, "_cleanup_locked", fake_cleanup)
    original = rr.RUNS_DIR
    rr.RUNS_DIR = runs_dir
    try:
        code = rr.main(["cleanup", "--uuid", run_uuid])
    finally:
        rr.RUNS_DIR = original

    err = capsys.readouterr().err
    assert code == 1, f"活跃 run 的 CLI cleanup 竟然成功（code={code}）"
    assert "正被另一个进程持有" in err or "活跃 run" in err
    assert fake.deleted == [], "活跃 run 的资源绝不能被删"


def test_cli_recover_is_refused_while_lock_held(holder, monkeypatch, capsys):
    """CLI `recover` 同样不得绕过（force 也不行）。"""
    _proc, run_uuid, name, runs_dir = holder
    fake = FakeQdrant({name})
    original = rr.RUNS_DIR
    rr.RUNS_DIR = runs_dir
    try:
        code = rr.main(["recover", "--uuid", run_uuid, "--force"])
    finally:
        rr.RUNS_DIR = original
    assert code == 1
    assert fake.deleted == []


def test_two_concurrent_recoverers_are_mutually_exclusive(runs_dir):
    """两个**恢复者并发**：**恰好一个成功**，另一个**明确因锁忙被拒**。

    上一版是假通过：fake 缺 `_client.rest_uri` → 两个都在端点校验处抛错，
    而断言 `ok <= 1` 允许 `ok == 0`；且 sleep 在 recover 返回后才开始，
    锁早已释放，根本没制造竞争。这里：

      * 完整 fake（带 `_client.rest_uri`，与登记 endpoint 一致）；
      * 在**持锁内部**（`_cleanup_locked` 里）写 marker 并短暂停留 → 真正的屏障；
      * 断言：`ok == 1` **且** `refused == 1`，且被拒那次的消息必须是"锁忙"；
        其它错误单独计数，出现即失败（不得把"端点校验失败"当拒绝）。
    """

    run_id = uuid.uuid4().hex[:12]
    res = rr.RunResources.new(
        endpoint="http://127.0.0.1:56339", identity="x", run_uuid=run_id, runs_dir=runs_dir
    )
    name = res.name("c")
    res.declare("qdrant_collection", name)
    res.confirm("qdrant_collection", name)
    res.release()                      # 模拟"进程已死"：OS 释放锁

    # **RECOVER_ONCE 放本夹具唯一目录**（全局固定文件会被两个 pytest 会话互相覆盖）
    helper_dir = runs_dir / f"recover-once-{uuid.uuid4().hex[:6]}"
    helper_dir.mkdir(parents=True, exist_ok=True)
    recover_once = helper_dir / "_recover_once.py"
    barrier = helper_dir / "in-cleanup.marker"
    recover_once.write_text(
        "import sys, time, json\n"
        "from pathlib import Path\n"
        "import os as _os\n"
        "sys.path.insert(0, _os.environ['KB_SCRIPTS_DIR'])\n"
        "import run_resources as rr\n"
        "\n"
        "class _Fake:\n"
        "    def __init__(self):\n"
        "        # 完整接口：交叉验证读的是 _client.rest_uri\n"
        "        self._client = type('C', (), {'rest_uri': 'http://127.0.0.1:56339'})()\n"
        "    def collection_exists(self, n): return False\n"
        "    def delete_collection(self, collection_name): pass\n"
        "    def get_collections(self):\n"
        "        return type('R', (), {'collections': []})()\n"
        "\n"
        "args = sys.argv[1:]\n"
        "run_id, runs_dir, barrier = args[0], Path(args[1]), Path(args[2])\n"
        "orig = rr.RunResources._cleanup_locked\n"
        "def slow(self, *a, **k):\n"
        "    # **在持锁内部**留痕并停留：这才是真正的屏障（recover 返回后再 sleep 无效）\n"
        "    barrier.write_text(json.dumps({'pid': 1}), encoding='utf-8')\n"
        "    time.sleep(2.0)\n"
        "    return orig(self, *a, **k)\n"
        "rr.RunResources._cleanup_locked = slow\n"
        "try:\n"
        "    rr.recover(run_id, runs_dir=runs_dir, qdrant_client=_Fake())\n"
        "    print('RECOVER_OK')\n"
        "except rr.CleanupError as exc:\n"
        "    print('RECOVER_ERR')\n"
        "    print(str(exc))\n",
        encoding="utf-8",
    )
    try:
        first = subprocess.Popen(
            [sys.executable, "-X", "utf8", str(recover_once), run_id, str(runs_dir), str(barrier)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", env=_utf8_env(),
        )
        # 等它**确实进入持锁区**，再起第二个恢复者
        deadline = time.time() + 60
        while time.time() < deadline and not barrier.exists():
            if first.poll() is not None:
                out, err = first.communicate()
                pytest.fail(f"第一个恢复者提前退出：{out}{err}")
            time.sleep(0.1)
        assert barrier.exists(), "第一个恢复者未进入持锁区（屏障未生效）"

        second = subprocess.Popen(
            [sys.executable, "-X", "utf8", str(recover_once), run_id, str(runs_dir), str(helper_dir / "b2")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", env=_utf8_env(),
        )
        first_out, _ = first.communicate(timeout=90)
        second_out, _ = second.communicate(timeout=90)
    finally:
        for proc in (locals().get("first"), locals().get("second")):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=20)

    ok1 = "RECOVER_OK" in first_out
    ok2 = "RECOVER_OK" in second_out
    refused = [o for o in (first_out, second_out) if "RECOVER_ERR" in o]

    assert ok1 + ok2 == 1, f"必须恰好一个成功，实际 first={ok1} second={ok2}\n{first_out}\n{second_out}"
    assert len(refused) == 1, f"另一个必须被明确拒绝\n{first_out}\n{second_out}"
    # **只有"锁忙"才算正当拒绝**；端点/身份等其它错误不得冒充
    assert "正被另一个进程持有" in refused[0] or "跨进程锁" in refused[0], (
        f"被拒原因必须是锁忙，实际：{refused[0][:200]}"
    )


# ------------------- 真实 pending 残留：create 不 confirm → kill → recover --
PENDING_HOLDER = ROOT / "scripts" / "pending_holder.py"


@pytest.mark.skipif(
    not os.environ.get("KB_TEST_QDRANT_URL"),
    reason="需要隔离端点（KB_TEST_QDRANT_URL）—— 真实集合才可复现",
)
def test_pending_collection_is_reconciled_after_kill(runs_dir):
    """**核心真实用例**：进程已建集合但死在 confirm 前 → recover 必须回收。

    旧行为：recover 调 cleanup(sweep_run_prefix=False)，pending 协调不执行、
    needs_client 又只看 created ⇒ 静默跳过**并返回成功**，集合永久残留。
    """
    import json as _json

    from qdrant_client import QdrantClient

    endpoint = os.environ["KB_TEST_QDRANT_URL"]
    run_id = uuid.uuid4().hex[:12]
    marker = runs_dir / f"{run_id}.pending"
    runs_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(PENDING_HOLDER), run_id, str(runs_dir), str(marker)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_utf8_env(),
    )
    try:
        deadline = time.time() + 60
        while time.time() < deadline and not marker.exists():
            if proc.poll() is not None:
                _o, err = proc.communicate()
                pytest.fail(f"pending 持有者提前退出：{err.decode('utf-8', 'replace')[-300:]}")
            time.sleep(0.2)
        assert marker.exists(), "pending 持有者未就绪"
        info = _json.loads(marker.read_text(encoding="utf-8"))
        name = info["collection"]
        raw_dir = Path(info["raw"])

        client = QdrantClient(url=endpoint, timeout=20)
        assert client.collection_exists(name), "前置：集合必须真的建出来了"
        assert raw_dir.exists(), "前置：raw 目录必须存在"

        # 模拟崩溃（**在 confirm 之前**被 kill）
        proc.kill()
        proc.wait(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=20)

    # 已死：recover 必须把它清掉（含 pending 协调 + 只读文件）
    report = rr.recover(run_id, runs_dir=runs_dir, qdrant_client=client)

    assert not client.collection_exists(name), "pending 集合未被回收（真实残留）"
    assert not raw_dir.exists(), "本 run raw 目录未被回收"
    assert report["failed"] == [], f"报告不得把未清项说成成功：{report['failed']}"
    left = [n for n in (c.name for c in client.get_collections().collections)
            if n.startswith(f"kb-{run_id}-")]
    assert left == [], f"本 run 前缀仍有残留：{left}"

@pytest.mark.skipif(
    not os.environ.get("KB_TEST_QDRANT_URL"),
    reason="需要隔离端点（KB_TEST_QDRANT_URL）",
)
def test_reconcile_pending_is_the_only_thing_that_can_promote_it(runs_dir):
    """**单独锁死 pending 协调路径**（不靠前缀清点代劳）。

    做法：直接观察只有 `reconcile_pending` 才能造成的清单状态跃迁 ——
    `pending` → `created` 且带上 `reconciled_reason`。
    这条断言在"关掉协调"时必然失败（已用反向对照验证）。
    """
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, VectorParams

    endpoint = os.environ["KB_TEST_QDRANT_URL"]
    client = QdrantClient(url=endpoint, timeout=20)
    run_id = uuid.uuid4().hex[:12]
    res = rr.RunResources.new(endpoint=endpoint, identity="reconcile-only",
                              run_uuid=run_id, runs_dir=runs_dir)
    try:
        name = res.name("recon")
        # **只 declare**：模拟"建出来了但没来得及 confirm"
        res.declare("qdrant_collection", name)
        client.create_collection(name, vectors_config=VectorParams(size=4, distance=Distance.COSINE))
        assert client.collection_exists(name)

        before = {e["key"]: e["status"] for e in res._read()["entries"]}
        assert before[name] == "pending", f"前置：应为 pending，实际 {before}"

        promoted = res.reconcile_pending(client)

        assert name in promoted, f"协调未发现已存在的 pending 集合：{promoted}"
        entry = next(e for e in res._read()["entries"] if e["key"] == name)
        assert entry["status"] == "created", f"状态未跃迁：{entry}"
        assert entry.get("reconciled_reason"), "缺少 reconciled_reason（无审计痕迹）"
        # 协调本身只登记，不删除；删由后续 cleanup 负责 —— 这里显式清掉
        client.delete_collection(name)
    finally:
        client.delete_collection(name) if client.collection_exists(name) else None
        res.release()

def test_pending_directory_is_reconciled_and_deleted(runs_dir):
    """**目录型 pending**：进程已建出目录但没 confirm → 协调后必须被清掉。

    旧实现只协调集合型 pending ⇒ 这类目录永久残留（recover 还会报成功）。
    """
    res = rr.RunResources.new(endpoint="", identity="path-pending", runs_dir=runs_dir)
    try:
        raw = res.dir / "raw"
        # **只 declare**（不 confirm）—— 但目录其实已经建出来了
        res.declare("dir", str(raw))
        raw.mkdir(parents=True, exist_ok=True)
        (raw / "blob.bin").write_bytes(b"x" * 512)

        before = {e["key"]: e["status"] for e in res._read()["entries"]}
        assert before[str(raw)] == "pending"

        promoted = res._reconcile_pending_paths([str(raw)])
        assert promoted == [str(raw)], f"目录型 pending 未被协调：{promoted}"
        entry = next(e for e in res._read()["entries"] if e["key"] == str(raw))
        assert entry["status"] == "created" and entry.get("reconciled_reason")

        report = res.cleanup()          # 统一清理负责真正删除
        assert not raw.exists(), "协调后仍未删除目录"
        assert report["failed"] == [], report["failed"]
    finally:
        res.release()


def test_pending_path_outside_run_is_not_promoted(runs_dir, tmp_path):
    """**越界的 pending 目录不得被提升**（保持 pending，由报告暴露，不静默删）。"""
    res = rr.RunResources.new(endpoint="", identity="path-pending", runs_dir=runs_dir)
    try:
        outside = tmp_path / "outside-dir"
        outside.mkdir(parents=True, exist_ok=True)
        res.declare("dir", str(outside))
        promoted = res._reconcile_pending_paths([str(outside)])
        assert promoted == [], f"越界路径竟被提升：{promoted}"
        entry = next(e for e in res._read()["entries"] if e["key"] == str(outside))
        assert entry["status"] == "pending", "越界项应保持 pending 以便报告暴露"
        assert outside.exists(), "越界目录绝不能被删"
    finally:
        res.release()