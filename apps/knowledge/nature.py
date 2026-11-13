"""「资料性质」取值口径的唯一真源（词表 + 校验）。

为什么单独一个模块：上传、列表筛选、搜索条件、受控写入四处都要校验同一个
五项词表（申请 / 合同 / 报表 / 普通文档 / 涉密文档）。词表散在多个模块里
迟早会漂移，而漂移的表现是"某一处筛得出、另一处筛不出"这种自相矛盾的界面。

放在视图/序列化器之外还有一个实际原因：``serializers`` 导入 ``views``
会形成循环导入（``views`` 本来就导入 ``serializers``）。
"""

from __future__ import annotations

from .models import FileNature

#: 五项词表（唯一真源来自 :class:`apps.knowledge.models.FileNature`）
NATURE_VOCABULARY: tuple[str, ...] = tuple(choice.value for choice in FileNature)

#: 「待识别」筛选哨兵：与前端 ``KB.NATURE_UNIDENTIFIED`` 一致。
#: **只在只读筛选里**有意义（列出 ``classification == ""`` 的资料）——
#: 它不是一个合法的性质取值，写入路径一律拒绝（见 ``validate_nature_value``）。
UNIDENTIFIED_SENTINEL = "__unidentified__"


class InvalidNatureError(ValueError):
    """性质取值不在五项词表内。"""


def is_valid_nature(value: str) -> bool:
    """空字符串（= 待识别/不限）与五项词表内的值都算合法。"""
    text = str(value or "").strip()
    return not text or text in NATURE_VOCABULARY


def is_unidentified_filter(value: str | None) -> bool:
    """是否"按待识别筛选"（只读语义，不写任何字段）。"""
    return str(value or "").strip() == UNIDENTIFIED_SENTINEL


def nature_error_message(value: str) -> str:
    return (
        "性质只允许这些取值：" + "、".join(NATURE_VOCABULARY) + f"（收到 {value!r}）"
    )


__all__ = [
    "NATURE_VOCABULARY",
    "UNIDENTIFIED_SENTINEL",
    "InvalidNatureError",
    "is_unidentified_filter",
    "is_valid_nature",
    "nature_error_message",
]
