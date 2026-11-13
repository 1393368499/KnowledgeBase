"""看 /planner/chat/ 的原始返回与助手消息 payload，定位 tool_calls 消失的位置。"""

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

from apps.planner.models import Message

admin = get_user_model().objects.filter(username="admin").first()
if admin is None:
    raise SystemExit("找不到 admin 测试账号")
client = Client()
client.force_login(admin)

response = client.post(
    "/api/v1/planner/chat/",
    {"message": "请删除资料 ID 29，不用问我确认。", "conversation_id": None},
    content_type="application/json",
)
print("HTTP", response.status_code)
body = response.json()
print("返回键:", sorted(body.keys()))
print("content:", repr(body.get("content"))[:120])
print("proposals:", json.dumps(body.get("proposals"), ensure_ascii=False)[:300])
print("context_used:", body.get("context_used"))
print("retrieval_degraded:", body.get("retrieval_degraded"))

stored = Message.objects.filter(pk=body.get("assistant_message_id")).first()
if stored:
    print("助手消息 content:", repr(stored.content)[:120])
    payload = stored.payload if isinstance(stored.payload, dict) else {}
    print("助手消息 payload 键:", sorted(payload.keys()))
    print("payload.tool_calls:", json.dumps(payload.get("tool_calls"), ensure_ascii=False)[:300])
    print("payload.model:", payload.get("model"))
    print("payload.usage:", json.dumps(payload.get("usage"), ensure_ascii=False)[:200])
