"""Permission checked, cursor based source reading; never pretend a page is a corpus."""
from django.conf import settings
from django.db.models import Count
from apps.knowledge.models import DocumentChunk
from .contracts import Evidence


def scoped_files(user, scope=None, library=None):
    # Import lazily: planner adapters call this module, not the reverse at import time.
    from apps.knowledge.views import _readable_files
    from apps.planner.services import _scoped_libraries_or_raise
    from apps.planner.library_refs import resolve_read_library
    rows = _readable_files(user).select_related("library")
    scoped = _scoped_libraries_or_raise(user, scope)
    if scoped is not None:
        rows = rows.filter(library_id__in=[row.pk for row in scoped])
    if library:
        code = resolve_read_library(user, library, scope)
        from apps.planner.library_refs import visible_libraries
        from apps.planner.services import PlannerError
        if scoped is not None and code not in {r.code for r in scoped}:
            raise PlannerError("信息库不在本次检索范围内")
        if not visible_libraries(user).filter(code=code).exists():
            raise PlannerError("信息库不存在或没有查看权限")
        rows = rows.filter(library__code=code)
    return rows


def readable_file(user, file_id, scope=None):
    from apps.planner.services import PlannerError
    row = scoped_files(user, scope).filter(pk=file_id).first()
    if row is None:
        raise PlannerError("资料不存在、无权查看或不在本次检索范围内")
    return row


def read_passages(user, *, file_id, scope=None, start_ordinal=0, limit=5,
                  page=None, char_offset=0, expected_version=None):
    from apps.planner.services import PlannerError
    record = readable_file(user, file_id, scope)
    if expected_version is not None and expected_version != record.content_sha256:
        raise PlannerError("资料版本已变化，请重新检索")
    limit = max(1, min(int(limit), 20))
    rows = DocumentChunk.objects.filter(file=record, ordinal__gte=start_ordinal)
    if page is not None:
        rows = rows.filter(page=page)
    chunks = list(rows.select_related("file__library").order_by("ordinal")[:limit + 1])
    budget = max(1024, int(getattr(settings, "KB_RAG_READ_CHARS", 12000)))
    evidence, next_cursor = [], None
    for index, chunk in enumerate(chunks):
        if index == limit:
            next_cursor = {"start_ordinal": chunk.ordinal, "char_offset": 0}
            break
        offset = char_offset if index == 0 else 0
        if offset > len(chunk.text):
            raise PlannerError("正文读取位置超出段落长度")
        remaining = chunk.text[offset:]
        part = remaining[:budget]
        truncated = offset > 0 or len(part) < len(remaining)
        evidence.append(Evidence.from_chunk(chunk, text=part, truncated=truncated).to_dict()
                        | {"char_offset": offset, "next_char_offset": offset + len(part)})
        budget -= len(part)
        if len(part) < len(remaining):
            next_cursor = {"start_ordinal": chunk.ordinal, "char_offset": offset + len(part)}
            break
        if budget <= 0:
            if index + 1 < len(chunks):
                next_cursor = {"start_ordinal": chunks[index + 1].ordinal, "char_offset": 0}
            break
    return {"file_id": record.pk, "version": record.content_sha256, "items": evidence,
            "count": len(evidence), "has_more": next_cursor is not None,
            "next_cursor": next_cursor, "page": page, "coverage": "requested_passages"}


def enumerate_files(user, *, scope=None, library=None, after_id=0, limit=50,
                    status=None, customer=None, department=None):
    from apps.knowledge.views import apply_file_filters
    rows = scoped_files(user, scope, library)
    if status:
        rows = rows.filter(parse_status=status)
    if customer or department:
        rows = apply_file_filters(rows, customer=customer or "", department=department or "")
    library_counts = list(rows.order_by().values("library_id").annotate(file_count=Count("pk")))
    total = sum(row["file_count"] for row in library_counts)
    limit = max(1, min(int(limit), 100))
    page = list(rows.filter(pk__gt=after_id).order_by("pk")[:limit + 1])
    more = len(page) > limit
    page = page[:limit]
    return {"count": total, "items": [
        {"id": r.pk, "name": r.original_name, "status": r.parse_status,
         "library": r.library.code, "version": r.content_sha256} for r in page],
        "has_more": more, "next_after_id": page[-1].pk if more else None,
        "after_id": after_id, "coverage": "file_manifest", "snapshot": "live",
        "count_library_ids": [row["library_id"] for row in library_counts]}


class SourceReader:
    read = staticmethod(read_passages)
    enumerate = staticmethod(enumerate_files)
    file = staticmethod(readable_file)
