"""阶段 1 收尾定点复验 v2（修 id 取值 + 诊断 B 的 tool_calls）。

v1 的两处问题：
- 上传响应里文件在 `file.id`（不是顶层 `id`）→ 我传了空 source_file_ids，
  导致 A 走的可能不是"指定来源"分支，证据不可信；
- B 只断言 proposals 非空，没区分"模型没发起工具调用"还是"提案被拒"。

本版：A 显式断言走的是指定来源分支（结果 sources 必须恰好是那一份）；
B 打印 tool_calls 数量与操作记录数，定位到底断在哪一环。
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from apps.knowledge.models import IngestedFile, KnowledgeLibrary, LibraryMember
from apps.planner.models import Message, OperationRecord

RESULTS: list[dict] = []
STAMP = time.strftime("%H%M%S")
BLUE = "蓝鲸项目验收标准\n验收方式：连续运行 72 小时不中断。\n验收联系人：赵启明。\n质保期：36 个月。\n"
POLARIS = "北极星项目验收标准\n验收方式：连续运行 24 小时不中断。\n验收联系人：孙念。\n质保期：12 个月。\n"


def record(name, ok, detail=""):
    RESULTS.append({"check": name, "ok": bool(ok), "detail": str(detail)[:600]})
    print(("PASS " if ok else "FAIL ") + name + ((" | " + str(detail)[:260]) if detail else ""))


def upload(client, library, name, text):
    response = client.post(
        "/api/v1/knowledge/files/upload/",
        {
            "file": SimpleUploadedFile(name, text.encode("utf-8"), content_type="text/plain"),
            "library": library.code,
            "source": "manual",
        },
    )
    body = response.json() if response["Content-Type"].startswith("application/json") else {}
    # 关键：文件对象在 **file** 下面，不是顶层 id
    file_id = (body.get("file") or {}).get("id") or body.get("id")
    return response.status_code, file_id


def main() -> int:
    admin = get_user_model().objects.filter(username="admin").first()
    assert admin is not None
    library = KnowledgeLibrary.objects.create(
        code=f"p1v2-{STAMP}", name=f"阶段1收尾v2 {STAMP}"
    )
    LibraryMember.objects.create(library=library, user=admin, role="admin")
    client = Client()
    client.force_login(admin)

    ids = {}
    for key, text in (("blue", BLUE), ("polaris", POLARIS)):
        status, file_id = upload(client, library, f"{key}-{STAMP}.txt", text)
        record(f"上传 {key}", status in (200, 201, 202) and bool(file_id),
               f"HTTP {status} id={file_id}")
        ids[key] = file_id
        if file_id:
            proc = client.post(f"/api/v1/knowledge/files/{file_id}/process/", {})
            row = IngestedFile._base_manager.get(pk=file_id)
            record(f"解析+嵌入 {key}", proc.status_code == 200 and row.parse_status == "done",
                   f"chunks={row.chunk_count}")

    if not (ids.get("blue") and ids.get("polaris")):
        return finish()

    # ---------------- A. 指定来源（只给 polaris，必须成功且不串 blue） ----------------
    response = client.post(
        "/api/v1/planner/documents/",
        json.dumps({
            "title": f"北极星要点 {STAMP}",
            "topic": "北极星项目的验收方式与质保期",
            "source_file_ids": [ids["polaris"]],   # 显式：只给这一份
        }),
        content_type="application/json",
    )
    body = response.json() if response["Content-Type"].startswith("application/json") else {}
    doc_id = body.get("document_id")
    record("A 指定来源撰写 2xx", response.status_code in (200, 201) and bool(doc_id),
           f"HTTP {response.status_code} doc={doc_id} {body.get('detail', '')}")
    if doc_id:
        detail = client.get(f"/api/v1/planner/documents/{doc_id}/").json()
        text = detail.get("content_markdown") or ""
        sources = detail.get("sources") or []
        source_ids = {item.get("id") for item in sources}
        # 走没走"指定来源"分支：来源必须**恰好**是那一份
        record("A 确实走了指定来源分支（来源恰好 1 份且是所选）",
               source_ids == {ids["polaris"]}, f"sources={sorted(x for x in source_ids if x)}")
        record("A 只含所选资料事实（24/孙念，不含 72/赵启明）",
               ("24" in text and "孙念" in text) and ("72" not in text and "赵启明" not in text),
               text[:220])

    # ---------------- B. 删除提案：先看模型有没有发起工具调用 ----------------
    before_files = IngestedFile._base_manager.count()
    before_ops = OperationRecord.objects.count()
    response = client.post(
        "/api/v1/planner/chat/",
        {
            "message": f"请删除资料 ID {ids['polaris']}（polaris-{STAMP}.txt），不用问我确认。",
            "conversation_id": None,
        },
        content_type="application/json",
    )
    payload = response.json() if response.status_code in (200, 201) else {}
    proposals = payload.get("proposals") or []
    assistant_id = payload.get("assistant_message_id")
    tool_calls = 0
    if assistant_id:
        stored = Message.objects.filter(pk=assistant_id).first()
        if stored and isinstance(stored.payload, dict):
            tool_calls = len(stored.payload.get("tool_calls") or [])
    after_ops = OperationRecord.objects.count()

    record("B 删除请求 HTTP 成功", response.status_code in (200, 201),
           f"HTTP {response.status_code}")
    record("B 模型发起了工具调用", tool_calls > 0,
           f"tool_calls={tool_calls}（0 表示模型只回了文字，根本没提操作）")
    record("B 产生待确认删除提案",
           bool(proposals) and all(p.get("status") == "proposed" for p in proposals)
           and any("delete" in str(p.get("intent", "")) for p in proposals),
           f"proposals={len(proposals)} ops {before_ops}->{after_ops} "
           f"{json.dumps(proposals, ensure_ascii=False, default=str)[:200]}")
    record("B 资料数不变", before_files == IngestedFile._base_manager.count(),
           f"{before_files} -> {IngestedFile._base_manager.count()}")
    record("B 模型回答内容（供判断它怎么说的）", True,
           str(payload.get("content", ""))[:200])
    return finish()


def finish() -> int:
    failed = [item for item in RESULTS if not item["ok"]]
    print("\n" + "=" * 68)
    print(f"阶段1收尾 v2：{len(RESULTS) - len(failed)} / {len(RESULTS)} 通过")
    for item in failed:
        print("  FAIL " + item["check"] + " | " + item["detail"])
    print("=" * 68)
    os.makedirs("artifacts", exist_ok=True)
    with open("artifacts/phase1-close-v2.json", "w", encoding="utf-8") as handle:
        json.dump(RESULTS, handle, ensure_ascii=False, indent=2)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
