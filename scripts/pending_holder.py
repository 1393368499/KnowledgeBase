"""测试助手：模拟"已 create 但死在 confirm 之前"的进程。

行为：
  1 以 owner 身份建 run（持有 run.lock）；
  2 `declare` 一个集合（**故意不 confirm**）；
  3 **真的**在隔离端点上把集合建出来；
  4 顺带在本 run 的 raw 目录写一个临时文件；
  5 写 marker，然后一直 sleep —— 由测试 kill 掉，模拟崩溃。

用法：python scripts/pending_holder.py <run_id> <runs_dir> <marker_path>
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_resources as rr  # noqa: E402


def main() -> int:
    run_id, runs_dir, marker = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
    endpoint = os.environ.get("KB_TEST_QDRANT_URL", "")
    res = rr.RunResources.new(
        endpoint=endpoint, identity="pending-holder", run_uuid=run_id, runs_dir=runs_dir
    )
    name = res.name("pending")
    # **关键**：只 declare，不 confirm —— 正是崩溃现场的形态
    res.declare("qdrant_collection", name)

    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, VectorParams

    client = QdrantClient(url=endpoint, timeout=20)
    client.create_collection(name, vectors_config=VectorParams(size=4, distance=Distance.COSINE))

    # 本 run 的 raw 目录里也留个临时件（只读，模拟不可变原件）
    raw = res.dir / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    blob = raw / "pending-blob.bin"
    blob.write_bytes(b"x" * 1024)
    try:
        os.chmod(blob, 0o444)      # 只读，验证清理要清只读位
    except OSError:
        pass
    res.declare("dir", str(raw))
    res.confirm("dir", str(raw))   # 目录这条**正常** confirm，便于检验两类并存

    marker.write_text(
        json.dumps({"run_id": run_id, "collection": name, "raw": str(raw)},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    while True:
        res.beat()
        time.sleep(0.5)


if __name__ == "__main__":
    raise SystemExit(main())
