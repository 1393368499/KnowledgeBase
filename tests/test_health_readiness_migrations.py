"""就绪探针必须能看出"库还没跑 migrate"。

背景（部署审查 B3）：三个镜像都没有 ENTRYPOINT，Compose 原先没有任何服务跑 migrate，
而 ready 探针只 `SELECT 1` —— 空库照样 200/healthy，用户一访问就 500。这是假绿灯，
所以探针改成真去比对 Django 的迁移计划。
"""

from knowledgebase import health_urls

import pytest

# 探针本身要真的访问数据库（这正是它的职责），所以整个模块都要 DB 权限
pytestmark = pytest.mark.django_db


def test_migrations_ok_when_applied(api):
    body = api.get("/api/v1/health/ready/").json()
    assert body["status"] == "ok"
    assert body["checks"]["migrations"] == "ok"


def test_pending_migrations_reads_real_plan():
    # 真跑一次迁移计划（不是 mock）：测试库已经迁完 → 必须为空
    assert health_urls._pending_migrations() == []


def test_pending_migration_degrades(api, monkeypatch):
    monkeypatch.setattr(health_urls, "_pending_migrations", lambda: ["knowledge.9999_fake"])
    response = api.get("/api/v1/health/ready/")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    # 只回状态词：迁移名、异常文本、绝对路径都不能出现在未认证可访问的响应体里
    # （2026-10-22 审计：探针端点信息泄露）。
    assert body["checks"]["migrations"] == "pending"
    assert "knowledge.9999_fake" not in response.content.decode()


def test_probe_error_degrades_instead_of_crashing(api, monkeypatch):
    def boom():
        raise RuntimeError("无法读取迁移图")

    monkeypatch.setattr(health_urls, "_pending_migrations", boom)
    response = api.get("/api/v1/health/ready/")
    assert response.status_code == 503
    body = response.json()
    assert body["checks"]["migrations"] == "error"
    assert "无法读取迁移图" not in response.content.decode()
