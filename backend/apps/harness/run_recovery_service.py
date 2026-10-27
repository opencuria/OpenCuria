"""Recover persisted harness turns without starting live run orchestration."""

from __future__ import annotations

import uuid
from datetime import datetime

from .models import HarnessMessage
from .run_recovery_repository import RunRecoveryRepository


class RunRecoveryService:
    """Own the harness state affected by an interrupted turn.

    The caller holds its recovery transaction and decides which unfinished
    turns were lost. This service scopes cleanup to those turns, leaving
    completed messages and unrelated follow-ups alone.
    """

    def __init__(self, repository: type[RunRecoveryRepository] | None = None) -> None:
        self.repository = repository or RunRecoveryRepository

    def find_assistant(
        self, session_id: uuid.UUID | None, assistant: HarnessMessage | None
    ) -> HarnessMessage | None:
        """Prefer the linked assistant; repair a missing link from its session."""
        if assistant is None and session_id is not None:
            return self.repository.first_assistant(session_id)
        return assistant

    def interrupt_turn(
        self,
        session_id: uuid.UUID,
        message_id: uuid.UUID | None,
        *,
        at: datetime,
        error: str,
    ) -> None:
        """Interrupt a lost root turn and only the subtask trees it spawned."""
        if message_id is not None:
            self.repository.interrupt_message(message_id, at=at, error=error)
            descendants = self.repository.linked_descendant_ids(message_id)
            if descendants:
                self.repository.interrupt_descendants(descendants, at=at, error=error)
                self.repository.reject_pending_inputs(descendants, at=at)

        if not self.repository.has_other_incomplete_assistant(session_id, message_id):
            self.repository.idle_busy_session(session_id)
        if message_id is not None:
            self.repository.reject_pending_inputs(
                {session_id}, message_id=message_id, at=at
            )
