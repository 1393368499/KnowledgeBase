"""治理层。"""

from .immutable import GuardedOperationError, ImmutableRecordError

__all__ = ["ImmutableRecordError", "GuardedOperationError"]
