"""搜索页/添加记录页所需 API 的权限与行为回归。

覆盖本轮新增/修正：
- `GET /knowledge/files/facets/`：筛选选项与服务端统计；
- 文件列表的服务端过滤：`nature`（真实字段 classification）/ `amount_min` / `file_type` / `mode`；
- **来源口径**：`session=1` 必须精确等于 ``source == "session"``，
  不能被 ``exclude(source="manual")`` 那种写法把"目录扫描"等别的来源算成会话资料；
- `GET /planner/conversations/`：会话列表是服务端数据源，且**只返回自己的会话**。
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from apps.knowledge.models import (
    DocumentChunk,
    IngestedFile,
    KnowledgeLibrary,
    LibraryMember,
    MemberRole,
    ParseStatus,
)
from apps.planner.models import Conversation, Message

pytestmark = pytest.mark.django_db


@pytest.fixture
def library(db):
    return KnowledgeLibrary.objects.create(code="facets-lib", name="筛选测试库")


@pytest.fixture
def member(library):
    user = get_user_model().objects.create_user(username="facets-user", password="x")
    LibraryMember.objects.create(library=library, user=user, role=MemberRole.ADMIN)
    return user


def _file(library, name, *, source="manual", classification="合同", amount=100.0,
          department="项目部", customer="集团内部", status=ParseStatus.DONE, text=""):
    record = IngestedFile.objects.create(
        library=library,
        original_name=name,
        content_sha256=(name.encode().hex() + "0" * 64)[:64],
        size_bytes=10,
        parse_status=status,
        source=source,
        classification=classification,
        amount_wan=amount,
        department=department,
        customer=customer,
        file_type="pdf",
    )
    if text:
        DocumentChunk.objects.create(file=record, ordinal=0, text=text, token_estimate=2)
        IngestedFile._base_manager.filter(pk=record.pk).update(chunk_count=1)
    return record


def test_source_filter_is_exactly_session(api, library, member):
    """`session=1` 只认 ``source == "session"``，不把目录扫描算进来。"""
    _file(library, "会话来的.pdf", source="session")
    _file(library, "手工上传.pdf", source="manual")
    _file(library, "目录扫描.pdf", source="folder_scan")  # 将来可能新增的来源

    api.force_authenticate(user=member)
    response = api.get("/api/v1/knowledge/files/?session=1")
    assert response.status_code == 200
    names = [row["original_name"] for row in response.json()["results"]]
    assert names == ["会话来的.pdf"], names

    # 显式按来源过滤（添加记录页可以用它列出真正的会话来源）
    response = api.get("/api/v1/knowledge/files/?source=session")
    names = [row["original_name"] for row in response.json()["results"]]
    assert names == ["会话来的.pdf"]


def test_facets_reports_server_side_counts(api, library, member):
    """facets 的计数必须来自**全量**，而不是某一页窗口。"""
    for index in range(12):
        _file(library, f"合同-{index}.pdf", department="项目部", classification="合同")
    _file(library, "方案.pdf", department="技术部", classification="方案")

    api.force_authenticate(user=member)
    response = api.get("/api/v1/knowledge/files/facets/")
    assert response.status_code == 200, response.content[:200]
    body = response.json()
    assert body["total"] == 13
    departments = {row["value"]: row["count"] for row in body["departments"]}
    assert departments["项目部"] == 12 and departments["技术部"] == 1


def test_facets_respects_library_permission(api, library, member):
    """无权用户的 facets 不应泄露别的库的统计。"""
    other = KnowledgeLibrary.objects.create(code="facets-other", name="别人的库")
    _file(other, "别人的.pdf")
    _file(library, "自己的.pdf")

    outsider = get_user_model().objects.create_user(username="facets-outsider", password="x")
    api.force_authenticate(user=outsider)
    body = api.get("/api/v1/knowledge/files/facets/").json()
    assert body["total"] == 0


def test_nature_and_amount_filters_are_server_side(api, library, member):
    _file(library, "小额合同.pdf", classification="合同", amount=50.0)
    _file(library, "大额合同.pdf", classification="合同", amount=5000.0)
    _file(library, "方案.pdf", classification="方案", amount=5000.0)

    api.force_authenticate(user=member)
    body = api.get("/api/v1/knowledge/files/?nature=合同&amount_min=1000").json()
    names = [row["original_name"] for row in body["results"]]
    assert names == ["大额合同.pdf"], names

    # 非数字金额要报错，而不是静默忽略条件
    bad = api.get("/api/v1/knowledge/files/?amount_min=abc")
    assert bad.status_code == 400


def test_mode_filter_distinguishes_filename_and_content(api, library, member):
    _file(library, "文件名含质保.pdf", text="完全无关的正文")
    _file(library, "无关名字.pdf", text="这里提到质保期十二个月")

    api.force_authenticate(user=member)
    filename_only = api.get("/api/v1/knowledge/files/?q=质保&mode=filename").json()
    assert [r["original_name"] for r in filename_only["results"]] == ["文件名含质保.pdf"]

    content_only = api.get("/api/v1/knowledge/files/?q=质保&mode=content").json()
    assert [r["original_name"] for r in content_only["results"]] == ["无关名字.pdf"]

    hybrid = api.get("/api/v1/knowledge/files/?q=质保&mode=hybrid").json()
    assert len(hybrid["results"]) == 2


def test_conversation_list_is_per_user(api, member):
    """会话列表只返回自己的会话，并带消息条数。"""
    mine = Conversation.objects.create(user=member, title="我的会话")
    Message.objects.create(conversation=mine, role=Message.Role.USER, content="你好")
    other_user = get_user_model().objects.create_user(username="conv-other", password="x")
    Conversation.objects.create(user=other_user, title="别人的会话")

    api.force_authenticate(user=member)
    response = api.get("/api/v1/planner/conversations/")
    assert response.status_code == 200, response.content[:200]
    rows = response.json()["results"]
    assert [row["title"] for row in rows] == ["我的会话"]
    assert rows[0]["count"] == 1


def test_conversation_list_requires_login(api):
    response = api.get("/api/v1/planner/conversations/")
    assert response.status_code in {401, 403}
