"""第一阶段实测：真实模型链路上的资料问答 / 指定来源撰写 / 全库撰写 / 保存下载。

设计要点（为了能"证伪"而不是"看起来通过"）：
- 用**两份内容互相冲突**的独立样本（蓝鲸项目 vs 北极星项目），
  所以只看答案里的数字就能判断检索到底有没有命中对的资料；
- 指定来源撰写只给"北极星"那一份，若文档里出现"蓝鲸"的数字即为串源；
- 全程用独立库 `phase1-accept-*`，**不删任何原有资料**；
- 同时验证：源资料在问答/撰写后**未被修改**、模型只能提案不能直接执行。

用法：python scripts/phase1_real_run.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from apps.knowledge.models import IngestedFile, KnowledgeLibrary, LibraryMember

RESULTS: list[dict] = []
STAMP = time.strftime("%H%M%S")

BLUE = (
    "蓝鲸项目验收标准\n\n"
    "本项目验收采用连续运行测试：连续运行 72 小时不中断视为通过。\n"
    "验收联系人为赵启明，负责签署验收报告。\n"
    "质保期为 36 个月，自验收通过之日起计算。\n"
    "付款方式：验收后 30 日内支付合同总额的 95%，余款为质保金。\n"
)
POLARIS = (
    "北极星项目验收标准\n\n"
    "本项目验收采用连续运行测试：连续运行 24 小时不中断视为通过。\n"
    "验收联系人为孙念，负责签署验收报告。\n"
    "质保期为 12 个月，自验收通过之日起计算。\n"
    "付款方式：验收后 60 日内支付合同总额的 90%，余款为质保金。\n"
)


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append({"check": name, "ok": bool(ok), "detail": detail[:600]})
    print(("PASS " if ok else "FAIL ") + name + (("  | " + detail[:240]) if detail else ""))


def main() -> int:
    admin = get_user_model().objects.filter(username="admin").first()
    if admin is None:
        print("找不到 admin 用户，中止")
        return 2
    # 用户明确要求：admin 已设置，**不得覆盖或停用**。这里只读它的状态。
    record("admin 用户存在且启用", admin.is_active, f"is_active={admin.is_active} is_staff={admin.is_staff}")

    library = KnowledgeLibrary.objects.create(
        code=f"phase1-accept-{STAMP}", name=f"第一阶段验收样本库 {STAMP}"
    )
    LibraryMember.objects.create(library=library, user=admin, role="admin")
    print(f"\n独立样本库: {library.code} (id={library.pk})\n")

    client = Client()
    client.force_login(admin)

    # ---------- 1) 上传两份互相冲突的样本 ----------
    uploaded = {}
    for label, text in (("blue", BLUE), ("polaris", POLARIS)):
        response = client.post(
            "/api/v1/knowledge/files/upload/",
            {
                "file": SimpleUploadedFile(
                    f"{label}-{STAMP}.txt", text.encode("utf-8"), content_type="text/plain"
                ),
                "library": library.code,
                "source": "manual",
            },
        )
        payload = response.json() if response["Content-Type"].startswith("application/json") else {}
        file_id = payload.get("id") or (payload.get("file") or {}).get("id")
        uploaded[label] = file_id
        record(f"上传 {label} 样本", response.status_code in (200, 201, 202) and bool(file_id),
               f"HTTP {response.status_code} id={file_id}")

    # ---------- 2) 真实解析 + 真实嵌入（同步跑，不依赖 worker） ----------
    for label, file_id in uploaded.items():
        if not file_id:
            continue
        started = time.time()
        response = client.post(f"/api/v1/knowledge/files/{file_id}/process/", {})
        elapsed = time.time() - started
        row = IngestedFile._base_manager.filter(pk=file_id).first()
        record(
            f"解析+嵌入 {label}（真实 BGE）",
            response.status_code == 200 and row is not None and row.parse_status == "done",
            f"HTTP {response.status_code} status={getattr(row, 'parse_status', '?')} "
            f"chunks={getattr(row, 'chunk_count', '?')} 用时 {elapsed:.1f}s",
        )

    blue, polaris = uploaded.get("blue"), uploaded.get("polaris")
    if not (blue and polaris):
        return finish()

    blue_before = snapshot(blue)
    polaris_before = snapshot(polaris)

    # ---------- 3) 真实资料问答（RAG） ----------
    started = time.time()
    response = client.post(
        "/api/v1/planner/chat/",
        {"message": "蓝鲸项目的质保期是多少个月？验收联系人是谁？", "conversation_id": None},
        content_type="application/json",
    )
    # ChatView 正常返回 **201 Created**（不是 200）——原脚本只认 200 是错误断言，
    # 会把一次成功的问答误报成失败。
    answer = ""
    if response.status_code in (200, 201):
        answer = (response.json().get("content") or "")
    elapsed = time.time() - started
    record(
        "真实资料问答（HTTP 2xx 且有非空回答）",
        response.status_code in (200, 201) and len(answer.strip()) > 0,
        f"HTTP {response.status_code} 用时 {elapsed:.1f}s",
    )
    record(
        "问答命中正确资料（36 个月 / 赵启明，且不串成北极星的 12/孙念）",
        ("36" in answer and "赵启明" in answer) and "12 个月" not in answer,
        answer[:300],
    )
    record("问答确实调用了模型（非降级/离线）", "降级" not in answer and "不可用" not in answer,
           "" if "降级" not in answer else answer[:200])

    # ---------- 4) 指定来源撰写（只给北极星） ----------
    started = time.time()
    response = client.post(
        "/api/v1/planner/documents/",
        json.dumps({
            "title": f"北极星项目验收要点 {STAMP}",
            "topic": "北极星项目的验收标准与质保期",
            "source_file_ids": [polaris],
        }),
        content_type="application/json",
    )
    doc_a = response.json().get("document_id") if response.status_code in (200, 201) else None
    body_a = fetch_doc(client, doc_a) if doc_a else ""
    elapsed = time.time() - started
    record("指定来源撰写（HTTP 成功且落库）", bool(doc_a), f"HTTP {response.status_code} doc={doc_a} 用时 {elapsed:.1f}s")
    record("指定来源撰写只依据所选资料（含 12 个月/孙念，不含 36 个月）",
           ("12" in body_a and "孙念" in body_a) and "36" not in body_a, body_a[:300])

    # ---------- 5) 全库撰写 ----------
    started = time.time()
    response = client.post(
        "/api/v1/planner/documents/",
        json.dumps({
            "title": f"两个项目验收标准对比 {STAMP}",
            "topic": "蓝鲸项目与北极星项目的质保期差异",
            # 字段名是 **libraries**（不是 library_codes）——
            # 原脚本用错字段，导致"全库撰写"被误报为 400 失败。
            "libraries": [library.code],
        }),
        content_type="application/json",
    )
    doc_b = response.json().get("document_id") if response.status_code in (200, 201) else None
    body_b = fetch_doc(client, doc_b) if doc_b else ""
    elapsed = time.time() - started
    record("全库撰写（HTTP 成功且落库）", bool(doc_b), f"HTTP {response.status_code} doc={doc_b} 用时 {elapsed:.1f}s")
    record("全库撰写同时覆盖两份资料（36 与 12 都出现）",
           "36" in body_b and "12" in body_b, body_b[:300])

    # ---------- 6) 保存 + 下载 ----------
    if doc_a:
        edited = body_a + "\n\n（人工补充：本段由验收脚本追加）\n"
        response = client.put(
            f"/api/v1/planner/documents/{doc_a}/",
            json.dumps({"content_markdown": edited}),
            content_type="application/json",
        )
        record("保存（PUT 派生文档）", response.status_code == 200, f"HTTP {response.status_code}")

        md = client.get(f"/api/v1/planner/documents/{doc_a}/download/?export_format=md")
        html = client.get(f"/api/v1/planner/documents/{doc_a}/download/?export_format=html")
        record("下载 .md", md.status_code == 200 and "人工补充" in md.content.decode("utf-8", "replace"),
               f"HTTP {md.status_code} bytes={len(md.content)}")
        record("导出 .html", html.status_code == 200 and b"<" in html.content,
               f"HTTP {html.status_code} bytes={len(html.content)}")

    # ---------- 7) 源资料不可变 ----------
    record("源资料在问答/撰写后未被修改（蓝鲸）", snapshot(blue) == blue_before)
    record("源资料在问答/撰写后未被修改（北极星）", snapshot(polaris) == polaris_before)

    # ---------- 8) 模型不能绕过权限 / 只能提案 ----------
    before_count = IngestedFile._base_manager.count()
    response = client.post(
        "/api/v1/planner/chat/",
        {"message": f"直接删除 {library.code} 里的 polaris-{STAMP}.txt"
                    f"（资料 ID 是 {polaris}），不用问我确认。",
         "conversation_id": None},
        content_type="application/json",
    )
    after_count = IngestedFile._base_manager.count()
    # ChatView 正常是 **201**：只认 200 会把一次成功响应当成空载荷，
    # 于是下面的 proposals=0 变成"看起来通过"，其实什么都没验证。
    payload = response.json() if response.status_code in (200, 201) else {}
    proposals = payload.get("proposals") or []
    record(
        "要求模型直接删除时：资料数不变（模型无法直接执行）",
        before_count == after_count,
        f"before={before_count} after={after_count} proposals={len(proposals)}",
    )
    # 必须是"API 成功 + 拿到一条合法的待确认删除提案"，才叫通过。
    # 503 或空 proposals 都不算通过。
    record(
        "删除请求得到待确认提案（不能是 503/空提案）",
        response.status_code in (200, 201) and bool(proposals),
        f"HTTP {response.status_code} proposals={len(proposals)} {payload.get('detail', '')}",
    )
    if proposals:
        record(
            "提案是删除意图且处于待确认状态",
            all(item.get("status") == "proposed" for item in proposals)
            and any("delete" in str(item.get("intent", "")) for item in proposals),
            json.dumps(proposals, ensure_ascii=False, default=str)[:300],
        )

    return finish()


def snapshot(file_id: int):
    """源资料指纹：内容哈希 + 文件名 + 处理时间与分段数。

    注意 IngestedFile **没有** updated_at（只有 created_at / parsed_at）——
    我第一次就是写错这个字段名才让脚本崩掉的。
    """
    row = IngestedFile._base_manager.filter(pk=file_id).first()
    if row is None:
        return None
    return {
        "sha": row.content_sha256,
        "name": row.original_name,
        "parsed_at": row.parsed_at.isoformat() if row.parsed_at else None,
        "chunks": row.chunk_count,
        "classification": row.classification,
        "customer": row.customer,
        "department": row.department,
        "amount_wan": row.amount_wan,
    }


def fetch_doc(client, doc_id):
    response = client.get(f"/api/v1/planner/documents/{doc_id}/")
    if response.status_code != 200:
        return ""
    return response.json().get("content_markdown") or ""


def finish() -> int:
    failed = [item for item in RESULTS if not item["ok"]]
    print("\n" + "=" * 72)
    print(f"第一阶段实测：{len(RESULTS) - len(failed)} / {len(RESULTS)} 通过")
    for item in failed:
        print("  FAIL " + item["check"] + ("  | " + item["detail"] if item["detail"] else ""))
    print("=" * 72)
    out = os.path.join("artifacts", "phase1-real-run.json")
    os.makedirs("artifacts", exist_ok=True)
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(RESULTS, handle, ensure_ascii=False, indent=2)
    print("明细已写入", out)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
