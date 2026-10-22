"""只读盘点：191 个向量集合分别是"什么"、该不该删、删了会失去什么。

**零变更**：只读 Qdrant 的集合列表 + 只读数据库登记 + 只读磁盘体积。
不删任何东西、不改任何东西。

输出分三类：
  A 孤儿索引  —— 磁盘上有、`kb_vector_collection` 里没有登记（库已被删）
  B 压测垃圾  —— `kb-lt-*` 命名空间（load test 专用）
  C 在用索引  —— 有登记，且其信息库仍存在（**删了会破坏真实检索**）

用法：python scripts/vector_inventory.py [--json 路径]
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COLLECTIONS_DIR = ROOT / "vector_data" / "qdrant" / "collections"


def collection_size_bytes(name: str) -> int:
    """磁盘上该集合目录的真实体积。"""
    target = COLLECTIONS_DIR / name
    if not target.is_dir():
        return 0
    return sum(p.stat().st_size for p in target.rglob("*") if p.is_file())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="向量集合只读盘点（零变更）")
    parser.add_argument("--json", default="artifacts/vector-inventory.json")
    args = parser.parse_args(argv)

    import django
    import os
    import sys

    # 以脚本方式运行时项目根不在 sys.path 上，`config.settings` 会导入失败
    sys.path.insert(0, str(ROOT))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()

    from qdrant_client import QdrantClient

    from apps.knowledge.models import IngestedFile, KnowledgeLibrary, VectorCollection

    client = QdrantClient(url="http://127.0.0.1:56333", timeout=60)
    disk = sorted(item.name for item in client.get_collections().collections)

    registered = {row.name: row for row in VectorCollection.objects.select_related("library")}
    live_library_ids = set(KnowledgeLibrary.objects.values_list("id", flat=True))
    files_per_library = {
        row["library_id"]: row["n"]
        for row in IngestedFile._base_manager.values("library_id").annotate(
            n=__import__("django.db.models", fromlist=["Count"]).Count("id")
        )
    }

    entries = []
    for name in disk:
        size = collection_size_bytes(name)
        row = registered.get(name)
        entry = {
            "name": name,
            "size_bytes": size,
            "registered": row is not None,
            "library_id": row.library_id if row is not None else None,
            "library_exists": (row.library_id in live_library_ids) if row else False,
            "library_files": files_per_library.get(row.library_id, 0) if row else 0,
            "is_loadtest": name.startswith("kb-lt-"),
        }
        if entry["is_loadtest"]:
            entry["category"] = "B_loadtest"
        elif not entry["registered"]:
            entry["category"] = "A_orphan"
        elif not entry["library_exists"]:
            entry["category"] = "A_orphan"      # 登记了但库已不存在
        else:
            entry["category"] = "C_in_use"
        entries.append(entry)

    def total(pred) -> tuple[int, int]:
        picked = [e for e in entries if pred(e)]
        return len(picked), sum(e["size_bytes"] for e in picked)

    summary = {}
    for key, pred in (
        ("A_orphan", lambda e: e["category"] == "A_orphan"),
        ("B_loadtest", lambda e: e["category"] == "B_loadtest"),
        ("C_in_use", lambda e: e["category"] == "C_in_use"),
    ):
        count, size = total(pred)
        summary[key] = {"collections": count, "bytes": size, "gib": round(size / 1024**3, 2)}

    report = {
        "total_collections": len(entries),
        "total_bytes": sum(e["size_bytes"] for e in entries),
        "registered_rows": len(registered),
        "live_libraries": len(live_library_ids),
        "live_files": IngestedFile._base_manager.count(),
        "summary": summary,
        "entries": sorted(entries, key=lambda e: -e["size_bytes"]),
    }

    out = ROOT / args.json
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"磁盘集合总数 = {report['total_collections']}"
          f"（{round(report['total_bytes'] / 1024**3, 2)} GiB）")
    print(f"数据库登记行 = {report['registered_rows']}；在用信息库 = {report['live_libraries']}；"
          f"在用资料 = {report['live_files']}")
    for key, label in (
        ("A_orphan", "A 孤儿索引（库已删/无登记）"),
        ("B_loadtest", "B 压测垃圾（kb-lt-*）"),
        ("C_in_use", "C 在用索引（**删了会破坏真实检索**）"),
    ):
        item = summary[key]
        print(f"  {label}: {item['collections']} 个 = {item['gib']} GiB")
    print(f"报告：{out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
