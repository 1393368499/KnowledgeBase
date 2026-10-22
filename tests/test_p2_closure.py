"""P2 收尾三项的回归：静态资源版本号、parse_error 角色隐藏、测试残留清理。

对应用户实测发现：
1. 改了 JS 但浏览器仍用旧脚本 → 静态资源必须带版本号（可靠缓存失效）。
2. ``parse_error`` 在服务端未按角色隐藏 → 普通用户能看到技术诊断。
3. 真实 worker 用例留下的测试 collection 没被清干净。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from django.conf import settings
from django.test import Client, override_settings
from django.urls import reverse

from apps.knowledge.models import IngestedFile, KnowledgeLibrary, ParseStatus

pytestmark = pytest.mark.django_db


# ------------------------------------- 1) 静态资源缓存失效 ----


def test_asset_version_is_computed(settings):
    """版本号必须是"基于文件 mtime"的字符串，改了脚本就会变。"""
    assert settings.KB_ASSET_VERSION
    assert re.match(r"^\d+-", settings.KB_ASSET_VERSION), settings.KB_ASSET_VERSION


def test_pages_include_versioned_script_urls(admin_user):
    """两个页面引用的脚本都必须带 ``?v=``，否则浏览器会继续用旧缓存。"""
    client = Client()
    client.login(username="admin", password="pw-not-a-secret-123")
    for name, must_have in (
        ("webui:files", ["kb-common.js", "kb-files.js"]),
        ("webui:search", ["kb-common.js", "kb-search.js"]),
    ):
        body = client.get(reverse(name)).content.decode("utf-8")
        for asset in must_have:
            pattern = re.compile(rf"/static/webui/{re.escape(asset)}\?v=[^\"']+")
            assert pattern.search(body), f"{name} 的 {asset} 没有版本号"


def test_asset_version_changes_when_a_script_changes(tmp_path, monkeypatch):
    """版本号确实随静态文件变化（不是写死的常量），且**绝不写源码目录**。

    为什么改成 tmp 目录（旧实现在**源码静态目录**上直接 ``os.utime``）：
    独立审查的 Linux 容器把源码**只读**绑定到 ``/app``，旧用例必然
    ``OSError: [Errno 30] Read-only file system``；而且"测试去改源码树"
    本身就是坏实践（即使可写也是污染）。
    现在把 ``_asset_version()`` 唯一的路径来源 ``PROJECT_ROOT`` 指向
    ``tmp_path`` 下的同构静态目录，于是：
      * mtime 完全由用例掌控 → 真实测出"文件一变版本号就变"；
      * 断言版本号**取的是最新 mtime**（不只是"变了"，还要求变对）；
      * 真实源码静态目录在整个用例期间一位不改（末尾做 mtime_ns 快照比对）。
    """
    from config import settings as settings_module

    real_static = (
        Path(settings_module.PROJECT_ROOT) / "apps" / "webui" / "static" / "webui"
    )
    real_before = {p: p.stat().st_mtime_ns for p in real_static.iterdir() if p.is_file()}
    assert real_before, "静态目录应当有文件"

    # 同构的临时静态根：结构必须是 <PROJECT_ROOT>/apps/webui/static/webui
    fake_static = tmp_path / "apps" / "webui" / "static" / "webui"
    fake_static.mkdir(parents=True)
    for name in ("kb-common.js", "kb-files.js"):
        (fake_static / name).write_text("// v1\n", encoding="utf-8")
    monkeypatch.setattr(settings_module, "PROJECT_ROOT", tmp_path)

    before = settings_module._asset_version()
    # 设成一个**确实超过当前最大值**的 mtime，确保它成为"最新文件"。
    # 注意：不能写成 original + 10000 —— 新增静态文件后，别的文件 mtime
    # 可能已经晚于该文件 + 10000，断言就会因为"本来就不是最新"而误报失败
    # （实测踩到）。
    target = fake_static / "kb-common.js"
    newest = max(p.stat().st_mtime for p in fake_static.iterdir() if p.is_file())
    os.utime(target, (newest + 10_000, newest + 10_000))
    after = settings_module._asset_version()

    assert after != before, "文件变了版本号却没变，缓存失效就是假的"
    expected = int(max(p.stat().st_mtime for p in fake_static.iterdir() if p.is_file()))
    assert after.startswith(f"{expected}-"), (
        f"版本号没有跟最新 mtime 走：{after}，期望前缀 {expected}-"
    )

    real_after = {p: p.stat().st_mtime_ns for p in real_static.iterdir() if p.is_file()}
    assert real_after == real_before, "用例改动了源码静态目录（只读挂载下会直接报错）"


# --------------------------------- 2) parse_error 按角色隐藏 ----


def _failed_record(library) -> IngestedFile:
    return IngestedFile.objects.create(
        library=KnowledgeLibrary.objects.get(pk=library.pk),
        original_name="失败样本.txt",
        content_sha256="b" * 64,
        size_bytes=10,
        raw_size_bytes=10,
        parse_status=ParseStatus.FAILED,
        parse_attempts=1,
        parse_error="AttributeError: 'X' object has no attribute 'name'（内部细节）",
    )


def test_parse_error_visible_to_staff(api, admin_user, library):
    record = _failed_record(library)
    api.force_authenticate(user=admin_user)
    payload = api.get(f"/api/v1/knowledge/files/{record.pk}/").json()
    assert "AttributeError" in payload["parse_error"]


def test_parse_error_hidden_from_regular_user(api, user, library, grant_member):
    """普通读者拿到的 parse_error 必须是空串（服务端隐藏，不是前端遮罩）。"""
    record = _failed_record(library)
    grant_member(library, user)
    api.force_authenticate(user=user)
    response = api.get(f"/api/v1/knowledge/files/{record.pk}/")
    assert response.status_code == 200, response.content[:200]
    payload = response.json()
    assert payload["parse_error"] == ""
    # 但"处理失败"这个状态本身仍然可见（用户需要知道）
    assert payload["parse_status"] == ParseStatus.FAILED


def test_parse_error_hidden_in_list_for_regular_user(api, user, library, grant_member):
    _failed_record(library)
    grant_member(library, user)
    api.force_authenticate(user=user)
    body = api.get("/api/v1/knowledge/files/").json()
    rows = body.get("results", body)
    assert rows
    assert all(row["parse_error"] == "" for row in rows)


# ------------------------------- 3) 测试 collection 命名空间 ----


def test_test_rows_never_use_production_collection_prefix(db, library, make_file):
    """本测试进程里创建的 collection 名都必须落在测试命名空间内。

    这条在真实 worker 用例里由 autouse 夹具保证；这里做一个通用断言，
    防止以后有人新增用例时忘了覆盖前缀，把测试向量写进正式命名空间。
    """
    from apps.knowledge import vectorindex

    prefix = vectorindex.collection_prefix()
    # **会话级唯一前缀**：整个 pytest 会话跑在 `kb-<run_uuid>` 下，
    # 与 `RunResources.prefix`（`kb-<uuid>-`）对齐，于是 `collection_name()`
    # 生成的 `kb-<uuid>-1-g1` 正好落在清理器的独占前缀内（可被回收）。
    # 绝不使用生产默认前缀 `kb`（否则会与主实例历史集合撞名，如哨兵 kb-1-g1）。
    assert prefix != "kb", f"测试进程不得使用生产前缀：{prefix}"
    assert prefix.startswith("kb-"), f"应为会话级唯一前缀，实际：{prefix}"
    assert len(prefix) > len("kb-") + 8, f"前缀应含完整 run_uuid：{prefix}"
