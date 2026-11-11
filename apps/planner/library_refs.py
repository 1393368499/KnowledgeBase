"""Resolve human read-only library references inside the caller's visible scope."""
import re
import unicodedata

from apps.knowledge.models import KnowledgeLibrary
from apps.knowledge.permissions import readable_library_ids


class LibraryReferenceError(ValueError):
    def __init__(self, message, *, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def visible_libraries(user, scope=None):
    rows = KnowledgeLibrary.objects.filter(is_active=True).order_by("id")
    allowed = readable_library_ids(user)
    if allowed is not None:
        rows = rows.filter(pk__in=allowed)
    if scope:
        rows = rows.filter(code__in=scope)
    return rows


def _core(text):
    text = unicodedata.normalize("NFKC", str(text)).casefold()
    text = re.sub(r"[\s\W_]+", "", text)
    # Only generic trailing container words are interchangeable. Business
    # qualifiers (采购/销售/劳动 etc.) must survive; no edit-distance guessing.
    return re.sub(r"(?:(?:知识库|资料库|档案库|文档库|文件库|信息库|数据库|资料|档案|文档|文件|库))+$", "", text)


def resolve_read_library(user, ref, scope=None):
    text = unicodedata.normalize("NFKC", str(ref)).strip()
    # Stable IDs and exact codes retain their identity, including permission
    # and scope failures checked by the existing execution layer.
    exact = (KnowledgeLibrary.objects.filter(pk=int(text)).first() if text.isdigit()
             else KnowledgeLibrary.objects.filter(code=text).first())
    if exact is not None:
        return exact.code
    exact_names = list(visible_libraries(user).filter(name=text))
    if len(exact_names) == 1:
        return exact_names[0].code
    if not exact_names and KnowledgeLibrary.objects.filter(name=text).exists():
        raise LibraryReferenceError(f"信息库不存在或不可用：{text}")
    visible = list(visible_libraries(user, scope))
    candidates = [row for row in visible if row.name == text]
    if not candidates:
        key = _core(text)
        if len(key) >= 2:
            candidates = [row for row in visible if _core(row.name) == key]
            if not candidates:
                candidates = [row for row in visible if key in _core(row.name)]
    if len(candidates) == 1:
        return candidates[0].code
    if candidates:
        choices = "、".join(f"「{r.name}」({r.code})" for r in candidates[:6])
        raise LibraryReferenceError(f"信息库名称不明确：{text}。请指定其中一个：{choices}")
    choices = "、".join(f"「{r.name}」({r.code})" for r in visible[:6])
    raise LibraryReferenceError(f"信息库不存在或在本次范围内不可用：{text}。"
                                + (f"可用信息库：{choices}。请使用实际 code 重试，不要扩大范围。" if choices else "没有可用信息库。"),
                                retryable=True)
