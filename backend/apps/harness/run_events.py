"""Run lifecycle events the harness publishes to the apps built on it.

The harness is a core context and never imports its consumers. Apps that
react to run outcomes (for example scheduled-task history) connect a
receiver from their ``AppConfig.ready()``, so dependencies point one way.
"""

from __future__ import annotations

import uuid

import structlog
from django.dispatch import Signal

from .models import HarnessMessage

log = structlog.get_logger(__name__)

assistant_run_finished = Signal()
"""Sent with ``message_id`` once an assistant run has settled.

Receivers are synchronous, may use the ORM, and must be idempotent.
Delivery is best effort: consumers keep their own repair path.
"""

session_activity_changed = Signal()
"""Low-frequency session status and gate events, independent of socket clients."""


async def publish_session_activity(event: str, data: dict) -> None:
    """Publish lifecycle changes without putting consumers on the token hot path."""
    if event not in {
        "harness.session_status",
        "harness.permission_required",
        "harness.question_required",
    }:
        return
    results = await session_activity_changed.asend_robust(
        sender=HarnessMessage, event=event, data=data
    )
    for receiver, result in results:
        if isinstance(result, Exception):
            log.error(
                "session_activity_receiver_failed",
                session_id=data.get("session_id"),
                receiver=getattr(receiver, "__qualname__", repr(receiver)),
            )


async def publish_assistant_run_finished(
    message_id: uuid.UUID, *, session_id: str
) -> None:
    """Notify receivers; a failing receiver never blocks run finalization."""
    results = await assistant_run_finished.asend_robust(
        sender=HarnessMessage, message_id=message_id
    )
    for receiver, result in results:
        if isinstance(result, Exception):
            log.error(
                "assistant_run_finished_receiver_failed",
                session_id=session_id,
                receiver=getattr(receiver, "__qualname__", repr(receiver)),
                exc_info=result,
            )
