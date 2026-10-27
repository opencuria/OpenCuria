"""Who may reach a workspace, shared by RunnerService and every adapter.

REST resolves workspaces through :meth:`get_workspace_for_user`; the
realtime adapters (Socket.IO frontend namespace and the desktop proxy) only
know the authenticated user ID and use the boolean predicates. Owners must
be members of the workspace runner's organization; organization admins may
additionally reach the desktop of any workspace in their organization.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from apps.organizations.services import OrganizationService

from ...exceptions import WorkspaceNotFoundError

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids runtime cycles
    from apps.accounts.models import User
    from apps.organizations.models import Membership

    from ...models import Workspace


class WorkspaceAccessMixin:
    """Workspace authorization rules shared by RunnerService."""

    def get_workspace_for_user(
        self,
        workspace_id: uuid.UUID,
        *,
        user,
        organization_id: uuid.UUID,
    ) -> Workspace:
        """Return a workspace only when it belongs to the active org and owner."""
        workspace = self.get_workspace(workspace_id)
        if workspace.runner.organization_id != organization_id:
            raise WorkspaceNotFoundError(str(workspace_id))
        if workspace.created_by_id != user.id:
            raise WorkspaceNotFoundError(str(workspace_id))
        return workspace

    def can_user_access_workspace(self, user_id: int | str, workspace_id: str) -> bool:
        """Return whether the user is an organization member owning the workspace."""
        found = self._workspace_membership(user_id, workspace_id)
        if found is None:
            return False
        workspace, membership = found
        return workspace.created_by_id == membership.user_id

    def can_user_view_workspace_desktop(
        self, user_id: int | str, workspace_id: str
    ) -> bool:
        """Return whether the user owns the workspace or administers its org."""
        found = self._workspace_membership(user_id, workspace_id)
        if found is None:
            return False
        workspace, membership = found
        return (
            membership.role == "admin" or workspace.created_by_id == membership.user_id
        )

    def resolve_desktop_viewer(
        self, user_id: int | str, workspace_id: str
    ) -> tuple[User, uuid.UUID]:
        """Return the viewer user and organization a desktop tunnel renews for.

        Raises WorkspaceNotFoundError when the workspace is gone or the user
        is no longer a member of its organization.
        """
        found = self._workspace_membership(user_id, workspace_id)
        if found is None:
            raise WorkspaceNotFoundError(str(workspace_id))
        workspace, membership = found
        return membership.user, workspace.runner.organization_id

    def _workspace_membership(
        self, user_id: int | str, workspace_id: str
    ) -> tuple[Workspace, Membership] | None:
        """Return the workspace and the user's membership in its organization."""
        try:
            workspace_uuid = uuid.UUID(str(workspace_id))
            member_id = int(user_id)
        except (TypeError, ValueError):
            return None
        workspace = self.workspaces.get_by_id(workspace_uuid)
        if workspace is None:
            return None
        membership = OrganizationService().get_membership(
            member_id, workspace.runner.organization_id
        )
        if membership is None:
            return None
        return workspace, membership
