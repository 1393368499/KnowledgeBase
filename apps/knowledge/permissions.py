"""信息库权限：库级角色 + 等级可见性。

角色来源：
1. 系统管理员（is_staff / is_superuser）→ 全部库的 admin；
2. `LibraryMember.role` → 该库的 reader / writer / admin；
3. 外部信息库（level=external）默认对已登录用户可读——这是"公开资料可查"的口径，
   内部信息库必须显式授权。
"""

from __future__ import annotations

from rest_framework.permissions import BasePermission

from .models import LibraryLevel, MemberRole

ROLE_ORDER = {MemberRole.READER: 1, MemberRole.WRITER: 2, MemberRole.ADMIN: 3}


def role_at_least(role: str | None, required: str) -> bool:
    if role is None:
        return False
    return ROLE_ORDER.get(role, 0) >= ROLE_ORDER.get(required, 99)


def is_system_admin(user) -> bool:
    return bool(
        getattr(user, "is_authenticated", False)
        and (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))
    )


def readable_library_ids(user) -> set[int] | None:
    """返回该用户可读的库 id 集合；``None`` 表示不受限。"""
    if is_system_admin(user):
        return None
    if not getattr(user, "is_authenticated", False):
        return set()
    from .models import KnowledgeLibrary  # 局部导入避免循环

    explicit = set(
        KnowledgeLibrary.objects.filter(members__user=user).values_list("id", flat=True)
    )
    public = set(
        KnowledgeLibrary.objects.filter(
            level=LibraryLevel.EXTERNAL, is_active=True
        ).values_list("id", flat=True)
    )
    return explicit | public


class LibraryAccessPermission(BasePermission):
    """视图级：必须登录。对象级：按库角色判定。"""

    message = "没有访问该信息库的权限"
    required_role = MemberRole.READER

    def has_permission(self, request, view) -> bool:
        # 视图级只要求登录；"能看哪些库"由 queryset 过滤 + 对象级判定负责，
        # 避免出现"看不到任何库就连自己的会话都读不了"的误伤。
        return bool(getattr(request.user, "is_authenticated", False))

    def has_object_permission(self, request, view, obj) -> bool:
        library = getattr(obj, "library", obj)
        user = request.user
        if is_system_admin(user):
            return True
        role = getattr(library, "member_role", lambda _u: None)(user)
        if role is None and getattr(library, "level", None) == LibraryLevel.EXTERNAL:
            role = MemberRole.READER
        return role_at_least(role, self.required_role)


class CanReadLibrary(LibraryAccessPermission):
    required_role = MemberRole.READER


class CanContributeLibrary(LibraryAccessPermission):
    required_role = MemberRole.WRITER
    message = "只有库管理员或可写入成员才能向该信息库添加资料"


class CanManageLibrary(LibraryAccessPermission):
    required_role = MemberRole.ADMIN
    message = "只有库管理员才能修改信息库配置"


def can_contribute(user, library) -> bool:
    """服务层复用：能否向该库写入资料。"""
    if is_system_admin(user):
        return True
    if not getattr(user, "is_authenticated", False):
        return False
    return role_at_least(library.member_role(user), MemberRole.WRITER)
