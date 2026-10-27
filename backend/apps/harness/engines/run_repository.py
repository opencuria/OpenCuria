"""Durable ownership repository for external-engine harness attempts."""

from __future__ import annotations

import uuid
from datetime import datetime

from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone

from common.exceptions import ConflictError

from ..models import (
    HarnessMessage,
    HarnessMessageRole,
    HarnessPart,
    HarnessRun,
    HarnessRunStatus,
    HarnessSession,
    HarnessSessionStatus,
)

LIVE_STATUSES = (
    HarnessRunStatus.STARTING,
    HarnessRunStatus.RUNNING,
    HarnessRunStatus.CLOSING,
)
INTERRUPTED_ERROR = "Run interrupted after worker ownership expired."


class HarnessRunRepository:
    """Database access for durable run attempts and owner-token fencing."""

    model = HarnessRun

    @staticmethod
    def create_attempt(
        *,
        session_id: uuid.UUID,
        assistant_message_id: uuid.UUID,
        user_id: object | None = None,
        harness_id: str = "claude",
    ) -> HarnessRun:
        """Create a unique live attempt for the supplied assistant turn."""
        with transaction.atomic():
            # Serialize admission on the session on databases where row locks are
            # not implemented as well as databases that support select_for_update.
            if not HarnessSession.objects.filter(id=session_id).update(
                updated_at=F("updated_at")
            ):
                raise HarnessSession.DoesNotExist
            session = HarnessSession.objects.get(id=session_id)
            engine_id = (harness_id or "").strip()
            if (
                not engine_id
                or engine_id == "native"
                or session.harness_id != engine_id
            ):
                raise ValueError("Run attempt must match a non-native session engine")
            message = HarnessMessage.objects.filter(
                id=assistant_message_id,
                session_id=session_id,
                role=HarnessMessageRole.ASSISTANT,
            ).first()
            if message is None or message.harness_id != engine_id:
                raise ValueError(
                    "assistant_message must belong to the session and engine"
                )
            if HarnessRun.objects.filter(
                session_id=session_id, status__in=LIVE_STATUSES
            ).exists():
                raise ConflictError("Harness session already has an unresolved run")
            try:
                return HarnessRun.objects.create(
                    session=session,
                    assistant_message=message,
                    organization_id=session.organization_id,
                    initiated_by_id=user_id,
                    harness_id=engine_id[:16],
                )
            except IntegrityError as exc:
                # The partial unique constraint is the final admission fence for
                # concurrent requests. Keep its DB details out of the response.
                raise ConflictError(
                    "Harness session already has an unresolved run"
                ) from exc

    @staticmethod
    def get_by_id(run_id: uuid.UUID | str) -> HarnessRun | None:
        """Return one attempt with its session and assistant turn."""
        return (
            HarnessRun.objects.select_related("session", "assistant_message")
            .filter(id=run_id)
            .first()
        )

    @staticmethod
    def list_open_for_sessions(session_ids: list[uuid.UUID]) -> list[HarnessRun]:
        """Return unresolved engine attempts for an abort closure."""
        if not session_ids:
            return []
        return list(
            HarnessRun.objects.filter(
                session_id__in=session_ids,
                status__in=LIVE_STATUSES,
            ).only("id", "session_id", "status", "lease_id")
        )

    @staticmethod
    def get_open_for_session(session_id: uuid.UUID | str) -> HarnessRun | None:
        """Return the newest unresolved attempt for a session."""
        return (
            HarnessRun.objects.select_related("assistant_message")
            .filter(session_id=session_id, status__in=LIVE_STATUSES)
            .order_by("-created_at", "-id")
            .first()
        )

    @staticmethod
    def list_open() -> list[HarnessRun]:
        """Return unresolved attempts for operational reconciliation."""
        return list(
            HarnessRun.objects.filter(status__in=LIVE_STATUSES).order_by("created_at")
        )

    @staticmethod
    def start_if_owner(run_id: uuid.UUID, owner_token: uuid.UUID) -> bool:
        """Move a freshly created attempt to running under its original owner."""
        return bool(
            HarnessRun.objects.filter(
                id=run_id,
                owner_token=owner_token,
                status=HarnessRunStatus.STARTING,
            ).update(status=HarnessRunStatus.RUNNING, heartbeat_at=timezone.now())
        )

    @staticmethod
    def is_current_owner(run_id: uuid.UUID, owner_token: uuid.UUID) -> bool:
        """Check whether callbacks still belong to the writable owner."""
        return HarnessRun.objects.filter(
            id=run_id,
            owner_token=owner_token,
            status__in=(HarnessRunStatus.STARTING, HarnessRunStatus.RUNNING),
        ).exists()

    @staticmethod
    def is_owner_for_finalization(run_id: uuid.UUID, owner_token: uuid.UUID) -> bool:
        """Check finalization authority, including an owner-fenced CLOSING row."""
        return HarnessRun.objects.filter(
            id=run_id,
            owner_token=owner_token,
            status__in=LIVE_STATUSES,
        ).exists()

    @staticmethod
    def touch_if_owner(
        run_id: uuid.UUID, owner_token: uuid.UUID, *, now: datetime | None = None
    ) -> bool:
        """Refresh the live owner heartbeat; closing/terminal rows are fenced."""
        return bool(
            HarnessRun.objects.filter(
                id=run_id,
                owner_token=owner_token,
                status__in=(HarnessRunStatus.STARTING, HarnessRunStatus.RUNNING),
            ).update(heartbeat_at=now or timezone.now())
        )

    @staticmethod
    def set_identity_if_owner(
        run_id: uuid.UUID,
        owner_token: uuid.UUID,
        *,
        lease_id: uuid.UUID | str,
        lease_epoch: str,
    ) -> bool:
        """Persist runner lease identity before an engine process is spawned."""
        try:
            lease_uuid = uuid.UUID(str(lease_id))
        except (TypeError, ValueError, AttributeError):
            raise ValueError("lease_id must be a UUID") from None
        epoch = (lease_epoch or "").strip()
        if not epoch or len(epoch) > 128:
            raise ValueError("lease_epoch must be 1..128 characters")
        return bool(
            HarnessRun.objects.filter(
                id=run_id,
                owner_token=owner_token,
                status__in=(HarnessRunStatus.STARTING, HarnessRunStatus.RUNNING),
            )
            .filter(
                Q(lease_id__isnull=True) | Q(lease_id=lease_uuid, lease_epoch=epoch)
            )
            .update(
                lease_id=lease_uuid,
                lease_epoch=epoch,
                heartbeat_at=timezone.now(),
            )
        )

    @staticmethod
    def begin_close_if_owner(run_id: uuid.UUID, owner_token: uuid.UUID) -> bool:
        """Stop the owner heartbeat and fence further writes before cleanup."""
        return bool(
            HarnessRun.objects.filter(
                id=run_id,
                owner_token=owner_token,
                status__in=(HarnessRunStatus.STARTING, HarnessRunStatus.RUNNING),
            ).update(status=HarnessRunStatus.CLOSING)
        )

    @staticmethod
    def begin_close_if_stale(
        run_id: uuid.UUID,
        *,
        cutoff: datetime,
        owner_token: uuid.UUID | None = None,
    ) -> bool:
        """Fence a stale owner before any external cleanup is attempted."""
        query = HarnessRun.objects.filter(
            id=run_id,
            status__in=(HarnessRunStatus.STARTING, HarnessRunStatus.RUNNING),
            heartbeat_at__lte=cutoff,
        )
        if owner_token is not None:
            query = query.filter(owner_token=owner_token)
        return bool(
            query.update(
                status=HarnessRunStatus.CLOSING,
                owner_token=uuid.uuid4(),
            )
        )

    @staticmethod
    def ensure_closing(run_id: uuid.UUID) -> bool:
        """Check an already fenced closing attempt without rotating tokens."""
        return HarnessRun.objects.filter(
            id=run_id, status=HarnessRunStatus.CLOSING
        ).exists()

    @staticmethod
    def finish_interrupted_after_cleanup(
        run_id: uuid.UUID,
        *,
        owner_token: uuid.UUID,
        now: datetime | None = None,
    ) -> bool:
        """Finalize a stale run after cleanup/no-lease expiry is proven.

        Message, open parts, and session status are repaired in the same
        transaction as the attempt state, preventing a new turn from racing
        ahead of its interruption marker.
        """
        moment = now or timezone.now()
        with transaction.atomic():
            row = (
                HarnessRun.objects.select_for_update()
                .filter(
                    id=run_id,
                    owner_token=owner_token,
                    status=HarnessRunStatus.CLOSING,
                )
                .first()
            )
            if row is None:
                return False
            changed = HarnessRun.objects.filter(
                id=run_id,
                owner_token=owner_token,
                status=HarnessRunStatus.CLOSING,
            ).update(
                status=HarnessRunStatus.INTERRUPTED,
                error=INTERRUPTED_ERROR,
                heartbeat_at=moment,
                completed_at=moment,
            )
            if not changed:
                return False
            HarnessMessage.objects.filter(id=row.assistant_message_id).update(
                error=INTERRUPTED_ERROR,
                finish="aborted",
                completed_at=moment,
            )
            HarnessPart.objects.filter(
                message_id=row.assistant_message_id,
                state__in=("pending", "running"),
            ).update(state="error", updated_at=moment)
            HarnessSession.objects.filter(
                id=row.session_id, status=HarnessSessionStatus.BUSY
            ).update(
                status=HarnessSessionStatus.IDLE,
                last_message_at=moment,
                updated_at=moment,
            )
            return True
