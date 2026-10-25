"""Per-request caller context: API key, user and active organization."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from apps.accounts.models import APIKeyPermission
from apps.organizations.services import OrganizationService

from .errors import ToolError


class ContextError(Exception):
    """The authenticated key cannot act in any (or the requested) organization."""


@dataclass(frozen=True)
class ToolContext:
    """Identity and scope every tool call runs with."""

    api_key: Any
    organization_id: uuid.UUID
    organization_name: str
    role: str

    @property
    def user(self) -> Any:
        """The user who owns the API key."""
        return self.api_key.user

    @property
    def is_admin(self) -> bool:
        """Whether the user administers the active organization."""
        return self.role == "admin"

    def can(self, permission: APIKeyPermission) -> bool:
        """Return whether the API key grants *permission*."""
        return bool(self.api_key.has_permission(permission))

    def require(self, *permissions: APIKeyPermission) -> None:
        """Raise a ToolError naming every missing API key permission."""
        missing = [p.value for p in permissions if not self.can(p)]
        if missing:
            raise ToolError(
                "This API key lacks the permission(s) "
                f"{', '.join(missing)}. Ask the user to add them to the key "
                "under Settings → API keys."
            )

    def require_admin(self, action: str) -> None:
        """Raise a ToolError unless the user is an organization admin."""
        if not self.is_admin:
            raise ToolError(
                f"Only organization admins can {action}. The user is a "
                f"'{self.role}' of '{self.organization_name}'."
            )


def resolve_context(api_key: Any, requested_org_id: str | None) -> ToolContext:
    """Bind *api_key* to the requested or the user's first organization.

    Raises:
        ContextError: When the user has no membership in the organization.
    """
    memberships = OrganizationService().list_user_organizations(api_key.user)
    if not memberships:
        raise ContextError("The API key's user is not a member of any organization.")
    if requested_org_id:
        try:
            wanted = uuid.UUID(requested_org_id.strip())
        except ValueError as exc:
            raise ContextError(
                "X-Organization-Id must be an organization UUID."
            ) from exc
        selected = next((m for m in memberships if m["id"] == wanted), None)
        if selected is None:
            raise ContextError(
                "The API key's user is not a member of the requested organization."
            )
    else:
        selected = memberships[0]
    return ToolContext(
        api_key=api_key,
        organization_id=selected["id"],
        organization_name=selected["name"],
        role=str(selected["role"]),
    )
