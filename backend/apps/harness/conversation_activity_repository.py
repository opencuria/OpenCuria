"""Persisted conversation and gate reads for the activity service."""

from __future__ import annotations

import uuid
from datetime import datetime

from django.db.models import F, OuterRef, Subquery

from .models import HarnessMessage, HarnessSession, QuestionRequest
from .permissions.models import PermissionRequest


class ConversationActivityRepository:
    """Load activity state without exposing queries to consuming contexts."""

    @staticmethod
    def root_session(session_id: uuid.UUID) -> HarnessSession | None:
        """Load a root whose workspace still belongs to its organization."""
        return (
            HarnessSession.objects.select_related("workspace")
            .filter(
                id=session_id,
                parent__isnull=True,
                workspace__runner__organization_id=F("organization_id"),
            )
            .first()
        )

    @staticmethod
    def latest_assistant(session_id: uuid.UUID) -> HarnessMessage | None:
        """Load the most recent assistant turn, including unfinished turns."""
        return (
            HarnessMessage.objects.filter(session_id=session_id, role="assistant")
            .order_by("-position")
            .first()
        )

    @staticmethod
    def root_message(message_id: uuid.UUID) -> HarnessMessage | None:
        """Load an assistant turn with its root and current workspace scope."""
        return (
            HarnessMessage.objects.select_related("session__workspace")
            .filter(
                id=message_id,
                role="assistant",
                session__parent__isnull=True,
                session__workspace__runner__organization_id=F(
                    "session__organization_id"
                ),
            )
            .first()
        )

    @staticmethod
    def session_parents(
        session_ids: set[uuid.UUID],
    ) -> dict[uuid.UUID, uuid.UUID | None]:
        """Load one ancestry frontier, omitting missing sessions."""
        return dict(
            HarnessSession.objects.filter(id__in=session_ids)
            .order_by()
            .values_list("id", "parent_id")
        )

    @staticmethod
    def busy_roots(org_id: uuid.UUID, user_id: int) -> list[HarnessSession]:
        """Load owned busy roots with their latest assistant completion time."""
        latest = (
            HarnessMessage.objects.filter(session_id=OuterRef("pk"), role="assistant")
            .order_by("-position")
            .values("completed_at")[:1]
        )
        return list(
            HarnessSession.objects.select_related("workspace")
            .filter(
                organization_id=org_id,
                workspace__runner__organization_id=org_id,
                workspace__created_by_id=user_id,
                parent__isnull=True,
                status="busy",
            )
            .annotate(latest_assistant_completed_at=Subquery(latest))
            .order_by("-updated_at")
        )

    @staticmethod
    def pending_session_ids(
        org_id: uuid.UUID,
        workspace_ids: list[uuid.UUID],
        *,
        permissions: bool,
    ) -> list[uuid.UUID]:
        """Load sessions with pending gates, restricted to busy sessions."""
        busy_ids = HarnessSession.objects.filter(
            organization_id=org_id,
            workspace_id__in=workspace_ids,
            status="busy",
        ).values_list("id", flat=True)
        model = PermissionRequest if permissions else QuestionRequest
        return list(
            model.objects.filter(
                organization_id=org_id,
                session_id__in=busy_ids,
                status="pending",
            )
            .values_list("session_id", flat=True)
            .distinct()
        )

    @staticmethod
    def pending_gate(
        request_id: uuid.UUID,
        org_id: uuid.UUID,
        workspace_id: uuid.UUID,
        *,
        permissions: bool,
    ) -> bool:
        """Check a specific gate in its exact organization and workspace."""
        model = PermissionRequest if permissions else QuestionRequest
        return model.objects.filter(
            id=request_id,
            organization_id=org_id,
            workspace_id=workspace_id,
            status="pending",
        ).exists()

    @staticmethod
    def recent_completions(since: datetime) -> list[uuid.UUID]:
        """Load root assistant turns completed since the repair cutoff."""
        return list(
            HarnessMessage.objects.filter(
                role="assistant",
                session__parent__isnull=True,
                completed_at__gte=since,
            ).values_list("id", flat=True)
        )

    @staticmethod
    def recent_pending_requests(
        since: datetime, *, permissions: bool
    ) -> list[tuple[uuid.UUID, uuid.UUID]]:
        """Load fresh unanswered gates belonging to busy sessions."""
        busy = HarnessSession.objects.filter(status="busy").values("id")
        model = PermissionRequest if permissions else QuestionRequest
        return list(
            model.objects.filter(
                status="pending", session_id__in=busy, created_at__gte=since
            ).values_list("id", "session_id")
        )
