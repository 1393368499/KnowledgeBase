"""O-01：停用信息库 = 不可读（2026-11-11 复审语义收敛）。

停用（``is_active=False``）后：文件列表隐藏、详情不可读；重新启用后恢复。
否则"停用"只是从下拉里消失，资料仍然可被深链读取。
"""

from __future__ import annotations

import pytest

from apps.knowledge.models import MemberRole

pytestmark = pytest.mark.django_db


def test_inactive_library_files_are_not_listed_or_readable(
    api, admin_user, make_library, make_file, grant_member
):
    library = make_library(code="inactive-demo", name="停用演示库")
    grant_member(library, admin_user, role=MemberRole.ADMIN)
    record = make_file(library, name="停用库资料.md")
    api.force_authenticate(user=admin_user)

    listing = api.get("/api/v1/knowledge/files/")
    assert listing.status_code == 200, listing.content[:300]
    assert record.pk in {row["id"] for row in listing.json()["results"]}

    library.is_active = False
    library.save(update_fields=["is_active"])

    hidden = api.get("/api/v1/knowledge/files/")
    assert hidden.status_code == 200
    assert record.pk not in {row["id"] for row in hidden.json()["results"]}, (
        "停用信息库的资料仍然出现在列表里"
    )
    detail = api.get(f"/api/v1/knowledge/files/{record.pk}/")
    assert detail.status_code == 404, "停用信息库的资料仍然可读"

    library.is_active = True
    library.save(update_fields=["is_active"])
    restored = api.get("/api/v1/knowledge/files/")
    assert record.pk in {row["id"] for row in restored.json()["results"]}
