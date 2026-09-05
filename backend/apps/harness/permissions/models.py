"""Permission request flow: models, repositories, service.

``PermissionRequest`` intentionally avoids a foreign key to the future
``HarnessSession`` model (M6). It stores ``session_id``/``workspace_id``
as plain UUID/char fields plus ``organization_id`` for scoping, so M6
can re-point the session reference to ``HarnessSession`` without touching
the evaluator or the request lifecycle.

``always`` approvals persist the approved pattern in
``PermissionAllowlist`` (org + optional workspace scope). M6 may move
this to org/workspace-level settings; the evaluator-facing lookup
(``AllowlistRepository.patterns_for``) stays stable.
"""

from __future__ import annotations

import uuid

from django.db import models


class PermissionRequestStatus(models.TextChoices):
    """Lifecycle states of a permission request."""

    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"


class PermissionRemember(models.TextChoices):
    """How the resolution should be remembered."""

    ONCE = "once", "Once"
    ALWAYS = "always", "Always"


class PermissionRequest(models.Model):
    """A tool invocation waiting for (or resolved by) user approval.

    M6 will replace ``session_id`` with an FK to ``HarnessSession``.
    Until then the plain UUID keeps this model independent of the
    runners app (no circular imports).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization_id = models.UUIDField(
        help_text="Owning organization (scoping, no FK to avoid cycles).",
    )
    workspace_id = models.UUIDField(
        null=True,
        blank=True,
        help_text="Workspace UUID (plain field until M6).",
    )
    session_id = models.UUIDField(
        help_text="Harness session UUID (FK to HarnessSession in M6).",
    )
    message_id = models.UUIDField(null=True, blank=True)
    call_id = models.CharField(max_length=255, blank=True, default="")
    tool = models.CharField(max_length=64)
    pattern = models.CharField(
        max_length=1024,
        help_text="Action/pattern the user approves (path or command).",
    )
    title = models.CharField(max_length=512, blank=True, default="")
    status = models.CharField(
        max_length=16,
        choices=PermissionRequestStatus.choices,
        default=PermissionRequestStatus.PENDING,
    )
    remember = models.CharField(
        max_length=16,
        choices=PermissionRemember.choices,
        default=PermissionRemember.ONCE,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "harness_permission_request"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"PermissionRequest({self.tool}:{self.pattern}={self.status})"


class PermissionAllowlist(models.Model):
    """Persisted ``always`` approvals: tool + pattern grants.

    Minimal v1 store. M6 may relocate this to org/workspace settings;
    the repository lookup keeps callers insulated.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization_id = models.UUIDField(
        help_text="Owning organization (scoping, no FK to avoid cycles).",
    )
    workspace_id = models.UUIDField(null=True, blank=True)
    tool = models.CharField(max_length=64)
    pattern = models.CharField(max_length=1024)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "harness_permission_allowlist"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization_id", "workspace_id", "tool", "pattern"],
                name="uniq_allowlist_scope_tool_pattern",
            )
        ]

    def __str__(self) -> str:
        return f"Allowlist({self.tool}:{self.pattern})"
