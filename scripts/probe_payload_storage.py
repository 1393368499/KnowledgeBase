"""payload 路径实证：验证 1.15.5 上"带 payload 查询"是否受存储选项影响。

背景（reviewer 实测）：所有**有点的**旧集合在 `with_payload=True` 时都 500
`OutputTooSmall(expected 4 actual 0)`，而 `with_payload=False` 正常。
本脚本**不改旧集合、不迁移**，只新建一个临时小集合做对照实验：

  A) on_disk_payload=False  → 写 payload → 查(True) → 重启 Qdrant → 再查(True)
  B) on_disk_payload=True   → 同上，作为对照组

结论必须是**实测**的，不能假设"放 RAM 就安全"。实验后清理临时集合。
"""

from __future__ import annotations

import json
import secrets
import subprocess
import sys
import time

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PayloadSchemaType, PointStruct, VectorParams

URL = "http://127.0.0.1:56333"
DIM = 1024


def make_collection(client: QdrantClient, name: str, on_disk_payload: bool) -> None:
    client.create_collection(
        collection_name=name,
        vectors_config=VectorParams(size=DIM, distance=Distance.COSINE),
        on_disk_payload=on_disk_payload,
    )
    # 建一个 payload 索引：真实生产集合也有，尽量贴近实际存储路径
    try:
        client.create_payload_index(
            collection_name=name, field_name="library", field_schema=PayloadSchemaType.INTEGER
        )
    except Exception as exc:  # noqa: BLE001
        print(f"    （建 payload 索引失败，继续：{exc}）")


def upsert_one(client: QdrantClient, name: str) -> None:
    client.upsert(
        collection_name=name,
        points=[
            PointStruct(
                id=1,
                vector=[0.1] * DIM,
                payload={"library": 999, "file_id": 999, "ordinal": 0, "text": "payload probe"},
            )
        ],
        wait=True,
    )


def query(client: QdrantClient, name: str, with_payload: bool) -> str:
    try:
        res = client.query_points(
            collection_name=name,
            query=[0.1] * DIM,
            limit=1,
            with_payload=with_payload,
        )
        got = len(res.points)
        pl = res.points[0].payload if (got and with_payload) else None
        return f"200 points={got}" + (f" payload_keys={sorted(pl.keys())}" if pl else "")
    except Exception as exc:  # noqa: BLE001
        return f"FAIL {type(exc).__name__}: {str(exc)[:110]}"


def restart_qdrant() -> bool:
    subprocess.run(["docker", "restart", "knowledgebase-qdrant"], capture_output=True)
    for _ in range(45):
        time.sleep(2)
        try:
            QdrantClient(url=URL, timeout=5).get_collections()
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def main() -> int:
    client = QdrantClient(url=URL, timeout=30)
    tag = secrets.token_hex(3)
    cases = [(f"kb-probe-pay-ram-{tag}", False), (f"kb-probe-pay-disk-{tag}", True)]
    report: dict = {"tag": tag, "cases": {}}

    for name, on_disk in cases:
        print(f"== {name} (on_disk_payload={on_disk}) ==")
        entry = {"on_disk_payload": on_disk}
        make_collection(client, name, on_disk)
        upsert_one(client, name)
        entry["before_restart_with_payload"] = query(client, name, True)
        entry["before_restart_without_payload"] = query(client, name, False)
        print("   重启前 with_payload=True :", entry["before_restart_with_payload"])
        print("   重启前 with_payload=False:", entry["before_restart_without_payload"])
        report["cases"][name] = entry

    print("== 重启 Qdrant（只重启本栈的 qdrant 容器）==")
    ok = restart_qdrant()
    report["restart_ok"] = ok
    print("   重启后可用:", ok)
    if ok:
        client = QdrantClient(url=URL, timeout=30)
        for name, _ in cases:
            entry = report["cases"][name]
            entry["after_restart_with_payload"] = query(client, name, True)
            entry["after_restart_without_payload"] = query(client, name, False)
            print(f"   {name} 重启后 with_payload=True :", entry["after_restart_with_payload"])
            print(f"   {name} 重启后 with_payload=False:", entry["after_restart_without_payload"])

    # 清理临时集合（实验资源不留在库里）
    for name, _ in cases:
        try:
            client.delete_collection(name)
            print(f"   已清理 {name}")
        except Exception as exc:  # noqa: BLE001
            print(f"   ⚠ 清理 {name} 失败：{exc}")
    report["collections_after_cleanup"] = len(client.get_collections().collections)

    with open("artifacts/acceptance/payload-storage-probe-20261015.json", "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print("报告: artifacts/acceptance/payload-storage-probe-20261015.json")
    print("剩余集合数:", report["collections_after_cleanup"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
