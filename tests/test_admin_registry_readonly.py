"""Django admin 的索引登记/库指针保护（审查要求，2026-10-17）。

真实缺口：``VectorCollectionAdmin`` 之前可以**任意新增/编辑/删除**登记行，
``KnowledgeLibraryAdmin`` 可以直接改 ``vector_collection`` / ``active_generation`` /
``next_generation``。这些都是"这一代索引是什么、当前生效哪一代"的**权威事实**，
手改就绕过了代次校验与写闸门：

* 改 dimension/model → ``validate_activation`` 拿被改过的值去比，维度不符也能过；
* 手改库指针 → 不经 coverage 校验就把线上指向空/半成品代次；
* 改 ``next_generation`` → 编号被复用，新向量写进历史索引；
* 删登记行 → 那一代变成"未登记集合"，检索与回退全部失效。

本模块守住：登记只读（仍可查看）、三个指针字段 readonly、**普通目录字段仍可编辑**。
"""

from __future__ import annotations

import pytest
from django.contrib.admin.sites import site

from apps.knowledge import admin as kb_admin
from apps.knowledge.models import KnowledgeLibrary, VectorCollection

pytestmark = pytest.mark.django_db


@pytest.fixture
def superuser(django_user_model):
    return django_user_model.objects.create_superuser(
        username="admin-ro", email="", password="x" * 12
    )


@pytest.fixture
def requests(rf, superuser):
    request = rf.get("/admin/")
    request.user = superuser
    return request


# ------------------------------------------------- 索引登记：只读，可查看 ----

def test_vectorcollection_admin_is_view_only(requests):
    model_admin = site._registry[VectorCollection]
    assert isinstance(model_admin, kb_admin.VectorCollectionAdmin)
    assert model_admin.has_add_permission(requests) is False, "登记行不允许手工新增"
    assert model_admin.has_change_permission(requests) is False, "登记行不允许手工编辑"
    assert model_admin.has_delete_permission(requests) is False, "登记行不允许手工删除"
    # 仍然**可以查看**（只读不等于藏起来）
    assert model_admin.has_view_permission(requests) is True


def test_vectorcollection_admin_exposes_the_identity_fields_for_review(requests):
    """只读页面必须**能看到**决定身份的那些字段（否则无法人工核对）。"""
    model_admin = site._registry[VectorCollection]
    for field in (
        "dimension",
        "embedding_model",
        "model_version",
        "profile",
        "build_status",
        "is_active",
    ):
        assert field in model_admin.list_display, f"只读页缺少 {field}"


# ------------------------------------------- 库：三个指针只读，元数据可改 ----

def test_library_pointer_fields_are_readonly(requests):
    model_admin = site._registry[KnowledgeLibrary]
    readonly = set(model_admin.get_readonly_fields(requests))
    for field in kb_admin.LIBRARY_INDEX_POINTER_FIELDS:
        assert field in readonly, f"{field} 必须只读（手改会绕过代次校验与写闸门）"
    assert readonly == set(kb_admin.LIBRARY_INDEX_POINTER_FIELDS), (
        "只读集合应当**恰好**是指针/编号字段：不要顺手把普通目录字段也锁掉"
    )


def test_library_metadata_stays_editable():
    """普通目录字段（名称/级别/部门/客户/启用）仍可维护：它们不影响索引正确性。"""
    readonly = set(kb_admin.KnowledgeLibraryAdmin.readonly_fields)
    for field in ("name", "level", "department", "customer", "is_active", "code"):
        assert field not in readonly, f"{field} 是普通元数据，不该被设成只读"


def test_library_admin_does_not_disable_the_whole_model(requests):
    """库本身仍可在后台管理（只有指针字段被锁），别把保护做成"整表只读"。"""
    model_admin = site._registry[KnowledgeLibrary]
    assert model_admin.has_change_permission(requests) is True
    assert model_admin.has_view_permission(requests) is True
