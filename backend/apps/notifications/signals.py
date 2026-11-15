"""Receive harness events; provider network I/O is always deferred to the outbox."""

from __future__ import annotations

import uuid
from typing import Any

from django.dispatch import receiver

from apps.harness.run_events import assistant_run_finished, session_activity_changed

from .services import NotificationService


@receiver(assistant_run_finished)
def assistant_finished(sender: object, message_id: uuid.UUID, **kwargs: Any) -> None:
    """Persist idempotent terminal notifications across all harness engines."""
    NotificationService().record_finished(message_id)


@receiver(session_activity_changed)
def session_activity(sender: object, event: str, data: dict, **kwargs: Any) -> None:
    """Persist status and permission/question changes, including subagent gates."""
    NotificationService().record_activity(event, data)
