"""校验负载测试的三道 fail-closed 闸门 + /guide/ 环境自述。

只读：不写业务数据、不发压测流量。
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.loadtest_settings")
os.environ.setdefault("KB_LOADTEST_DB_NAME", "kb_loadtest_verify")
os.environ.setdefault("KB_LOADTEST_QDRANT_PREFIX", "kb-lt-verify")
os.environ.setdefault("KB_LOADTEST_BROKER_DB", "14")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.test import Client  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(name)


# ---- 1) settings 落地隔离 ----
check("落点是隔离 PG 库", settings.DATABASES["default"]["NAME"] == "kb_loadtest_verify",
      settings.DATABASES["default"]["NAME"])
check("落点不是 SQLite", "sqlite" not in settings.DATABASES["default"]["ENGINE"])
check("向量前缀是压测专用", settings.KB_QDRANT_COLLECTION_PREFIX == "kb-lt-verify",
      settings.KB_QDRANT_COLLECTION_PREFIX)
check("模型已禁用", settings.KB_MODEL_CALLS_DISABLED is True)
check("load_test_mode 已开", settings.KB_LOAD_TEST_MODE is True)
check("禁止派发", settings.KB_DISPATCH_DISABLED is True)
check("broker 是独立 Redis DB", settings.CELERY_BROKER_URL.endswith("/14"),
      settings.CELERY_BROKER_URL)

# ---- 2) 禁止派发真的生效（不产生队列消息）----
from config.celery import dispatch_processing  # noqa: E402

result = dispatch_processing(999999)
check("派发被拒绝且不产生 task_id",
      result.get("dispatched") is False and result.get("reason") == "dispatch_disabled",
      str(result))

# ---- 3) /guide/ 环境自述（真实 HTTP 经 Django test client）----
from django.contrib.auth import get_user_model  # noqa: E402

user = get_user_model().objects.get(username="loadbot")
client = Client()
client.force_login(user)
response = client.get("/api/v1/planner/guide/")
check("GET /guide/ 成功", response.status_code == 200, str(response.status_code))
environment = (response.json() or {}).get("environment") or {}
check("guide 声明 model_calls_disabled=True", environment.get("model_calls_disabled") is True)
check("guide 声明 load_test_mode=True", environment.get("load_test_mode") is True)
check("guide 声明 dispatch_disabled=True", environment.get("dispatch_disabled") is True)
check("guide 暴露向量前缀", environment.get("qdrant_collection_prefix") == "kb-lt-verify",
      str(environment.get("qdrant_collection_prefix")))
check("guide 暴露库名（便于确认压的是哪套）",
      environment.get("database_name") == "kb_loadtest_verify")

# ---- 4) 压测脚本的三道闸门：正例 + 负例 ----
sys.path.insert(0, os.path.join(os.getcwd(), "scripts"))
import load_probe  # noqa: E402


class FakeSession:
    """按需返回受控响应的假会话。"""

    def __init__(self, guide_env: dict, catalog: list | None = None) -> None:
        self.guide_env = guide_env
        self.catalog = catalog or []

    def request(self, method, path, **kwargs):
        if path.endswith("/guide/"):
            return 200, {"environment": self.guide_env}
        if path.endswith("/catalog/"):
            return 200, {"results": self.catalog}
        return 200, {}


GOOD_ENV = {
    "model_calls_disabled": True,
    "load_test_mode": True,
    "dispatch_disabled": True,
    "qdrant_collection_prefix": "kb-lt-verify",
}

# 正例：全部满足 → 不抛
try:
    load_probe.assert_models_disabled("http://127.0.0.1:8011", FakeSession(GOOD_ENV))
    check("正例：隔离达标时放行", True)
except SystemExit as exc:
    check("正例：隔离达标时放行", False, str(exc))

# 负例 1：模型未禁用（正式服务）→ 拒绝
for label, env, expect in [
    ("模型未禁用被拒", {**GOOD_ENV, "model_calls_disabled": False}, "model_calls_disabled"),
    ("非测试实例被拒", {**GOOD_ENV, "load_test_mode": False}, "load_test_mode"),
    ("未禁止派发被拒", {**GOOD_ENV, "dispatch_disabled": False}, "dispatch_disabled"),
    ("业务向量前缀被拒", {**GOOD_ENV, "qdrant_collection_prefix": "kb"}, "前缀"),
    ("缺 environment 被拒", {}, "environment"),
]:
    try:
        load_probe.assert_models_disabled("http://127.0.0.1:8011", FakeSession(env))
        check(label, False, "竟然通过了")
    except SystemExit as exc:
        check(label, expect in str(exc), str(exc)[:70])

# ---- 5) upload 库前缀（严格 load-test-）----
for code, expect_pass in [
    ("load-test-main", True),
    ("contract-archive", False),
    ("loadtest-nodash", False),
    ("", False),
]:
    try:
        load_probe.assert_load_test_library(code)
        ok = expect_pass
        detail = "放行"
    except SystemExit as exc:
        ok = not expect_pass
        detail = str(exc)[:60]
    check(f"upload 库校验 {code!r}", ok, detail)

# ---- 6) execute 只读白名单（含服务端目录交叉核对）----
try:
    load_probe.assert_read_only_intent(
        "delete_file", FakeSession(GOOD_ENV, [{"intent": "delete_file", "is_mutation": True}])
    )
    check("execute 拒绝写操作 delete_file", False, "竟然通过了")
except SystemExit as exc:
    check("execute 拒绝写操作 delete_file", "只读白名单" in str(exc), str(exc)[:60])

try:
    load_probe.assert_read_only_intent("get_stats", FakeSession(GOOD_ENV, []))
    check("execute 放行只读 get_stats", True)
except SystemExit as exc:
    check("execute 放行只读 get_stats", False, str(exc))

# 白名单里有、但服务端目录说是写操作 → 按更严格一方处理
try:
    load_probe.assert_read_only_intent(
        "get_stats", FakeSession(GOOD_ENV, [{"intent": "get_stats", "is_mutation": True}])
    )
    check("execute 与目录冲突时按更严格处理", False, "竟然通过了")
except SystemExit as exc:
    check("execute 与目录冲突时按更严格处理", "is_mutation" in str(exc), str(exc)[:60])

print("\n失败项:", failures if failures else "无")
raise SystemExit(1 if failures else 0)
