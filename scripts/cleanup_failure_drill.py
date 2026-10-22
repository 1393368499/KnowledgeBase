"""真实"故意失败仍清理"演练（逐条落实审查意见）。

要点：
  * 生成的用例带 `@pytest.mark.django_db` —— 否则会在**建集合之前**就因数据库
    访问失败而中止，根本证明不了"故意失败仍清理"；
  * 用例文件名含本次演练的 run_uuid（唯一），**拒绝覆盖已有文件**；
  * 创建成功后写**结构化 marker**（collection / run_uuid / endpoint）；
  * 子进程退码必须**恰为 1**（pytest 的"有用例失败"），不是"非零就行" ——
    收集失败是 2/4，编码错误同样非零，都会假通过；
  * marker 必须证明集合**确实被创建**；
  * 独立复查：marker 里的集合已消失、`cleanup-report.failed == []`、
    raw 与 vector_data 目录已清、原有集合一个不少；
  * 删除演练文件前验证**真实绝对包含关系**（必须落在 tests/ 内且非链接）。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = ROOT / "tests"
RUNS_DIR = ROOT / "artifacts" / "test-runs"

DRILL_TEMPLATE = '''"""自动生成的演练用例：建真集合后**故意失败**（跑完即删）。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

ENDPOINT = os.environ.get("KB_TEST_QDRANT_URL", "")
pytestmark = [
    pytest.mark.django_db(transaction=True),   # 必须：否则建集合前就因 DB 失败
    pytest.mark.skipif(not ENDPOINT, reason="需要隔离端点"),
]


def test_create_real_collection_then_fail():
    """真实命名/建集合 → 写 marker → 故意 assert False。删除只由会话 finally 完成。"""
    from qdrant_client import QdrantClient

    from apps.knowledge import vectorindex
    from apps.knowledge.models import KnowledgeLibrary

    lib = KnowledgeLibrary.objects.create(
        code="drill{uuid8}", name="演练库",
        vector_collection="", active_generation=0, next_generation=0,
    )
    client = QdrantClient(url=ENDPOINT, timeout=20)
    name = vectorindex.collection_name(lib.pk, 1)      # 生产同一个命名函数
    vectorindex.ensure_collection(client, name, 4)
    assert client.collection_exists(name), "集合必须真的建出来"

    # **结构化 marker**：证明"确实创建成功"，供独立进程核对。
    # 注意：模板用 `.replace("{uuid8}", ...)` 生成，**不是** `str.format`，
    # 所以这里必须用**单**大括号 —— 写 `{{...}}` 会生成 set-of-dict，
    # 触发 `TypeError: unhashable type: 'dict'`，marker 永远写不出来。
    Path(os.environ["KB_DRILL_MARKER"]).write_text(
        json.dumps({"collection": name, "run_uuid": os.environ["KB_TEST_RUN_UUID"],
                    "endpoint": ENDPOINT, "library_id": lib.pk},
                   ensure_ascii=False), encoding="utf-8")
    assert False, "演练：故意失败，用于验证失败退出仍会清理"
'''


def snapshot(endpoint: str) -> list[str]:
    from qdrant_client import QdrantClient

    client = QdrantClient(url=endpoint, timeout=20)
    return sorted(c.name for c in client.get_collections().collections)


def safe_unlink(path: Path) -> None:
    """删除前验证**真实绝对包含关系**：必须是 tests/ 下的普通文件。"""
    real = Path(os.path.realpath(path))
    allowed = Path(os.path.realpath(TESTS_DIR))
    if os.path.commonpath([str(real), str(allowed)]) != str(allowed):
        raise RuntimeError(f"拒绝删除（越界）：{real}")
    if real.is_symlink():
        raise RuntimeError(f"拒绝删除（链接）：{real}")
    real.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真实故意失败仍清理的演练")
    parser.add_argument("--endpoint", default="http://127.0.0.1:56339")
    args = parser.parse_args(argv)

    drill_uuid = uuid.uuid4().hex[:12]
    test_file = TESTS_DIR / f"_zz_drill_failing_{drill_uuid}.py"
    marker = RUNS_DIR / drill_uuid / "drill-marker.json"

    if test_file.exists():
        print(f"演练文件已存在，拒绝覆盖：{test_file}", file=sys.stderr)
        return 1
    if not TESTS_DIR.is_dir():
        print(f"缺少 tests 目录：{TESTS_DIR}", file=sys.stderr)
        return 1

    before = snapshot(args.endpoint)
    print(f"演练前集合（{len(before)}）：{before}")

    marker.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text(DRILL_TEMPLATE.replace("{uuid8}", drill_uuid), encoding="utf-8")
    try:
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", "-m", "pytest", str(test_file),
             "-p", "no:randomly", "-q", "--tb=line"],
            cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            env={**os.environ,
                 "KB_TEST_QDRANT_URL": args.endpoint,
                 "KB_TEST_RUN_UUID": drill_uuid,
                 "KB_DRILL_MARKER": str(marker),
                 "KB_PYTEST_DB": "sqlite",
                 "PYTHONUTF8": "1"},
        )
    finally:
        safe_unlink(test_file)

    combined = proc.stdout + proc.stderr
    print(f"子进程退码 = {proc.returncode}")

    problems: list[str] = []
    # 退码必须**恰为 1**：=1 是"有用例失败"；2/4 是收集/内部错误，不是要验的东西
    if proc.returncode != 1:
        problems.append(
            f"退码为 {proc.returncode}，期望恰为 1（=1 才是'用例失败'）\n{combined[-600:]}"
        )
    if not marker.exists():
        problems.append("缺少演练 marker —— 无法证明集合真的被创建（可能在建之前就失败了）")
    else:
        info = json.loads(marker.read_text(encoding="utf-8"))
        print(f"marker：{info}")
        if info.get("run_uuid") != drill_uuid:
            problems.append(f"marker 的 run_uuid 不符：{info.get('run_uuid')} ≠ {drill_uuid}")

        after = snapshot(args.endpoint)
        print(f"演练后集合（{len(after)}）：{after}")
        if info["collection"] in after:
            problems.append(f"**失败退出后集合泄漏**：{info['collection']}")
        leaked = [n for n in after if n.startswith("kb-") and not n.startswith("kb-lt-")
                  and n not in before]
        if leaked:
            problems.append(f"发现未回收的本 run 集合：{leaked}")
        missing = [n for n in before if n not in after]
        if missing:
            problems.append(f"原有集合被误删：{missing}")

        report_path = RUNS_DIR / drill_uuid / "cleanup-report.json"
        if not report_path.exists():
            problems.append("缺少 cleanup-report.json（会话清理没跑？）")
        else:
            rep = json.loads(report_path.read_text(encoding="utf-8"))
            if rep.get("failed"):
                problems.append(f"cleanup-report.failed 非空：{rep['failed']}")
            if not any(info["collection"] in d for d in rep.get("deleted", [])):
                problems.append("cleanup-report 里没有该集合的删除记录")
        for sub in ("raw", "vector_data"):
            target = RUNS_DIR / drill_uuid / sub
            if target.exists():
                problems.append(f"{sub} 目录未被清理：{target}")

    if problems:
        print("\n演练失败：", file=sys.stderr)
        for item in problems:
            print("  - " + item, file=sys.stderr)
        return 1
    print(f"\n演练通过：用例故意失败（退码恰为 1）后，marker 集合已删除、"
          f"cleanup-report.failed=[]、raw/vector_data 已清、原有 {len(before)} 个集合全保留")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
