"""不可篡改底座：异常类型与登记表。

设计要点（对应需求 7）：
- "已入库信息不可修改"是**结构约束**，由登记表 + ORM 守卫 + DRF 权限 + HTTP 中间件共同强制，
  不依赖任何提示词或模型自觉。
- 登记表集中在 ``governance/registry.py``，任何模块都可以把它读出来做审计。
"""

from __future__ import annotations


class ImmutableRecordError(RuntimeError):
    """试图修改或删除受保护记录。"""

    def __init__(self, model_label: str, operation: str, detail: str = "") -> None:
        self.model_label = model_label
        self.operation = operation
        self.detail = detail
        message = f"记录 {model_label} 不允许 {operation}：已入库信息不可篡改"
        if detail:
            message = f"{message}（{detail}）"
        super().__init__(message)


class GuardedOperationError(RuntimeError):
    """受保护资源上的危险操作被治理层拒绝。"""


# 允许在 append-only 模型上"新写入"的动作白名单（永远只有创建）
APPEND_ONLY_ACTIONS: frozenset[str] = frozenset({"create", "append"})

# 允许通过受控特权通道删除的动作白名单。
# 删除不是"随便删"：只有走完三次确认 + 一次性凭证的服务层才会打开通道。
PARTIAL_DELETE_ACTIONS: frozenset[str] = frozenset(
    {"knowledge.delete_file", "knowledge.delete_library"}
)

# 明确声明"任何人都不能改删"的 HTTP 方法。声明式守卫见 :func:`immutable_endpoint`。
MUTATING_HTTP_METHODS: frozenset[str] = frozenset({"PUT", "PATCH", "DELETE"})


def immutable_endpoint(view):
    """把视图显式标记为"只读"：任何改/删方法在进入业务逻辑前就被拒绝。

    为什么要在中间件之外再加一层：中间件的保护依赖 URL 前缀登记表，是**按路径**
    生效的；装饰器是**按视图**生效的。两层并存的意义是把"这个资源只读"写在视图
    声明处——将来有人新加一条 URL 却忘了登记前缀，装饰器仍然是硬的。

    同时支持函数视图（``def view(request)``）与 DRF/Django 类视图。
    """

    if isinstance(view, type):
        for method in ("put", "patch", "delete"):
            setattr(view, method, _readonly_method(view))
        view.is_immutable_readonly = True  # type: ignore[attr-defined]
        return view

    def wrapper(request, *args, **kwargs):
        if getattr(request, "method", "GET").upper() in MUTATING_HTTP_METHODS:
            return _reject(request)
        return view(request, *args, **kwargs)

    wrapper.is_immutable_readonly = True  # type: ignore[attr-defined]
    wrapper.__name__ = getattr(view, "__name__", "immutable_view")
    wrapper.__doc__ = view.__doc__
    return wrapper


def _readonly_method(view_cls):
    def _method(self, request, *args, **kwargs):
        return _reject(request)

    _method.__name__ = "immutable_readonly"
    _method.__qualname__ = f"{view_cls.__qualname__}.immutable_readonly"
    _method.__doc__ = "只读资源：改/删在视图层被拒绝。"
    return _method


def _reject(request):
    from django.http import JsonResponse

    route = getattr(getattr(request, "resolver_match", None), "route", "")
    label = route or getattr(request, "path", "")
    return JsonResponse(
        {
            "detail": "该资源为只读，不可修改或删除（后端强制约束）",
            "code": "immutable_resource",
            "resource": label,
            "method": getattr(request, "method", "").upper(),
        },
        status=403,
        json_dumps_params={"ensure_ascii": False},
    )
