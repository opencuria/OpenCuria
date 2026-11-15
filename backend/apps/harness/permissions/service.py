"""Service layer for permission requests."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import structlog

from . import models as perm_models
from .evaluator import (
    ALLOW,
    DEFAULT_GLOBAL_RULES,
    DENY,
    Decision,
    PermissionEvaluator,
)
from .repositories import AllowlistRepository, PermissionRequestRepository
from .scopes import is_permission_scope, permission_scope_matches

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ResolveResult:
    """Outcome of resolving a permission request."""

    request_id: uuid.UUID
    decision: Decision
    remember: str


class PermissionService:
    """Shared evaluator, persisted approvals and request resolution.

    Engines evaluate policy before the harness's permission callback creates
    pending requests. This service resolves them and stores reusable grants.
    """

    def __init__(
        self,
        evaluator: PermissionEvaluator | None = None,
        requests: type[PermissionRequestRepository] | None = None,
        allowlist: type[AllowlistRepository] | None = None,
    ) -> None:
        self.evaluator = evaluator or PermissionEvaluator(
            global_rules=dict(DEFAULT_GLOBAL_RULES)
        )
        self.requests = requests or PermissionRequestRepository
        self.allowlist = allowlist or AllowlistRepository

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

    def is_allowlisted(
        self,
        organization_id: uuid.UUID,
        tool: str,
        action: str,
        workspace_id: uuid.UUID | None = None,
    ) -> bool:
        """Return whether a persisted grant covers this scoped tool action."""
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
        if is_permission_scope(action):
            # A fingerprint is an exact action capability. Never apply glob
            # semantics to its public display or let a legacy redacted grant
            # bridge multiple distinct secret-bearing actions.
            return any(
                is_permission_scope(pattern)
                and permission_scope_matches(pattern, action)
                for pattern in (*patterns, *global_patterns)
            )
        for pattern in (*patterns, *global_patterns):
            if is_permission_scope(pattern):
                continue
            if fnmatch.fnmatchcase(action, pattern) or pattern in (action, "*"):
                return True
        return False
