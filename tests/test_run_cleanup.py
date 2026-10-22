"""`run_resources` 的真实验收（用假 Qdrant 客户端，不依赖真实服务）。

覆盖用户点名的 9 个场景：
  1 正常结束自动清理；2 用例故意失败也会清理；3 重复清理幂等；
  4 同 endpoint 两个 run 互不影响；5 `kb-1-g1` 哨兵不可删；
  6 拒绝越界 / junction / 未登记集合；7 模拟遗留 run 恢复；
  8 清理失败非零 + 显式报告；9 只列不删（历史 `kb-lt-*` 不动）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_resources as rr  # noqa: E402


class FakeQdrant:
    """只记录调用，不连任何服务。"""

    def __init__(self, existing: set[str] | None = None,
                 endpoint: str = "http://127.0.0.1:56339") -> None:
        self.existing = set(existing or ())
        # 交叉验证读的是真实字段 `_client.rest_uri`；假客户端也要如实暴露，
        # 否则会被"端点未知 → 拒绝清理"正当拦下。
        self._client = type("C", (), {"rest_uri": endpoint})()
        self.deleted: list[str] = []
        self.fail_on: set[str] = set()

    def collection_exists(self, name: str) -> bool:
        return name in self.existing

    def get_collections(self):
        """补齐接口：pending 协调/前缀清点需要它（不可为测试绿而跳过）。"""
        item = type("C", (), {})
        return type("R", (), {"collections": [
            type("I", (), {"name": n})() for n in sorted(self.existing)
        ]})()

    def delete_collection(self, collection_name: str) -> None:
        if collection_name in self.fail_on:
            raise RuntimeError("模拟删除失败")
        self.existing.discard(collection_name)
        self.deleted.append(collection_name)


@pytest.fixture
def runs_dir(tmp_path):
    return tmp_path / "test-runs"


def make_run(runs_dir, *, endpoint="http://127.0.0.1:56339", identity="pytest"):
    # 用 new()：新建 run 会建目录并打心跳
    return rr.RunResources.new(endpoint=endpoint, identity=identity, runs_dir=runs_dir)


# ---------------------------------------------------------------- 1) 正常 --
def test_normal_run_cleans_only_registered(runs_dir):
    res = make_run(runs_dir)
    name = res.name("alpha")
    res.declare("qdrant_collection", name)
    res.confirm("qdrant_collection", name)
    fake = FakeQdrant({name, "kb-1-g1", "kb-lt-other"})

    report = res.cleanup(qdrant_client=fake)

    assert fake.deleted == [name], "只应删本 run 登记的那一个"
    assert "kb-1-g1" in fake.existing, "哨兵不能被删"
    assert "kb-lt-other" in fake.existing, "历史集合不能被碰"
    assert report["failed"] == []


# -------------------------------------------------- 4) 两 run 互不影响 --
def test_two_runs_same_endpoint_are_isolated(runs_dir):
    a, b = make_run(runs_dir), make_run(runs_dir)
    na, nb = a.name("x"), b.name("x")
    assert na != nb, "不同 run 必须有不同的独占前缀"
    for res, name in ((a, na), (b, nb)):
        res.declare("qdrant_collection", name)
        res.confirm("qdrant_collection", name)
    fake = FakeQdrant({na, nb})

    a.cleanup(qdrant_client=fake)

    assert fake.deleted == [na]
    assert nb in fake.existing, "另一个 run 的集合不能被这个 run 删掉"


# ------------------------------------------------ 5/6) 拒绝与越界 --
def test_protected_sentinel_is_refused(runs_dir):
    res = make_run(runs_dir)
    # 即使被误登记，也必须拒绝（前缀/保护名单双重把关）
    res.declare("qdrant_collection", "kb-1-g1")
    res.confirm("qdrant_collection", "kb-1-g1")
    with pytest.raises(rr.CleanupError) as exc:
        res.cleanup(qdrant_client=FakeQdrant({"kb-1-g1"}))
    assert "受保护" in str(exc.value)


def test_unregistered_collection_is_never_touched(runs_dir):
    res = make_run(runs_dir)
    fake = FakeQdrant({"kb-1-g1", "kb-lt-legacy", "kb-otherrun-abc"})
    res.cleanup(qdrant_client=fake)
    assert fake.deleted == [], "没登记的集合一个都不能删"


def test_prefix_mismatch_is_refused(runs_dir):
    res = make_run(runs_dir)
    res.declare("qdrant_collection", "kb-somebodyelse-1")
    res.confirm("qdrant_collection", "kb-somebodyelse-1")
    with pytest.raises(rr.CleanupError) as exc:
        res.cleanup(qdrant_client=FakeQdrant({"kb-somebodyelse-1"}))
    assert "不属于本次 run" in str(exc.value)


def test_path_outside_allowed_root_is_refused(runs_dir, tmp_path):
    res = make_run(runs_dir)
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    res.declare("file", str(outside))
    res.confirm("file", str(outside))
    with pytest.raises(rr.CleanupError) as exc:
        res.cleanup(qdrant_client=FakeQdrant())
    assert "越界" in str(exc.value)
    assert outside.exists(), "越界文件不能被删"


def test_junction_or_symlink_is_refused(runs_dir):
    res = make_run(runs_dir)
    inside = res.dir / "link"
    real = res.dir / "real"
    real.mkdir(parents=True, exist_ok=True)
    (real / "f.txt").write_text("x", encoding="utf-8")
    try:
        os.symlink(real, inside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("该平台/权限不支持创建符号链接")
    res.declare("dir", str(inside))
    res.confirm("dir", str(inside))
    with pytest.raises(rr.CleanupError) as exc:
        res.cleanup(qdrant_client=FakeQdrant())
    assert "链接" in str(exc.value)


# ---------------------------------------------------------- 3) 幂等 --
def test_cleanup_is_idempotent(runs_dir):
    res = make_run(runs_dir)
    name = res.name("idem")
    res.declare("qdrant_collection", name)
    res.confirm("qdrant_collection", name)
    fake = FakeQdrant({name})

    first = res.cleanup(qdrant_client=fake)
    second = res.cleanup(qdrant_client=fake)

    assert first["deleted"] and not first["failed"]
    assert second["deleted"] == [], "第二次不应再删"
    assert any("已删过" in s for s in second["skipped"])
    assert fake.deleted == [name]


def test_missing_collection_counts_as_success(runs_dir):
    """集合本来就不存在（上次已删/从未建成功）→ 幂等成功，不算失败。"""
    res = make_run(runs_dir)
    name = res.name("gone")
    res.declare("qdrant_collection", name)
    res.confirm("qdrant_collection", name)
    fake = FakeQdrant(set())          # 服务端已经没有这个集合
    report = res.cleanup(qdrant_client=fake)
    assert report["failed"] == []
    assert fake.deleted == []


# ------------------------------------------------------- 8) 失败语义 --
def test_cleanup_failure_is_reported_and_nonzero(runs_dir, capsys, monkeypatch):
    res = make_run(runs_dir)
    good, bad = res.name("good"), res.name("bad")
    for name in (good, bad):
        res.declare("qdrant_collection", name)
        res.confirm("qdrant_collection", name)
    fake = FakeQdrant({good, bad})
    fake.fail_on = {bad}

    with pytest.raises(rr.CleanupError) as exc:
        res.cleanup(qdrant_client=fake)
    assert "清理失败" in str(exc.value)
    # 已成功的那个必须**已被标记**（不因后面的失败而丢失）
    doc = json.loads(res.manifest.read_text(encoding="utf-8"))
    status = {e["key"]: e["status"] for e in doc["entries"]}
    assert status[good] == "deleted"

    # CLI 也要非零退出并显式报告。
    # **必须密闭**：不能让它真去连 Qdrant（原写法会构造真实 QdrantClient 打到
    # 127.0.0.1:56339，既用了真实端点又让断言"因为网络失败"而假通过）。
    # 这里把 cleanup 换成必定抛错的替身，专测 CLI 的退出码与报告。
    original = rr.RUNS_DIR
    rr.RUNS_DIR = runs_dir

    def boom(self, **_kw):
        raise rr.CleanupError("模拟清理失败")

    # 用 monkeypatch（自动还原）—— 直接赋值会**永久改写类**，污染后续用例
    monkeypatch.setattr(rr.RunResources, "cleanup", boom)
    try:
        code = rr.main(["cleanup", "--uuid", res.run_uuid])
    finally:
        rr.RUNS_DIR = original
    assert code == 1
    assert "清理失败" in capsys.readouterr().err


def test_redis_key_without_client_is_failure_not_crash(runs_dir):
    res = make_run(runs_dir)
    key = res.prefix + "cache:1"
    res.declare("redis_key", key)
    res.confirm("redis_key", key)
    with pytest.raises(rr.CleanupError) as exc:
        res.cleanup(qdrant_client=FakeQdrant())
    assert "未提供隔离 Redis 客户端" in str(exc.value)


def test_redis_key_outside_run_prefix_is_refused(runs_dir):
    class FakeRedis:
        def __init__(self, db: int = 13) -> None:
            self.deleted: list[str] = []
            # 让 `_assert_redis_allowed` 能读到专用 DB（非 DB0）
            self.connection_pool = type(
                "P", (), {"connection_kwargs": {"db": db}}
            )()

        def delete(self, key):
            self.deleted.append(key)

        def hdel(self, key, field):
            self.deleted.append(f"{key}:{field}")

    res = make_run(runs_dir)
    res.declare("redis_key", "someone-else-key")
    res.confirm("redis_key", "someone-else-key")
    redis = FakeRedis()
    with pytest.raises(rr.CleanupError) as exc:
        res.cleanup(qdrant_client=FakeQdrant(), redis_client=redis)
    assert "不允许" in str(exc.value) or "不属于" in str(exc.value)
    assert redis.deleted == []


# --------------------------------------------------- 7) 遗留 run 恢复 --
def test_recover_leftover_run_with_stale_heartbeat(runs_dir):
    res = make_run(runs_dir, identity="leftover")
    name = res.name("left")
    res.declare("qdrant_collection", name)
    res.confirm("qdrant_collection", name)
    # 模拟进程被杀：OS 会**自动释放**它持有的锁，心跳也会停在很久以前。
    # 只改心跳是不够的 —— 锁判活才是权威依据（心跳仅作辅助）。
    res.release()
    (res.dir / "heartbeat").write_text(json.dumps({"at": 0, "pid": 1}), encoding="utf-8")

    fake = FakeQdrant({name})
    report = rr.recover(res.run_uuid, runs_dir=runs_dir, qdrant_client=fake)
    assert fake.deleted == [name]
    assert report["failed"] == []


def test_recover_refuses_when_run_still_alive(runs_dir):
    res = make_run(runs_dir)
    name = res.name("alive")
    res.declare("qdrant_collection", name)
    res.confirm("qdrant_collection", name)
    res.beat()                        # 心跳新鲜 → 可能仍在运行
    fake = FakeQdrant({name})
    with pytest.raises(rr.CleanupError) as exc:
        rr.recover(res.run_uuid, runs_dir=runs_dir, qdrant_client=fake)
    assert "跨进程锁" in str(exc.value)
    assert fake.deleted == [], "活跃 run 的资源绝不能被恢复清理动到"


def test_recover_rejects_conflicting_endpoint(runs_dir):
    res = make_run(runs_dir, endpoint="http://127.0.0.1:56339")
    name = res.name("ep")
    res.declare("qdrant_collection", name)
    res.confirm("qdrant_collection", name)
    with pytest.raises(rr.CleanupError) as exc:
        rr.recover(res.run_uuid, endpoint="http://127.0.0.1:9999", runs_dir=runs_dir)
    assert "不一致" in str(exc.value)


# ------------------------------------------------------ 2) 失败也清理 --
def test_cleanup_runs_even_when_the_case_fails(runs_dir):
    """模拟 pytest 的 finally 语义：用例中途抛错，清理仍要发生。"""
    res = make_run(runs_dir)
    name = res.name("finally")
    res.declare("qdrant_collection", name)
    res.confirm("qdrant_collection", name)
    fake = FakeQdrant({name})

    cleaned = False
    with pytest.raises(AssertionError):      # 用例确实失败了
        try:
            raise AssertionError("用例故意失败")
        finally:
            res.cleanup(qdrant_client=fake)  # 但清理照样发生
            cleaned = True

    assert cleaned and fake.deleted == [name]


# --------------------------------------------------------- 9) 只列不删 --
def test_list_reports_without_deleting(runs_dir, capsys):
    res = make_run(runs_dir)
    name = res.name("listed")
    res.declare("qdrant_collection", name)
    res.confirm("qdrant_collection", name)

    original = rr.RUNS_DIR
    rr.RUNS_DIR = runs_dir
    try:
        code = rr.main(["list"])
    finally:
        rr.RUNS_DIR = original

    out = capsys.readouterr().out
    assert code == 0
    assert res.run_uuid in out and "只列不删" in out
    # 清单里那条依然是 created（list 不改变状态）
    doc = json.loads(res.manifest.read_text(encoding="utf-8"))
    assert doc["entries"][0]["status"] == "created"


# ------------------------------------------------------ ④ 并发登记安全 --
def test_concurrent_declares_keep_all_50(runs_dir):
    """50 个线程并发登记，必须**一条不丢**（锁 + 锁内重读 + 唯一 tmp）。"""
    import threading

    res = make_run(runs_dir)
    errors: list[str] = []

    def declare(i: int) -> None:
        try:
            res.declare("qdrant_collection", res.name(f"c{i}"))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=declare, args=(i,)) for i in range(50)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert errors == [], f"并发登记报错：{errors[:3]}"
    doc = json.loads(res.manifest.read_text(encoding="utf-8"))
    keys = {e["key"] for e in doc["entries"]}
    assert len(keys) == 50, f"并发登记丢了更新：只保留 {len(keys)}/50"


def test_run_uuid_must_be_strict_hex(runs_dir):
    """`--uuid ../..` 之类必须被拒（否则能逃出 runs 根）。"""
    for bad in ("..", "../../etc", "abc/def", "zzzzzzzzzzzz", ""):
        with pytest.raises(rr.CleanupError):
            rr.RunResources.open(bad, runs_dir=runs_dir)


def test_run_own_files_are_never_deleted(runs_dir):
    """run 根自身与清单/报告/心跳都不得被删。"""
    res = make_run(runs_dir)
    for target in (str(res.dir), str(res.manifest), str(res.dir / "cleanup-report.json")):
        res.declare("dir", target)
        res.confirm("dir", target)
    with pytest.raises(rr.CleanupError) as exc:
        res.cleanup(qdrant_client=FakeQdrant())
    msg = str(exc.value)
    assert "run 根目录自身" in msg or "run 关键文件" in msg
    assert res.manifest.exists(), "清单必须还在"


# ------------------------------------------------- ⑥ Redis 专用库约束 --
def test_redis_db0_is_refused(runs_dir):
    """DB0 是业务库（含现有队列消息）—— 必须拒绝，绝不清。"""

    class DbZeroRedis:
        def __init__(self) -> None:
            self.deleted: list[str] = []
            self.connection_pool = type(
                "P", (), {"connection_kwargs": {"db": 0}}
            )()

        def delete(self, key):
            self.deleted.append(key)

    res = make_run(runs_dir)
    key = res.prefix + "q"
    res.declare("redis_key", key)
    res.confirm("redis_key", key)
    redis = DbZeroRedis()
    with pytest.raises(rr.CleanupError) as exc:
        res.cleanup(qdrant_client=FakeQdrant(), redis_client=redis)
    assert "DB0" in str(exc.value)
    assert redis.deleted == [], "绝不能真的去删 DB0 的键"


def test_redis_with_unidentifiable_db_is_refused(runs_dir):
    """读不到 DB 号的客户端一律拒绝（安全优先，不猜）。"""

    class OpaqueRedis:
        def delete(self, key):  # pragma: no cover - 不应被调用
            raise AssertionError("不该被调用")

    res = make_run(runs_dir)
    key = res.prefix + "q"
    res.declare("redis_key", key)
    res.confirm("redis_key", key)
    with pytest.raises(rr.CleanupError) as exc:
        res.cleanup(qdrant_client=FakeQdrant(), redis_client=OpaqueRedis())
    assert "无法确认" in str(exc.value)


# --------------------------------------------- ② 判活以跨进程锁为准 --
def test_alive_run_cannot_be_forced(runs_dir):
    """`--force` **不得**绕过仍然持有锁的活跃 run。"""
    res = make_run(runs_dir)
    name = res.name("live")
    res.declare("qdrant_collection", name)
    res.confirm("qdrant_collection", name)
    assert res.is_alive() is True, "持有锁的 run 必须判为存活"
    fake = FakeQdrant({name})
    with pytest.raises(rr.CleanupError):
        rr.recover(res.run_uuid, runs_dir=runs_dir, force=True, qdrant_client=fake)
    assert fake.deleted == [], "活跃 run 的集合绝不能被 force 删掉"


def test_released_run_is_treated_as_dead(runs_dir):
    """锁被释放（进程已结束）→ 判为死亡，可被恢复清理。"""
    res = make_run(runs_dir)
    name = res.name("dead")
    res.declare("qdrant_collection", name)
    res.confirm("qdrant_collection", name)
    res.release()
    assert res.is_alive() is False
    fake = FakeQdrant({name})
    report = rr.recover(res.run_uuid, runs_dir=runs_dir, qdrant_client=fake)
    assert fake.deleted == [name] and report["failed"] == []