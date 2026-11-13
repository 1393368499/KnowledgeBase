"""直接看：删除请求下模型到底返回了什么（内容/工具调用/finish_reason）。

B 的现象是 201 + 空 content + tool_calls=0 —— 这与新加的"空答复必须失败"
矛盾（那样应该 503）。必须看原始响应才能定性。
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "knowledgebase.settings")
django.setup()

from apps.planner.llm import LLMError, LlmClient
from apps.planner.services import SYSTEM_PROMPT, tool_schema

tools = tool_schema()
print("工具数:", len(tools))
names = [t.get("function", {}).get("name") for t in tools]
print("含 delete_file:", "delete_file" in names)
print("前几个:", names[:6])

client = LlmClient()
messages = [
    {"role": "system", "content": SYSTEM_PROMPT},
    {"role": "user", "content": "请删除资料 ID 29（polaris-blabla.txt），不用问我确认。"},
]
try:
    reply = client.chat(messages, tools=tools)
except LLMError as exc:
    print("LLMError:", exc)
    raise SystemExit(3)

print(json.dumps({
    "content_len": len(reply.content),
    "content": reply.content[:200],
    "finish_reason": reply.finish_reason,
    "tool_calls": [c.name for c in reply.tool_calls],
    "usage": reply.usage,
}, ensure_ascii=False, indent=2))
