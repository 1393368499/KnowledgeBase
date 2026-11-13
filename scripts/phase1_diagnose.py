"""定位第一阶段暴露的两个真实缺陷。"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "knowledgebase.settings")
django.setup()

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client

from apps.knowledge.models import KnowledgeLibrary

admin = get_user_model().objects.filter(username="admin").first()
client = Client()
client.force_login(admin)

library = KnowledgeLibrary.objects.filter(code__startswith="phase1-accept").order_by("-id").first()
print("样本库:", library.code if library else None)

print("\n--- 缺陷 A：全库撰写 400 的真实原因 ---")
for payload in (
    {"title": "t", "topic": "两个项目质保期对比", "library_codes": [library.code]},
    {"title": "t", "topic": "两个项目质保期对比"},
    {"title": "t", "topic": "两个项目质保期对比", "libraries": [library.code]},
):
    response = client.post(
        "/api/v1/planner/documents/", json.dumps(payload), content_type="application/json"
    )
    body = response.content.decode("utf-8", "replace")[:300]
    print(f"  {sorted(payload)} -> HTTP {response.status_code} {body}")

print("\n--- 缺陷 B：Qdrant 客户端模式与集合 ---")
from apps.knowledge.vectorindex import build_qdrant_client

client_q = build_qdrant_client()
print("  client:", type(client_q).__name__)
print("  QDRANT_URL =", repr(getattr(settings, "QDRANT_URL", None)))
print("  QDRANT_PATH =", repr(getattr(settings, "QDRANT_PATH", None)))
try:
    names = [c.name for c in client_q.get_collections().collections]
    print("  集合:", names)
    for name in names:
        info = client_q.get_collection(name)
        print(f"    {name}: points={info.points_count} vectors={info.config.params.vectors}")
except Exception as exc:  # noqa: BLE001
    print("  取集合失败:", exc)
