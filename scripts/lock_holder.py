"""测试助手：在**独立子进程**里持有一个 run 的跨进程锁，直到被指示退出。

用途：证明 `is_alive()` / `recover()` 是靠 **OS 锁**判断存活，而不是靠心跳。
用法：
    python scripts/lock_holder.py <run_uuid> <runs_dir> <ready_marker>
进程会：
  1 打开（或创建）该 run；2 持有锁；3 写 ready 标记；4 一直 sleep 直到被杀。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_resources as rr  # noqa: E402


def main() -> int:
    run_uuid, runs_dir, marker = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
    res = rr.RunResources.new(
        endpoint="http://127.0.0.1:56339", identity="holder", run_uuid=run_uuid,
        runs_dir=runs_dir,
    )
    # 登记一个"待清理"的集合名，供测试验证 recover 只会删本 run 的
    name = res.name("held")
    res.declare("qdrant_collection", name)
    res.confirm("qdrant_collection", name)
    marker.write_text(json_dumps({"run_uuid": run_uuid, "collection": name}), encoding="utf-8")
    while True:                     # 持有锁，直到被 kill
        res.beat()
        time.sleep(0.5)


def json_dumps(doc: dict) -> str:
    import json

    return json.dumps(doc, ensure_ascii=False)


if __name__ == "__main__":
    raise SystemExit(main())
