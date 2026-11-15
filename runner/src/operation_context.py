"""Identity of the lifecycle operation that owns the effect being executed."""

from __future__ import annotations

import contextvars

#: Binding of the lifecycle operation whose command runs in this context.
active_operation: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "lifecycle_operation", default=None
)


def current_operation_id() -> str | None:
    """Return the operation owning this lifecycle effect, when one is active."""
    return (active_operation.get() or {}).get("operation_id")
