"""阶段 1 收尾：真实多轮验证「只读核对 → 再删除提案」。

流程（全部走真实模型，用同一个 conversation）：
1. 第 1 轮：请查看指定资料 → 期望 read_file 提案 → **按既有一次确认**执行
   （只读，不做任何删除）；
2. 第 2 轮（同 conversation）："目标已核对，请提出删除这份资料的待确认提案"
   → 核验 delete_file、目标精确、status=proposed、required_confirmations=3、资料仍在。

顺带实际验证新的多轮历史摘要：第 1 轮执行后，历史摘要必须有内容，
否则第 2 轮"这份资料"就没有指代依据。
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "knowledgebase.settings")
django.setup()

from django.contrib.auth import get_user_model
from django.test import Client

from apps.knowledge.models import IngestedFile
from apps.planner.models import Conversation, OperationRecord
from apps.planner.services import executed_history_summary

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append({"check": name, "ok": bool(ok), "detail": str(detail)[:500]})
    print(("PASS " if ok else "FAIL ") + name + ((" | " + str(detail)[:240]) if detail else ""))


def main() -> int:
    admin = get_user_model().objects.filter(username="admin").first()
    if admin is None:
        print("找不到 admin 测试账号")
        return 2
    # 用最近一批验收样本里的那一份（不新建、不删除）
    record_row = (
        IngestedFile._base_manager.filter(
            original_name__startswith="polaris-", parse_status="done"
        )
        .order_by("-id")
        .first()
    )
    if record_row is None:
        print("找不到可用的验收样本")
        return 2
    file_id = record_row.pk
    name = record_row.original_name
    print(f"验收样本: id={file_id} name={name} library={record_row.library.code}\n")

    client = Client()
    client.force_login(admin)

    # ---------------- 第 1 轮：只读核对 ----------------
    first = client.post(
        "/api/v1/planner/chat/",
        {"message": f"请先查看资料 ID {file_id} 的详情。", "conversation_id": None},
        content_type="application/json",
    )
    body1 = first.json()
    conv_id = body1.get("conversation_id")
    proposals1 = body1.get("proposals") or []
    record(
        "第1轮：得到只读提案",
        first.status_code == 201 and bool(proposals1),
        f"props={[p.get('intent') for p in proposals1]}",
    )
    read_prop = next((p for p in proposals1 if p.get("intent") == "read_file"), None)
    record("第1轮：提案是 read_file 且指向该资料", bool(read_prop) and
           (read_prop or {}).get("params", {}).get("file_id") == file_id,
           json.dumps(read_prop, ensure_ascii=False)[:200] if read_prop else "无")

    if read_prop:
        op_id = read_prop["operation_id"]
        # 既有的一次确认流程：resume 拿一次性凭证 → confirm → execute
        resumed = client.post(f"/api/v1/planner/operations/{op_id}/resume/", {},
                              content_type="application/json")
        token = (resumed.json() or {}).get("confirm_token")
        confirmed = client.post(
            f"/api/v1/planner/operations/{op_id}/confirm/",
            json.dumps({"token": token, "typed_name": name}),
            content_type="application/json",
        )
        record("第1轮：一次确认成功", confirmed.status_code == 200,
               f"HTTP {confirmed.status_code} {(confirmed.json() or {}).get('detail','')}")
        executed = client.post(f"/api/v1/planner/operations/{op_id}/execute/", {},
                               content_type="application/json")
        record("第1轮：只读执行成功且未删除", executed.status_code == 200
               and IngestedFile._base_manager.filter(pk=file_id).exists(),
               f"HTTP {executed.status_code}")
        op = OperationRecord.objects.filter(pk=op_id).first()
        record("第1轮：操作落成 executed", op is not None and op.status == "executed",
               f"status={getattr(op, 'status', None)}")

    # ---------------- 历史摘要必须真的有内容 ----------------
    conversation = Conversation.objects.filter(pk=conv_id).first()
    summary = executed_history_summary(admin, conversation) if conversation else ""
    record("多轮历史摘要已包含刚执行的操作（第2轮指代依据）",
           "read_file" in summary or name in summary, summary[:220])

    # ---------------- 第 2 轮：删除提案 ----------------
    second = client.post(
        "/api/v1/planner/chat/",
        {"message": "目标已核对，请提出删除这份资料的待确认提案。",
         "conversation_id": conv_id},
        content_type="application/json",
    )
    body2 = second.json()
    proposals2 = body2.get("proposals") or []
    del_prop = next((p for p in proposals2 if p.get("intent") == "delete_file"), None)
    record("第2轮：得到 delete_file 提案", bool(del_prop),
           f"props={[p.get('intent') for p in proposals2]}")
    if del_prop:
        record("第2轮：目标精确指向同一份资料",
               del_prop.get("params", {}).get("file_id") == file_id
               and del_prop.get("target_label") == name,
               f"{del_prop.get('params')} label={del_prop.get('target_label')}")
        record("第2轮：状态为待确认", del_prop.get("status") == "proposed",
               f"status={del_prop.get('status')}")
        record("第2轮：要求三次确认", del_prop.get("required_confirmations") == 3,
               f"required={del_prop.get('required_confirmations')}")
        record("第2轮：风险标记为破坏性", del_prop.get("risk") == "destructive",
               f"risk={del_prop.get('risk')}")
    record("第2轮：资料仍然存在（未执行删除）",
           IngestedFile._base_manager.filter(pk=file_id).exists())

    failed = [item for item in RESULTS if not item["ok"]]
    print("\n" + "=" * 68)
    print(f"多轮删除链路：{len(RESULTS) - len(failed)} / {len(RESULTS)} 通过")
    for item in failed:
        print("  FAIL " + item["check"] + " | " + item["detail"])
    print("=" * 68)
    os.makedirs("artifacts", exist_ok=True)
    with open("artifacts/phase1-multiturn-delete.json", "w", encoding="utf-8") as handle:
        json.dump(RESULTS, handle, ensure_ascii=False, indent=2)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
