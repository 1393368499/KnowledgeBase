"""真实 LLM 冒烟（默认预算）：证明"非空 + finish_reason + usage"而不是只看 HTTP 200。

同时验证：不给 tools 时也能拿到正文；空答复会被显式判为失败。
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

from apps.planner.llm import LLMError, LlmClient, default_profile

profile = default_profile()
print("预算: max_tokens =", profile.max_tokens, " timeout =", profile.timeout_seconds)

client = LlmClient()
started = time.time()
try:
    reply = client.chat(
        [
            {"role": "system", "content": "你是一个简洁的中文助手。"},
            {"role": "user", "content": "请只回复四个字：连接正常"},
        ]
    )
except LLMError as exc:
    print("模型调用失败（显式失败，不是静默成功）：", exc)
    raise SystemExit(3)

elapsed = time.time() - started
print(json.dumps({
    "seconds": round(elapsed, 2),
    "content_len": len(reply.content),
    "content_head": reply.content[:120],
    "finish_reason": reply.finish_reason,
    "reasoning_tokens": reply.reasoning_tokens,
    "usage": reply.usage,
    "tool_calls": len(reply.tool_calls),
}, ensure_ascii=False, indent=2))

ok = bool(reply.content.strip()) and reply.finish_reason != "length"
print("\n判定:", "PASS 有非空正文且未因长度截断" if ok else "FAIL 空正文或被截断")
raise SystemExit(0 if ok else 1)
