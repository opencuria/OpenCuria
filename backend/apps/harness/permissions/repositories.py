"""Repository layer for permission requests and persisted approvals."""

from __future__ import annotations

import uuid

from django.db.models import Q
from django.utils import timezone

from . import models as perm_models


class PermissionRequestRepository:
    """Data access for PermissionRequest records."""

    model = perm_models.PermissionRequest

    @staticmethod
    def create(
        *,
        organization_id: uuid.UUID,
        session_id: uuid.UUID,
        tool: str,
        pattern: str,
        workspace_id: uuid.UUID | None = None,
        title: str = "",
        message_id: uuid.UUID | None = None,
        call_id: str = "",
    ) -> perm_models.PermissionRequest:
        """Create a pending permission request."""
        return perm_models.PermissionRequest.objects.create(
            organization_id=organization_id,
            workspace_id=workspace_id,
            session_id=session_id,
            message_id=message_id,
            call_id=call_id or "",
            tool=tool,
            pattern=pattern,
            title=title or "",
        )

    @staticmethod
    def get_pending(
        request_id: uuid.UUID,
    ) -> perm_models.PermissionRequest | None:
        """Fetch a pending request by ID (None when missing/resolved)."""
        return perm_models.PermissionRequest.objects.filter(
            id=request_id,
            status=perm_models.PermissionRequestStatus.PENDING,
        ).first()

    @staticmethod
    def get_by_id(
        request_id: uuid.UUID,
    ) -> perm_models.PermissionRequest | None:
        """Fetch any request by ID."""
        return perm_models.PermissionRequest.objects.filter(id=request_id).first()

    @staticmethod
    def list_pending_for_session(
        session_id: uuid.UUID, *, tool: str | None = None
    ) -> list[perm_models.PermissionRequest]:
        """Return pending requests for *session_id*, optionally filtered by tool."""
        query = perm_models.PermissionRequest.objects.filter(
            session_id=session_id,
            status=perm_models.PermissionRequestStatus.PENDING,
        )
        if tool:
            query = query.filter(tool=tool)
        return list(query.order_by("created_at"))

    @staticmethod
    def list_pending_for_sessions(
        session_ids: list[uuid.UUID], *, tool: str | None = None
    ) -> list[perm_models.PermissionRequest]:
        """Return pending requests for any of *session_ids*."""
        if not session_ids:
            return []
        query = perm_models.PermissionRequest.objects.filter(
            session_id__in=session_ids,
            status=perm_models.PermissionRequestStatus.PENDING,
        )
        if tool:
            query = query.filter(tool=tool)
        return list(query.order_by("created_at"))

    @staticmethod
    def mark_resolved(
        request: perm_models.PermissionRequest,
        *,
        approved: bool,
        remember: str,
    ) -> perm_models.PermissionRequest:
        """Mark *request* approved/rejected with a remember choice."""
        request.status = (
            perm_models.PermissionRequestStatus.APPROVED
            if approved
            else perm_models.PermissionRequestStatus.REJECTED
        )
        request.remember = remember
        request.resolved_at = timezone.now()
        request.save(update_fields=["status", "remember", "resolved_at"])
        return request


class AllowlistRepository:
    """Data access for persisted ``always`` approvals."""

    model = perm_models.PermissionAllowlist

    @staticmethod
    def add(
        *,
        organization_id: uuid.UUID,
        tool: str,
        pattern: str,
        workspace_id: uuid.UUID | None = None,
    ) -> perm_models.PermissionAllowlist:
        """Store an always-approval (idempotent)."""
        record, _ = perm_models.PermissionAllowlist.objects.get_or_create(
            organization_id=organization_id,
            workspace_id=workspace_id,
            tool=tool,
            pattern=pattern,
        )
        return record

    @staticmethod
    def patterns_for(
        *,
        organization_id: uuid.UUID,
        tool: str,
        workspace_id: uuid.UUID | None = None,
    ) -> list[str]:
        """Return allowlisted patterns for a tool (org + workspace scope)."""
        query = perm_models.PermissionAllowlist.objects.filter(
            organization_id=organization_id, tool=tool
        )
        if workspace_id is not None:
            query = query.filter(
                Q(workspace_id=workspace_id) | Q(workspace_id__isnull=True)
            )
        else:
            query = query.filter(workspace_id__isnull=True)
        return list(query.values_list("pattern", flat=True))
