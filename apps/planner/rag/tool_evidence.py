"""Audited tool facts (manifest counts/calculations) are citable, distinct from text."""
import json
from hashlib import sha256
from .reader import scoped_files


def digest(result):
    return sha256(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def facts(operation):
    result = operation.result or {}
    if operation.intent == "list_files":
        return {"type": "manifest_page", "total": result.get("count"), "has_more": result.get("has_more"),
                "returned_ids": [r["id"] for r in result.get("items", [])],
                "filters": result.get("filters"), "snapshot": "live_page_at_operation_time"}
    return {k: result.get(k) for k in ("operation", "value", "unit", "operand_count", "coverage", "note")}


def make(operation):
    fingerprint = digest(operation.result)
    key = "T" + sha256(f"{operation.pk}:{fingerprint}".encode()).hexdigest()[:20]
    return {"kind": "operation", "evidence_id": key, "operation_id": operation.pk,
            "file_id": None, "ordinal": None, "page": None, "version": fingerprint,
            "name": "文件清单记录" if operation.intent == "list_files" else "数字计算记录",
            "library": "", "text": json.dumps(facts(operation), ensure_ascii=False),
            "text_hash": fingerprint, "source": operation.intent, "truncated": False,
            "url": f"/api/v1/planner/research-record/{operation.pk}/?digest={fingerprint}"}


def accessible(user, operation, scope=None):
    if operation.user_id != user.pk or operation.intent not in {"list_files", "calculate_evidence"} or operation.status != "executed":
        return False
    result = operation.result or {}
    ids = ({r["id"] for r in result.get("items", [])} if operation.intent == "list_files"
           else {r["file_id"] for r in result.get("evidence", [])})
    allowed = scoped_files(user, scope)
    if len(set(allowed.filter(pk__in=ids).values_list("pk", flat=True))) != len(ids):
        return False
    from apps.planner.library_refs import visible_libraries
    visible = visible_libraries(user, scope)
    if operation.library_scope and not set(operation.library_scope) <= set(visible.values_list("code", flat=True)):
        return False
    requested = operation.params.get("library")
    if requested and not visible.filter(code=requested).exists():
        return False
    if operation.intent == "list_files":
        contributors = result.get("count_library_ids")
        # Older counts cannot prove which libraries contributed.
        if contributors is None or not set(contributors) <= set(visible.values_list("id", flat=True)):
            return False
    return True
