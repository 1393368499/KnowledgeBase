"""知识库后台。

已入库资料与分段在 admin 里同样只读——这层限制和模型层守卫互相独立。

**索引登记与库指针也必须只读**（审查要求，2026-10-17）
------------------------------------------------------

``VectorCollection`` 是"这一代索引用哪套模型、多少维、端点在哪"的**唯一权威登记**，
``KnowledgeLibrary`` 上的三个字段（``vector_collection`` / ``active_generation`` /
``next_generation``）是"当前生效哪一代"的**指针**。它们原本在 admin 里可以随便改：

* 改登记行的 dimension/model → ``validate_activation`` 会拿被改过的值去和真实
  collection 比，把"维度不符"判成通过（或者反过来），**绕过代次校验**；
* 手改库指针 → 谁都没经过 coverage 校验就把线上索引指向一个空/半成品代次，
  **绕过写闸门与激活校验**；改 ``next_generation`` 还会让编号被重用；
* 删登记行 → 那一代变成"未登记的集合"，检索与回退全部失效。

所以这里把两者都设成**只读**（仍可在后台**查看**）。要换代/回退必须走受控入口：
``manage.py kbrebuild`` 或 planner 的受控意图（提案 + 确认 + 审计），
它们带冻结、coverage 校验与指针发布。

``KnowledgeLibrary`` 的普通目录字段（名称/级别/部门/客户/启用状态等）**保持可编辑**：
那是业务元数据，改它不影响索引正确性。
"""

from __future__ import annotations

from django.contrib import admin

from .models import DocumentChunk, IngestedFile, KnowledgeLibrary, LibraryMember, VectorCollection

#: “当前生效哪一代索引”的三个指针/编号字段：**后台一律只读**。
#:
#: 为什么连 ``next_generation`` 也要锁：它是"编号只增不减"的计数器，
#: 被改小会让后续 ``prepare_generation`` **重用一个已经建过、装着旧数据的编号**，
#: 于是新向量写进历史索引里。
LIBRARY_INDEX_POINTER_FIELDS = (
    "vector_collection",
    "active_generation",
    "next_generation",
)


@admin.register(KnowledgeLibrary)
class KnowledgeLibraryAdmin(admin.ModelAdmin):
    list_display = ("id", "code", "name", "level", "department", "customer", "is_active")
    list_filter = ("level", "is_active")
    search_fields = ("code", "name", "customer", "department")
    # 只读的是**向量指针/编号**；名称、级别、部门、客户、启用状态等普通元数据
    # 仍然可以在后台维护（改它们不会让索引指向错误的东西）。
    readonly_fields = LIBRARY_INDEX_POINTER_FIELDS

    def get_readonly_fields(self, request, obj=None):
        # 显式返回元组（而不是依赖类属性被 Django 合并）：这样"只读集合"在
        # 继承/混入时也不会被悄悄扩展成"全部可写"。
        return tuple(LIBRARY_INDEX_POINTER_FIELDS)


@admin.register(LibraryMember)
class LibraryMemberAdmin(admin.ModelAdmin):
    list_display = ("id", "library", "user", "role", "department", "created_at")
    list_filter = ("role",)
    search_fields = ("library__code", "user__username")


class ImmutableAdminMixin:
    """追加型数据：后台不允许改、不允许删。"""

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


class ReadOnlyRegistryAdmin(ImmutableAdminMixin):
    """**索引登记**：可查看，但不可新增/修改/删除。

    与 :class:`ImmutableAdminMixin` 的区别只有一条：``has_add_permission`` 也关掉。
    登记行只能由 ``prepare_generation`` 创建（它会分配不可复用的编号并写维度/模型/
    端点快照），手工新增一行等于**伪造一代索引的身份**。
    """

    def has_add_permission(self, request) -> bool:
        return False


@admin.register(IngestedFile)
class IngestedFileAdmin(ImmutableAdminMixin, admin.ModelAdmin):
    list_display = (
        "id",
        "original_name",
        "library",
        "parse_status",
        "chunk_count",
        "customer",
        "created_at",
    )
    list_filter = ("parse_status", "library", "customer")
    search_fields = ("original_name", "content_sha256")


@admin.register(DocumentChunk)
class DocumentChunkAdmin(ImmutableAdminMixin, admin.ModelAdmin):
    list_display = ("id", "file", "ordinal", "page", "token_estimate", "permission_code")
    list_filter = ("permission_code",)
    search_fields = ("text",)


@admin.register(VectorCollection)
class VectorCollectionAdmin(ReadOnlyRegistryAdmin, admin.ModelAdmin):
    """向量索引登记：**只读**（可查看，不可新增/编辑/删除）。

    见模块 docstring：这里能改的每一个字段（dimension / embedding_model /
    model_version / base_url / profile / build_status / is_active）都直接决定
    "这一代索引是什么、能不能被切上线"，手工改动会绕过 ``validate_activation``
    与写闸门。要建新代次只能走 ``manage.py kbrebuild`` 或受控重建意图。
    """

    list_display = (
        "id",
        "name",
        "library",
        "dimension",
        "embedding_model",
        "model_version",
        "profile",
        "build_status",
        "is_active",
    )
    list_filter = ("storage_mode", "embedding_model", "build_status", "is_active")
    search_fields = ("name", "embedding_model", "library__code")

