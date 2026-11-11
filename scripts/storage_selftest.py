"""存储迁移的**共享实现**自检：与生产走同一套函数，只做真实负例与往返验证。

审查要求：selftest 必须调用共享的备份/校验逻辑，不能再另写一份与生产不同的
夹具实现（那样测的不是真东西）。

覆盖的真实负例：
  * 越界路径（root 之外）被拒；
  * **兄弟前缀**（proj2 不算在 proj 之内）被拒；
  * 输入/输出**相交**被拒；
  * 非法 Linux 目标（前缀不符 / 含 .. / 含元字符）被拒；
  * 空间判据用真实 `shutil.disk_usage` 取值，且边界是"严格多于下限"；
  * 冷备往返：流式哈希 → 写 zip → **全量流式解压核对** → 篡改后必须被发现；
  * `Abort` 可被捕获（Python 异常，不像 PS 的 exit 会绕过 catch）。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

# 允许以脚本方式直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent))

import storage_migrate as sm  # noqa: E402


def _check(results: list, name: str, ok: bool, detail: str = "") -> None:
    results.append({"check": name, "ok": bool(ok), "detail": detail})
    print(("  PASS " if ok else "  FAIL ") + name + (f" | {detail}" if detail else ""), flush=True)


def _rejects(fn, *args, **kwargs) -> bool:
    """调用应当抛 Abort 才算"被拒绝"。"""
    try:
        fn(*args, **kwargs)
    except sm.Abort:
        return True
    except Exception:  # noqa: BLE001 - 非 Abort 的异常不算预期拒绝
        return False
    return False


def run_selftest(root: Path) -> int:
    results: list[dict] = []
    # 夹具**必须落在项目 artifacts 下**：do_backup 的包含关系约束针对项目 root，
    # 用系统临时目录会绕过该约束（审查指出）。
    artifacts = Path(os.path.realpath(root / "artifacts"))
    artifacts.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="selftest-fixture-", dir=str(artifacts)))
    root = Path(os.path.realpath(root))

    # ---------------------------------------------------------- 1. 路径守卫 --
    print("\n== 1. 路径与包含关系（真实负例）")
    _check(results, "root 内相对路径被接受",
           sm.real_contained(root / "vector_data", root, "x") is not None)
    _check(results, "root 之外被拒（真实越界）",
           _rejects(sm.real_contained, Path("C:/Windows/System32"), root, "x"))
    # 兄弟前缀：proj2 不能被当作在 proj 之内
    sibling = Path(str(root) + "2") / "x"
    _check(results, "兄弟前缀（proj2 不算 proj 内）被拒",
           _rejects(sm.real_contained, sibling, root, "x"))

    # ------------------------------------------------------------ 2. 不相交 --
    print("\n== 2. 输入/输出必须不相交")
    a = root / "vector_data"
    _check(results, "输出在输入之内被拒",
           _rejects(sm.assert_disjoint, a, a / "backup"))
    _check(results, "输入在输出之内被拒",
           _rejects(sm.assert_disjoint, a / "inner", a))
    _check(results, "互不相交时通过",
           not _rejects(sm.assert_disjoint, a, root / "artifacts" / "cold"))

    # ------------------------------------------------------- 3. Linux 目标 --
    print("\n== 3. Linux 目标校验")
    _check(results, "合法目标通过", not _rejects(sm.assert_linux_target, "/var/lib/qdrant-gen2"))
    _check(results, "前缀不符被拒", _rejects(sm.assert_linux_target, "/tmp/evil"))
    _check(results, "含 .. 被拒", _rejects(sm.assert_linux_target, "/var/lib/qdrant-../etc"))
    _check(results, "含元字符被拒", _rejects(sm.assert_linux_target, "/var/lib/qdrant-x;rm -rf /"))

    # --------------------------------------------------------- 4. 空间判据 --
    print("\n== 4. 空间判据（真实 disk_usage + 边界）")
    free = sm.free_bytes(root)
    _check(results, "能取到真实剩余空间", free > 0, f"{free/1024**3:.2f} GiB")
    prev = sm.MIN_FREE_BYTES
    try:
        sm.MIN_FREE_BYTES = free + 1     # 抬高下限 → 必须拒绝
        _check(results, "余量低于下限被拒", _rejects(sm.assert_space, root, "t"))
        sm.MIN_FREE_BYTES = max(1, free - 1)   # 下调 → 必须通过（严格多于下限）
        _check(results, "余量高于下限通过", not _rejects(sm.assert_space, root, "t"))
    finally:
        sm.MIN_FREE_BYTES = prev

    # ------------------------------------------------- 5. 冷备往返（共享实现）--
    print("\n== 5. 冷备往返：流式哈希 → zip → 全量流式核对")
    src_dir = tmp / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    payloads = {
        "collections/kb-1-g1/segments/a.bin": b"\x00" * 300_000,
        "collections/kb-1-g1/segments/b.bin": b"hello-qdrant" * 5_000,
        "meta.json": json.dumps({"v": 1}).encode(),
    }
    for rel, blob in payloads.items():
        target_file = src_dir / rel
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_bytes(blob)

    # ---- 调用**真实** do_backup（注入 NullController：不触碰任何容器）----
    backup_dir = tmp / "backup"
    rb = sm.Rollback("KnowledgeBase-Runtime", 56333, 56339, service_control=False)
    ctl = sm.NullController()
    rb.controller = ctl
    sm.do_backup(
        src_dir, backup_dir, execute=True, rb=rb, service_control=False, controller=ctl
    )

    zips = sorted(backup_dir.glob("*.zip"))
    _check(results, "真实 do_backup 产出归档", len(zips) == 1, f"{[z.name for z in zips]}")
    manifest_path = backup_dir / "manifest.json"
    _check(results, "真实 do_backup 产出清单", manifest_path.exists())
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))["files"]
    _check(results, "清单条数 = 源文件数", len(manifest) == len(payloads),
           f"{len(manifest)}/{len(payloads)}")
    verified_path = backup_dir / "manifest.verified.json"
    verified = json.loads(verified_path.read_text(encoding="utf-8")) if verified_path.exists() else {}
    _check(results, "verified 标记已原子写入且计数一致",
           verified.get("count") == len(payloads), json.dumps(verified, ensure_ascii=False))

    # 归档条目压缩方式必须是 DEFLATED（否则"压缩冷备"名不副实）
    with zipfile.ZipFile(zips[0]) as zf:
        stored = [i.filename for i in zf.infolist() if i.compress_type == zipfile.ZIP_STORED]
    _check(results, "所有条目都是 DEFLATED（非原样存储）", not stored, f"STORED={stored}")

    # ---- 篡改**真实归档**的一个条目 → 必须能被共享哈希发现（真负例）----
    tampered = tmp / "tampered.zip"
    target_rel = manifest[0]["rel"]
    with zipfile.ZipFile(zips[0]) as zin, zipfile.ZipFile(tampered, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item)
            if item.filename == target_rel:
                data = data + b"x"
            zout.writestr(item, data)
    by_rel = {m["rel"]: m for m in manifest}
    detected = False
    with zipfile.ZipFile(tampered) as zf:
        for entry in zf.infolist():
            with zf.open(entry) as fh:
                digest, size = sm.sha256_stream(fh)   # 与生产同一个函数
            want = by_rel.get(entry.filename)
            if want is None or want["sha256"] != digest or want["size"] != size:
                detected = True
    _check(results, "被篡改的归档会被发现", detected, f"篡改目标 {target_rel}")

    # ------------------------------------------------- 6. Abort 可被捕获 --
    print("\n== 6. Abort 是 Python 异常（不像 PS 的 exit 会绕过 catch）")
    caught = False
    try:
        raise sm.Abort("测试")
    except sm.Abort:
        caught = True
    _check(results, "Abort 可被 except 捕获", caught)
    _check(results, "Abort 是 RuntimeError 子类（能被通用 except Exception 兜住）",
           issubclass(sm.Abort, RuntimeError))

    # --------------------------------------------------------------- 汇总 --
    failed = [r for r in results if not r["ok"]]
    print(f"\n== 自检汇总\n  {len(results) - len(failed)} / {len(results)} 通过")
    for item in failed:
        print("  FAIL " + item["check"] + " | " + item["detail"])
    safe_rmtree(tmp, root)
    print(f"  临时目录已清理：{tmp}")
    return 1 if failed else 0


def safe_rmtree(path: Path, root: Path) -> None:
    """删除前**验证真实绝对包含关系**：只删项目 artifacts 之下的自建夹具。

    直接 rmtree 一个算出来的路径太危险（变量一旦被改就成了删别处），
    所以这里做三重校验：真实路径在 artifacts 之内、不是 artifacts 本身、
    目录名以 selftest-fixture- 开头。
    """
    import shutil

    target = Path(os.path.realpath(path))
    allowed = Path(os.path.realpath(root / "artifacts"))
    try:
        inside = os.path.commonpath([str(target), str(allowed)]) == str(allowed)
    except ValueError:
        inside = False
    if not inside or target == allowed or not target.name.startswith("selftest-fixture-"):
        print(f"  ⚠ 拒绝删除（不在允许范围或名字不符）：{target}")
        return
    shutil.rmtree(target, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(run_selftest(Path(__file__).resolve().parent.parent))
