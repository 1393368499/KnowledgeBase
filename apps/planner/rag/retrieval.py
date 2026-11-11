"""Retriever adapters return one evidence contract, independently of orchestration."""
from dataclasses import dataclass
from django.conf import settings
from apps.knowledge.models import DocumentChunk
from .contracts import Evidence
from .reader import scoped_files, readable_file


@dataclass(frozen=True)
class RetrievalPolicy:
    name: str
    rerank: bool
    limit: int = 10


def policy(name="default"):
    policies = getattr(settings, "KB_RAG_RETRIEVAL_POLICIES", {})
    selected = policies.get(name, policies.get("default", {}))
    return RetrievalPolicy(name, bool(selected.get("rerank", False)),
                           max(1, min(int(selected.get("limit", 10)), 30)))


def attach_evidence(user, hits, scope=None):
    """Resolve vector results against authorized SQL source; reject stale text."""
    ids = {h.get("file_id") for h in hits if isinstance(h.get("file_id"), int)}
    files = scoped_files(user, scope).filter(pk__in=ids)
    chunks = DocumentChunk.objects.filter(file__in=files).select_related("file__library")
    # Bound by returned candidates, not the size of a long source document.
    wanted = {h.get("chunk_ordinal", h.get("ordinal")) for h in hits}
    known = list(chunks.filter(ordinal__in=[x for x in wanted if isinstance(x, int)]))
    by_key = {(c.file_id, c.ordinal): c for c in known}
    result = []
    for hit in hits:
        ordinal = hit.get("chunk_ordinal", hit.get("ordinal"))
        chunk = by_key.get((hit.get("file_id"), ordinal))
        if chunk is None:
            continue
        # A stale index must not provide a false source link to a newer paragraph.
        if str(hit.get("text") or "").strip() != chunk.text.strip():
            continue
        result.append(Evidence.from_chunk(chunk, source="retrieval").to_dict())
    return result


def related_files(user, *, file_id, relation_query, scope=None, search):
    """Search hypotheses stay hypotheses; association is not inferred from filenames."""
    record = readable_file(user, file_id, scope)
    from apps.planner.services import _readable_library_ids
    allowed = _readable_library_ids(user)
    outcome = search(query=relation_query, libraries=[record.library],
                     permission_codes=None if allowed is None else [f"library:{x}" for x in allowed],
                     limit=policy("deep").limit, use_rerank=policy("deep").rerank)
    items = [h for h in outcome.get("results", []) if h.get("file_id") != record.pk]
    return {"count": len(items), "items": items,
            "evidence": attach_evidence(user, items, scope), "report": outcome.get("report", {}),
            "relation_status": "candidates_require_original_text_verification",
            "relation_query": relation_query, "source_file_id": record.pk}


class HybridRetriever:
    evidence = staticmethod(attach_evidence)
    related = staticmethod(related_files)

    def search(self, *, backend, strategy="default", **kwargs):
        selected = policy(strategy)
        kwargs.setdefault("use_rerank", selected.rerank)
        kwargs.setdefault("limit", selected.limit)
        if kwargs["use_rerank"]:
            from .modules import module
            kwargs.setdefault("rerank_client", module("reranker"))
        return backend(**kwargs)
