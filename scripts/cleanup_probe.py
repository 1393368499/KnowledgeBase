"""自动清理的**真实闭环**探针（隔离端点）。

修正（Codex 审查）：
  * **绝不覆盖** `artifacts/acceptance/cleanup-before-20261015.json` ——
    那是**主实例（56333）**的只读保护基线，写一次即锁定；再跑 `--baseline`
    只会在**本 run 报告目录**里另存一份端点基线。
  * **不再强制 `kb-1-g1` 存在**：它是主实例的哨兵，隔离端点本来就没有。
    隔离端点的正确判据是"**已有集合全部保留**"
    （如 `codex-runtime-probe-20261015`），而不是"某个同名集合存在"。
  * 只为**本 run 的独占前缀**创建集合，绝不为"哨兵"造一个主 `kb-` 集合。

用法：
  python scripts/cleanup_probe.py --endpoint http://127.0.0.1:56339            # 真实闭环
  python scripts/cleanup_probe.py --endpoint ... --baseline                   # 写端点基线
  python scripts/cleanup_probe.py --endpoint http://127.0.0.1:56333 --protect # 主实例只读保护
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import run_resources as rr  # noqa: E402

#: **主实例只读保护基线**：只读、写一次、永不覆盖
MAIN_BASELINE = ROOT / "artifacts" / "acceptance" / "cleanup-before-20261015.json"
RAW_DIR = ROOT / "media" / "raw"


def snapshot(endpoint: str, with_raw: bool = False) -> dict:
    from qdrant_client import QdrantClient

    client = QdrantClient(url=endpoint, timeout=30)
    names = sorted(c.name for c in client.get_collections().collections)
    doc = {
        "endpoint": endpoint,
        "collections": names,
        "collections_count": len(names),
        "captured_at": __import__("time").time(),
    }
    if with_raw:
        raw: dict[str, str] = {}
        if RAW_DIR.exists():
            for path in sorted(p for p in RAW_DIR.rglob("*") if p.is_file()):
                digest = hashlib.sha256()
                with path.open("rb") as fh:
                    for block in iter(lambda: fh.read(1024 * 1024), b""):
                        digest.update(block)
                raw[path.relative_to(RAW_DIR).as_posix()] = digest.hexdigest()
        doc["raw_files"] = raw
        doc["raw_count"] = len(raw)
    return doc


def write_main_baseline_once(doc: dict) -> None:
    """写主实例保护基线；**已存在就绝不覆盖**（这是审查明确要求的）。"""
    if MAIN_BASELINE.exists():
        saved = json.loads(MAIN_BASELINE.read_text(encoding="utf-8"))
        print(f"主实例保护基线已存在（{saved['collections_count']} 集合 / "
              f"{saved.get('raw_count', '?')} raw 文件），**不覆盖**："
              f"{MAIN_BASELINE.relative_to(ROOT)}")
        return
    MAIN_BASELINE.parent.mkdir(parents=True, exist_ok=True)
    MAIN_BASELINE.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"主实例保护基线已写入（首次）：{MAIN_BASELINE.relative_to(ROOT)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="自动清理真实闭环探针")
    parser.add_argument("--endpoint", default="http://127.0.0.1:56339")
    parser.add_argument("--baseline", action="store_true",
                        help="把**本端点**的基线写进本 run 报告目录（不覆盖主基线）")
    parser.add_argument("--protect", action="store_true",
                        help="主实例只读保护：写一次主基线并校验 raw 未被改动")
    args = parser.parse_args(argv)

    is_main = "56333" in args.endpoint
    before = snapshot(args.endpoint, with_raw=is_main)
    print(f"[{args.endpoint}] 现有 {before['collections_count']} 个集合"
          + (f"，media/raw {before['raw_count']} 个文件" if is_main else ""))

    # ---- 主实例只读保护：只写一次基线，绝不做任何创建/删除 ----
    if args.protect:
        if not is_main:
            print("--protect 只用于主实例（56333）", file=sys.stderr)
            return 2
        write_main_baseline_once(before)
        return 0

    # ---- 端点基线：写在**本 run 报告目录**，不碰主基线 ----
    run = rr.RunResources.new(endpoint=args.endpoint, identity="cleanup-probe")
    if args.baseline:
        target = run.dir / "endpoint-baseline.json"
        target.write_text(json.dumps(before, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"端点基线已写入本 run 报告目录：{target.relative_to(ROOT)}")
        run.release()
        return 0

    # ---- 真实闭环：本 run 独占前缀 → 真建 → 真删 ----
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, VectorParams

    client = QdrantClient(url=args.endpoint, timeout=30)
    name = run.name("e2e")
    run.declare("qdrant_collection", name)
    if client.collection_exists(name):        # 唯一前缀 + 创建前不存在检查
        print(f"命名冲突（不该发生）：{name}", file=sys.stderr)
        run.release()
        return 1
    client.create_collection(name, vectors_config=VectorParams(size=4, distance=Distance.COSINE))
    run.confirm("qdrant_collection", name)
    print(f"已创建本 run 集合：{name}（size=4）")

    report = run.cleanup(qdrant_client=client)
    print("清理报告：", json.dumps(report, ensure_ascii=False))

    after = snapshot(args.endpoint, with_raw=is_main)
    problems: list[str] = []
    if client.collection_exists(name):
        problems.append(f"本 run 集合未被删除：{name}")
    # **正确判据**：创建前已存在的集合必须**一个不少**地保留
    missing = [c for c in before["collections"] if c not in after["collections"]]
    if missing:
        problems.append(f"原有集合被删掉了：{missing[:5]}")
    if after["collections_count"] != before["collections_count"]:
        problems.append(
            f"集合总数未回到创建前：{before['collections_count']} → {after['collections_count']}"
        )
    if is_main and MAIN_BASELINE.exists():
        saved = json.loads(MAIN_BASELINE.read_text(encoding="utf-8"))
        if saved["collections"] != after["collections"]:
            problems.append("与主实例保护基线的集合清单不一致")
        if saved.get("raw_files") != after.get("raw_files"):
            problems.append("media/raw 文件哈希变化（原件被动）")

    run.release()
    if problems:
        print("\n真实闭环校验失败：", file=sys.stderr)
        for item in problems:
            print("  - " + item, file=sys.stderr)
        return 1
    preserved = "、".join(before["collections"][:3]) or "（原本为空）"
    print(f"\n真实闭环通过：原有 {before['collections_count']} 个集合全部保留"
          f"（含 {preserved} …），本 run 集合已删除，计数回到 {after['collections_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
