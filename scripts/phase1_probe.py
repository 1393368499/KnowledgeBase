"""第一阶段：真实模型链路的现状体检（只读探测，不启停任何服务）。

判定"还缺什么"必须基于**实测**，不能靠推断。
"""

import json
import os
import sys

# 脚本在 scripts/ 下，项目根不在 sys.path 里 → 需要显式加入才能 import knowledgebase
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "knowledgebase.settings")
django.setup()

from django.conf import settings

services = getattr(settings, "MODEL_SERVICES", {}) or {}
print("KB_MODEL_CALLS_DISABLED =", getattr(settings, "KB_MODEL_CALLS_DISABLED", None))
print("KB_UPLOAD_PROCESS_MODE  =", getattr(settings, "KB_UPLOAD_PROCESS_MODE", None))
print("KB_TEST_ADAPTER        =", repr(getattr(settings, "KB_TEST_ADAPTER", "")))
print()
for name in ("llm", "embedding", "rerank", "ocr"):
    item = services.get(name) or {}
    print(f"{name:10} model={item.get('model','-')!r} base_url={item.get('base_url','-')!r}")

print()
print("--- 各服务真实连通性（HTTP，只读） ---")


def probe(url, payload=None, method="GET"):
    import urllib.error
    import urllib.request

    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read()[:200].decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()[:200].decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


for name in ("llm", "embedding", "rerank", "ocr"):
    item = services.get(name) or {}
    base = item.get("base_url") or ""
    if not base:
        print(f"{name:10} 未配置")
        continue
    if name == "llm":
        status, body = probe(base.rstrip("/") + "/models")
    else:
        status, body = probe(base.rstrip("/") + "/health")
    print(f"{name:10} {status} {body[:120]}")
