"""Shared rules for durable multi-step workspace requests (capture, recreate).

A request owns its workspace between child tasks. A child with an unknown
outcome is never advanced past: it stays fenced until an operator resolves it.
"""

from __future__ import annotations

from .models import LifecycleCommand, Task, Workspace
from .operations import OperationRepository

UNKNOWN_CHILD_DIAGNOSTIC = (
    "Unknown child outcome; exact journal reconciliation required"
)
TERMINAL_PHASES = ["completed", "failed"]


def fence_unknown_child(
    request, ws: Workspace, child: Task, command: LifecycleCommand
) -> tuple[bool, dict | None]:
    """Fence an ambiguous child; return (fenced, notification-if-changed)."""
    if not (
        command.phase == "intervention"
        or ws.current_task_id
        or (child.status == "failed" and "intervention" in child.error.lower())
    ):
        return False, None
    changed = request.diagnostic != UNKNOWN_CHILD_DIAGNOSTIC
    OperationRepository.intervene(child, UNKNOWN_CHILD_DIAGNOSTIC)
    ws.current_task = child
    ws.active_operation = None
    ws.save(update_fields=["current_task", "active_operation"])
    request.diagnostic = UNKNOWN_CHILD_DIAGNOSTIC
    request.save(update_fields=["diagnostic"])
    if not changed:
        return True, None
    return True, {
        "workspace_id": str(ws.id),
        "phase": "intervention",
        "diagnostic": UNKNOWN_CHILD_DIAGNOSTIC,
    }
