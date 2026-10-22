"""金额校验与分页覆盖（离線，不调用模型）。"""

from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.knowledge.models import IngestedFile, KnowledgeLibrary, LibraryMember

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin():
    return get_user_model().objects.create_user(
        username=f"pg-{uuid.uuid4().hex[:8]}", password="x", is_staff=True
    )


@pytest.fixture
def library(admin):
    item = KnowledgeLibrary.objects.create(code=f"lib-{uuid.uuid4().hex[:6]}", name="分页库")
    LibraryMember.objects.create(library=item, user=admin, role="admin")
    return item


def make_files(library, count, status="done"):
    IngestedFile._base_manager.bulk_create([
        IngestedFile(
            library=library,
            original_name=f"p-{index}.txt",
            content_sha256=uuid.uuid4().hex,
            size_bytes=10,
            file_type="txt",
            parse_status=status,
        )
        for index in range(count)
    ])


# ------------------------------------------------------------ 分页 ----

def test_page_size_is_honoured_and_capped(client, admin, library):
    make_files(library, 25)
    client.force_login(admin)

    response = client.get("/api/v1/knowledge/files/?page_size=10")
    assert response.status_code == 200
    assert len(response.json()["results"]) == 10, "page_size=10 未生效"
    assert response.json()["count"] == 25

    # 页大小有上限，不能一次拉爆
    capped = client.get("/api/v1/knowledge/files/?page_size=100000")
    assert len(capped.json()["results"]) <= 200, "缺少 max_page_size 上限"


def test_second_page_does_not_repeat_first(client, admin, library):
    make_files(library, 25)
    client.force_login(admin)
    first = client.get("/api/v1/knowledge/files/?page_size=10&page=1").json()["results"]
    second = client.get("/api/v1/knowledge/files/?page_size=10&page=2").json()["results"]
    assert {row["id"] for row in first}.isdisjoint({row["id"] for row in second})


def test_all_pages_cover_every_row(client, admin, library):
    """界面按每页 10 条翻页，必须能覆盖到全部记录（验证"分页可达"）。"""
    make_files(library, 25)
    client.force_login(admin)
    seen = set()
    for page in (1, 2, 3):
        rows = client.get(
            f"/api/v1/knowledge/files/?page_size=10&page={page}"
        ).json()["results"]
        seen.update(row["id"] for row in rows)
    assert len(seen) == 25, f"翻页只覆盖到 {len(seen)} 条"


# ---------------------------------------------------- 金额校验 ----

@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "-1", "abc"])
def test_bad_amount_is_rejected_before_any_blob_write(
    client, admin, library, monkeypatch, bad
):
    """金额非法必须 400，且**证明**校验发生在落盘之前。

    做法：把 ``store_upload`` 换成"一旦被调用就失败"的哨兵。
    如果校验晚于落盘，这个测试会因为哨兵抛错而红——所以它证明的是顺序，
    不是"事后检查有没有留下文件"。
    """
    from apps.knowledge import views

    called = {"store_upload": 0}

    def sentinel(*args, **kwargs):
        called["store_upload"] += 1
        raise AssertionError("非法金额竟然走到了落盘（store_upload）")

    monkeypatch.setattr(views, "store_upload", sentinel)
    client.force_login(admin)

    response = client.post(
        "/api/v1/knowledge/files/upload/",
        {
            "file": SimpleUploadedFile("a.txt", b"hello", content_type="text/plain"),
            "library": library.code,
            "amount_wan": bad,
        },
    )
    assert response.status_code == 400, f"{bad} 应被拒绝，实际 {response.status_code}"
    assert called["store_upload"] == 0, "校验没有拦住落盘"
    assert IngestedFile._base_manager.count() == 0


def test_finite_non_negative_amount_is_accepted(client, admin, library):
    client.force_login(admin)
    response = client.post(
        "/api/v1/knowledge/files/upload/",
        {
            "file": SimpleUploadedFile("ok.txt", b"hello", content_type="text/plain"),
            "library": library.code,
            "amount_wan": "12.5",
        },
    )
    assert response.status_code in (200, 201, 202), response.content[:200]


# ------------------------------------------- serializer 字段 ----

def test_file_serializer_exposes_summary_and_amount(client, admin, library):
    from django.utils import timezone

    file = IngestedFile._base_manager.create(
        library=library,
        original_name="s.txt",
        content_sha256=uuid.uuid4().hex,
        size_bytes=5,
        file_type="txt",
        parse_status="done",
        amount_wan=88.0,
        parsed_at=timezone.now(),
    )
    client.force_login(admin)
    row = client.get(f"/api/v1/knowledge/files/{file.pk}/").json()
    assert "summary" in row, "序列化缺 summary"
    assert float(row["amount_wan"]) == 88.0
    assert row["parsed_at"], "序列化缺 parsed_at（界面时间列要用它）"
