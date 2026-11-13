"""按**只读盘点报告的精确名单**清理孤儿/压测向量集合（A+B 类）。

安全设计（针对"不可逆删除"这件事本身）：
  * **只删报告里 category ∈ {A_orphan, B_loadtest} 的条目** —— 按精确名字逐条删，
    **绝不**按 `kb-*` / `kb-lt-*` 模糊扫删；
  * **硬性拒绝 C 类**：任何 `registered && library_exists` 的集合一律不删
    （即使误写进报告也不删，双保险）；
  * 默认 `--dry-run`；真正执行要 `--execute` **且** `--authorized-by "<授权说明>"`；
  * 先经 Qdrant API 删集合 → **复核它确实从列表消失** → 再清残留磁盘目录；
  * 磁盘目录删除有**真实包含校验**（必须落在 `data/vector_data/qdrant/collections/` 下、
    不是该根目录本身、拒绝链接）；
  * **不碰** `data/media/raw` 原件、不碰任何数据库记录、不碰 C 类索引；
  * 逐条记录到审计报告；任何一条失败 → 非零退出 + 列出残留。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COLLECTIONS_DIR = ROOT / "data" / "vector_data" / "qdrant" / "collections"
INVENTORY = ROOT / "artifacts" / "vector-inventory.json"
REPORT = ROOT / "artifacts" / "vector-purge-report.json"
QDRANT_URL = "http://127.0.0.1:56333"
DELETABLE = ("A_orphan", "B_loadtest")


def safe_rmtree(path: Path) -> None:
    """只删 collections 根之下的真实子目录；拒绝链接、拒绝根本身。"""
    allowed = Path(os.path.realpath(COLLECTIONS_DIR))
    real = Path(os.path.realpath(path))
    if real == allowed:
        raise RuntimeError(f"拒绝删除 collections 根本身：{real}")
    try:
        inside = os.path.commonpath([str(real), str(allowed)]) == str(allowed)
    except ValueError:
        inside = False
    if not inside:
        raise RuntimeError(f"越界，拒绝删除：{real}")
    if real.is_symlink():
        raise RuntimeError(f"是链接，拒绝删除：{real}")
    if real.exists():
        shutil.rmtree(real)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按盘点报告清理 A+B 类向量集合")
    parser.add_argument("--execute", action="store_true", help="真正删除（默认 dry-run）")
    parser.add_argument("--authorized-by", default="", help="授权说明（写进审计）")
    parser.add_argument("--inventory", default=str(INVENTORY))
    args = parser.parse_args(argv)

    inventory = json.loads(Path(args.inventory).read_text(encoding="utf-8"))
    targets = [e for e in inventory["entries"] if e["category"] in DELETABLE]
    refused = [
        e for e in targets if e.get("registered") and e.get("library_exists")
    ]
    if refused:
        print("发现本应受保护的 C 类条目混入目标，拒绝继续：", [e["name"] for e in refused])
        return 2

    total = sum(e["size_bytes"] for e in targets)
    print(f"目标：{len(targets)} 个集合 = {round(total / 1024**3, 2)} GiB")
    print(f"  其中 A 孤儿 = {sum(1 for e in targets if e['category'] == 'A_orphan')}，"
          f"B 压测 = {sum(1 for e in targets if e['category'] == 'B_loadtest')}")
    print(f"保留（不删）：{inventory['summary']['C_in_use']['collections']} 个在用索引 "
          f"= {inventory['summary']['C_in_use']['gib']} GiB")

    if not args.execute:
        print("\n[dry-run] 未删除任何东西；确认后加 --execute 与 --authorized-by")
        return 0
    if not args.authorized_by.strip():
        print("拒绝执行：必须提供 --authorized-by（授权说明会写入审计）")
        return 2

    from qdrant_client import QdrantClient

    client = QdrantClient(url=QDRANT_URL, timeout=120)
    deleted: list[dict] = []
    failed: list[str] = []

    for entry in targets:
        name = entry["name"]
        try:
            client.delete_collection(collection_name=name)
            # **复核**：必须真的从列表消失，才继续清磁盘残留
            still = {c.name for c in client.get_collections().collections}
            if name in still:
                failed.append(f"{name}（API 删除后仍在列表中）")
                continue
            safe_rmtree(COLLECTIONS_DIR / name)
            deleted.append({"name": name, "category": entry["category"],
                            "size_bytes": entry["size_bytes"]})
        except Exception as exc:  # noqa: BLE001 - 逐条收集，不中断整体
            failed.append(f"{name}（{type(exc).__name__}: {exc}）")

    remaining = {c.name for c in client.get_collections().collections}
    report = {
        "at": time.time(),
        "authorized_by": args.authorized_by,
        "inventory": str(Path(args.inventory).relative_to(ROOT)),
        "requested": len(targets),
        "deleted": deleted,
        "deleted_count": len(deleted),
        "freed_bytes": sum(item["size_bytes"] for item in deleted),
        "failed": failed,
        "collections_remaining": len(remaining),
        "protected_kept": inventory["summary"]["C_in_use"]["collections"],
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n已删 {report['deleted_count']} 个，释放 "
          f"{round(report['freed_bytes'] / 1024**3, 2)} GiB")
    print(f"端点剩余集合 = {report['collections_remaining']}"
          f"（期望 {report['protected_kept']}）")
    print(f"审计：{REPORT.relative_to(ROOT)}")
    if failed:
        print("\n未完成项：", file=__import__("sys").stderr)
        for item in failed[:20]:
            print("  - " + item, file=__import__("sys").stderr)
        return 1
    if report["collections_remaining"] != report["protected_kept"]:
        print("警告：剩余集合数与保留数不一致，请复核", file=__import__("sys").stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
