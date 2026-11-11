"""Request-local outbound deadline, shared by model clients without global mutation."""
from contextvars import ContextVar
from functools import wraps
import time
from django.conf import settings

deadline = ContextVar("knowledge_request_deadline", default=None)


def remaining():
    value = deadline.get()
    return None if value is None else max(0.0, value - time.monotonic())


def research_deadline(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        if not getattr(settings, "KB_RAG_ENABLED", True):
            return func(*args, **kwargs)
        previous = deadline.get()
        limit = time.monotonic() + max(10, float(getattr(settings, "KB_RAG_MAX_SECONDS", 120)))
        token = deadline.set(min(previous, limit) if previous is not None else limit)
        try:
            return func(*args, **kwargs)
        finally:
            deadline.reset(token)
    return wrapped
