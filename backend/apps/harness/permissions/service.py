"""Repository and service layer for permission requests."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import structlog
from django.utils import timezone

from . import models as perm_models
from .evaluator import ALLOW, ASK, DENY, Decision, PermissionEvaluator

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ResolveResult:
    """Outcome of resolving a permission request."""

    request_id: uuid.UUID
    decision: Decision
    remember: str


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
            query = query.filter(workspace_id__in=[workspace_id, None])
        return list(query.values_list("pattern", flat=True))


class PermissionService:
    """Permission gate: evaluator + request lifecycle + allowlist.

    No ORM outside the repositories. Tools/services call
    :meth:`check_or_request` before executing a gated tool.
    """

    def __init__(
        self,
        evaluator: PermissionEvaluator | None = None,
        requests: type[PermissionRequestRepository] | None = None,
        allowlist: type[AllowlistRepository] | None = None,
    ) -> None:
        self.evaluator = evaluator or PermissionEvaluator()
        self.requests = requests or PermissionRequestRepository
        self.allowlist = allowlist or AllowlistRepository

    def check(
        self,
        tool: str,
        action: str = "",
        *,
        mode: str = "",
        organization_id: uuid.UUID | None = None,
        workspace_id: uuid.UUID | None = None,
    ) -> Decision:
        """Evaluate without side effects (allowlist counts as allow)."""
        if organization_id is not None and self._allowlisted(
            organization_id, tool, action, workspace_id
        ):
            return ALLOW
        return self.evaluator.evaluate(tool, action, mode=mode)

    def check_or_request(
        self,
        *,
        organization_id: uuid.UUID,
        session_id: uuid.UUID,
        tool: str,
        pattern: str,
        title: str = "",
        mode: str = "",
        workspace_id: uuid.UUID | None = None,
    ) -> tuple[Decision, perm_models.PermissionRequest | None]:
        """Evaluate; on ``ask`` persist a pending request.

        Returns ``(decision, request)`` where request is None unless the
        decision is ``ask`` (a pending request was created).
        """
        if self._allowlisted(organization_id, tool, pattern, workspace_id):
            log.debug("permission_allowlisted", tool=tool, pattern=pattern)
            return ALLOW, None
        decision = self.evaluator.evaluate(tool, pattern, mode=mode)
        if decision != ASK:
            return decision, None
        request = self.requests.create(
            organization_id=organization_id,
            session_id=session_id,
            workspace_id=workspace_id,
            tool=tool,
            pattern=pattern,
            title=title,
        )
        log.info(
            "permission_requested",
            request_id=str(request.id),
            tool=tool,
            pattern=pattern,
        )
        return ASK, request

    def resolve(self, request_id: uuid.UUID, response: str) -> ResolveResult:
        """Resolve a pending request: once | always | reject.

        ``always`` persists the request's (tool, pattern) pair in the
        allowlist so future checks return ``allow``.
        """
        normalized = response.strip().lower()
        if normalized not in ("once", "always", "reject"):
            raise ValueError(
                f"Invalid permission response '{response}'; expected once|always|reject"
            )
        request = self.requests.get_pending(request_id)
        if request is None:
            raise LookupError(f"Pending permission request '{request_id}' not found")
        if normalized == "reject":
            self.requests.mark_resolved(request, approved=False, remember="once")
            log.info("permission_rejected", request_id=str(request_id))
            return ResolveResult(request_id, DENY, "once")
        remember = (
            perm_models.PermissionRemember.ALWAYS
            if normalized == "always"
            else perm_models.PermissionRemember.ONCE
        )
        self.requests.mark_resolved(request, approved=True, remember=remember)
        if normalized == "always":
            self.allowlist.add(
                organization_id=request.organization_id,
                workspace_id=request.workspace_id,
                tool=request.tool,
                pattern=request.pattern,
            )
        log.info(
            "permission_approved",
            request_id=str(request_id),
            remember=remember,
        )
        return ResolveResult(request_id, ALLOW, remember)

    def _allowlisted(
        self,
        organization_id: uuid.UUID,
        tool: str,
        action: str,
        workspace_id: uuid.UUID | None,
    ) -> bool:
        """Return True when an allowlist entry covers (tool, action)."""
        import fnmatch

        patterns = self.allowlist.patterns_for(
            organization_id=organization_id,
            tool=tool,
            workspace_id=workspace_id,
        )
        global_patterns = self.allowlist.patterns_for(
            organization_id=organization_id,
            tool="*",
            workspace_id=workspace_id,
        )
        for pattern in (*patterns, *global_patterns):
            if fnmatch.fnmatchcase(action, pattern) or pattern in (
                action,
                "*",
            ):
                return True
        return False
