"""Test-only queries for inspecting persisted harness state."""

from __future__ import annotations

import uuid

from apps.harness.models import HarnessPart


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
