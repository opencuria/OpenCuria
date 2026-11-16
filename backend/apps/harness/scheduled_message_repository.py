"""ORM access for scheduled conversation messages.

Status changes are compare-and-set updates, so a message is delivered at most
once even when the scheduler and a "send now" request race for it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from django.utils import timezone

from .scheduled_message_models import HarnessScheduledMessage

Status = HarnessScheduledMessage.Status

#: States the owner may still edit, reschedule, withdraw or send now.
OPEN_STATUSES = (Status.PENDING, Status.FAILED)


class ScheduledMessageRepository:
    """Persist scheduled messages; no business rules."""

    @staticmethod
    def create(**fields: Any) -> HarnessScheduledMessage:
        """Insert one pending message."""
        return HarnessScheduledMessage.objects.create(**fields)

    @staticmethod
    def get(message_id: uuid.UUID) -> HarnessScheduledMessage | None:
        """Return a message by id, or ``None``."""
        return HarnessScheduledMessage.objects.filter(pk=message_id).first()

    @staticmethod
    def get_for_session(
        session_id: uuid.UUID, message_id: uuid.UUID
    ) -> HarnessScheduledMessage | None:
        """Return a message only when it belongs to *session_id*."""
        return HarnessScheduledMessage.objects.filter(
            pk=message_id, session_id=session_id
        ).first()

    @staticmethod
    def list_for_session(session_id: uuid.UUID) -> list[HarnessScheduledMessage]:
        """Return a session's messages, earliest first."""
        return list(HarnessScheduledMessage.objects.filter(session_id=session_id))

    @staticmethod
    def count_for_session(session_id: uuid.UUID) -> int:
        """Count every message still waiting for delivery or a decision."""
        return HarnessScheduledMessage.objects.filter(session_id=session_id).count()

    @staticmethod
    def reschedule(
        session_id: uuid.UUID, message_id: uuid.UUID, send_at: datetime
    ) -> HarnessScheduledMessage | None:
        """Move an open message to *send_at*; ``None`` while it is being sent."""
        updated = HarnessScheduledMessage.objects.filter(
            pk=message_id, session_id=session_id, status__in=OPEN_STATUSES
        ).update(
            send_at=send_at,
            status=Status.PENDING,
            error="",
            claimed_at=None,
            updated_at=timezone.now(),
        )
        return ScheduledMessageRepository.get(message_id) if updated else None

    @staticmethod
    def withdraw(
        session_id: uuid.UUID, message_id: uuid.UUID
    ) -> HarnessScheduledMessage | None:
        """Delete an open message and return it; ``None`` while it is being sent."""
        row = HarnessScheduledMessage.objects.filter(
            pk=message_id, session_id=session_id, status__in=OPEN_STATUSES
        ).first()
        if row is None:
            return None
        deleted, _ = HarnessScheduledMessage.objects.filter(
            pk=row.pk, status__in=OPEN_STATUSES
        ).delete()
        return row if deleted else None

    @staticmethod
    def claim(
        session_id: uuid.UUID, message_id: uuid.UUID, now: datetime
    ) -> HarnessScheduledMessage | None:
        """Fence an open message for immediate delivery."""
        updated = HarnessScheduledMessage.objects.filter(
            pk=message_id, session_id=session_id, status__in=OPEN_STATUSES
        ).update(status=Status.SENDING, error="", claimed_at=now, updated_at=now)
        return ScheduledMessageRepository.get(message_id) if updated else None

    @staticmethod
    def claim_due(now: datetime, limit: int) -> list[HarnessScheduledMessage]:
        """Fence up to *limit* pending messages whose time has come."""
        due = list(
            HarnessScheduledMessage.objects.filter(
                status=Status.PENDING, send_at__lte=now
            )
            .order_by("send_at", "created_at")
            .values_list("pk", flat=True)[:limit]
        )
        claimed = [
            pk
            for pk in due
            if HarnessScheduledMessage.objects.filter(
                pk=pk, status=Status.PENDING
            ).update(status=Status.SENDING, claimed_at=now, updated_at=now)
        ]
        return list(
            HarnessScheduledMessage.objects.filter(pk__in=claimed)
            .select_related("session")
            .order_by("send_at", "created_at")
        )

    @staticmethod
    def fail(message_id: uuid.UUID, error: str) -> bool:
        """Record why a fenced delivery did not happen."""
        return bool(
            HarnessScheduledMessage.objects.filter(
                pk=message_id, status=Status.SENDING
            ).update(
                status=Status.FAILED,
                error=error,
                claimed_at=None,
                updated_at=timezone.now(),
            )
        )

    @staticmethod
    def delete(message_id: uuid.UUID) -> bool:
        """Remove a delivered message."""
        deleted, _ = HarnessScheduledMessage.objects.filter(pk=message_id).delete()
        return deleted > 0

    @staticmethod
    def fail_stale(before: datetime, error: str) -> list[uuid.UUID]:
        """Fail deliveries fenced before *before*; return their sessions."""
        stale = HarnessScheduledMessage.objects.filter(
            status=Status.SENDING, claimed_at__lt=before
        )
        session_ids = list(stale.values_list("session_id", flat=True).distinct())
        if session_ids:
            stale.update(
                status=Status.FAILED,
                error=error,
                claimed_at=None,
                updated_at=timezone.now(),
            )
        return session_ids
