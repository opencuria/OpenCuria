"""Harness persistence for interrupted turns after a backend restart."""

from __future__ import annotations

import uuid
from datetime import datetime

from .models import HarnessMessage, HarnessPart, HarnessSession, QuestionRequest
from .permissions.models import PermissionRequest


class RunRecoveryRepository:
    """Read turn ownership and persist scoped recovery without live execution."""

    @staticmethod
    def first_assistant(session_id: uuid.UUID) -> HarnessMessage | None:
        """Find the first admitted assistant when its ledger link was not saved."""
        return (
            HarnessMessage.objects.filter(session_id=session_id, role="assistant")
            .order_by("position", "created_at")
            .first()
        )

    @staticmethod
    def interrupt_message(message_id: uuid.UUID, *, at: datetime, error: str) -> None:
        """Persist interruption of the identified root assistant."""
        HarnessMessage.objects.filter(id=message_id).update(
            finish="aborted", error=error, completed_at=at
        )

    @staticmethod
    def linked_descendant_ids(message_id: uuid.UUID) -> set[uuid.UUID]:
        """Read existing session trees referenced by this turn's subtask parts."""
        child_ids = set(
            HarnessPart.objects.filter(
                message_id=message_id, type="subtask"
            ).values_list("meta__child_session_id", flat=True)
        )
        child_ids.discard(None)
        child_ids.discard("")
        session_ids: set[uuid.UUID] = set()
        frontier = {uuid.UUID(str(child_id)) for child_id in child_ids}
        while frontier:
            descendants = set(
                HarnessSession.objects.filter(id__in=frontier).values_list(
                    "id", flat=True
                )
            )
            descendants -= session_ids
            if not descendants:
                break
            session_ids.update(descendants)
            frontier = (
                set(
                    HarnessSession.objects.filter(
                        parent_id__in=descendants
                    ).values_list("id", flat=True)
                )
                - session_ids
            )
        return session_ids

    @staticmethod
    def interrupt_descendants(
        session_ids: set[uuid.UUID], *, at: datetime, error: str
    ) -> None:
        """Abort incomplete descendant assistants and idle their sessions."""
        HarnessMessage.objects.filter(
            session_id__in=session_ids, role="assistant", completed_at__isnull=True
        ).update(finish="aborted", error=error, completed_at=at)
        HarnessSession.objects.filter(id__in=session_ids).update(status="idle")

    @staticmethod
    def has_other_incomplete_assistant(
        session_id: uuid.UUID, message_id: uuid.UUID | None
    ) -> bool:
        """Return whether idling this root would disrupt another turn."""
        rows = HarnessMessage.objects.filter(
            session_id=session_id, role="assistant", completed_at__isnull=True
        )
        if message_id is not None:
            rows = rows.exclude(id=message_id)
        return rows.exists()

    @staticmethod
    def idle_busy_session(session_id: uuid.UUID) -> None:
        """Clear the busy state without changing other root session states."""
        HarnessSession.objects.filter(id=session_id, status="busy").update(
            status="idle"
        )

    @staticmethod
    def reject_pending_inputs(
        session_ids: set[uuid.UUID],
        *,
        at: datetime,
        message_id: uuid.UUID | None = None,
    ) -> None:
        """Reject pending gates in the supplied sessions, optionally for one turn."""
        filters: dict[str, object] = {
            "session_id__in": session_ids,
            "status": "pending",
        }
        if message_id is not None:
            filters["message_id"] = message_id
        PermissionRequest.objects.filter(**filters).update(
            status="rejected", resolved_at=at
        )
        QuestionRequest.objects.filter(**filters).update(
            status="rejected", resolved_at=at
        )
