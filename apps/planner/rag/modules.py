"""Small explicit adapter registry. Deployment configuration, never user supplied paths."""
from django.conf import settings
from django.utils.module_loading import import_string

DEFAULTS = {
    "router": "apps.planner.rag.routing.FlowRouter",
    "planner": "apps.planner.rag.routing.ResearchPlanner",
    "reranker": "apps.knowledge.rerank.RerankClient",
    "reader": "apps.planner.rag.reader.SourceReader",
    "retriever": "apps.planner.rag.retrieval.HybridRetriever",
    "calculator": "apps.planner.rag.calculator.EvidenceCalculator",
    "verifier": "apps.planner.rag.verifier.EvidenceVerifier",
    "generator": "apps.planner.llm.LlmClient",
    "runtime": "apps.planner.rag.runtime.ResearchRun",
}


def module(name, *args, **kwargs):
    if name not in DEFAULTS:
        raise ValueError(f"Unknown RAG module: {name}")
    paths = getattr(settings, "KB_RAG_MODULES", {})
    return import_string(paths.get(name, DEFAULTS[name]))(*args, **kwargs)
