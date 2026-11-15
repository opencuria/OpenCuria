"""Shared run fixtures and queries for inspecting persisted harness state."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from apps.harness.models import HarnessPart

if TYPE_CHECKING:
    from apps.harness.harness_service import HarnessService
    from apps.harness.models import HarnessMessage, HarnessSession


def attach_run_context(
    service: HarnessService,
    session: HarnessSession,
    assistant: HarnessMessage,
    **extra: Any,
) -> None:
    """Attach timeline fixtures through the same transition as admitted runs."""
    service.run_registry.attach_context(
        str(session.id),
        {
            "session_id": str(session.id),
            "message_id": str(assistant.id),
            "tool_parts": {},
            "step_parts": {},
            "subtask_parts": {},
            **extra,
        },
    )


def parts_for_session(session_id: uuid.UUID) -> list[HarnessPart]:
    """Return full session parts in their persisted timeline order for assertions."""
    return list(
        HarnessPart.objects.filter(message__session_id=session_id).order_by(
            "message__position",
            "message__created_at",
            "message__id",
            "position",
            "created_at",
            "id",
        )
    )
