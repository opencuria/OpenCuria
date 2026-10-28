"""Workspace plugin runtime for live work owned by other contexts.

Workspaces compose plugins: this service loads the workspace (owner and
runner organization) and lets the plugins context build the effective
snapshot and resolve its credentials. The harness prepares each run here
instead of reading workspace persistence or composing plugin runtime itself.
"""

from __future__ import annotations

import uuid

from apps.plugins.runtime import prepare_workspace_runtime
from apps.plugins.runtime_snapshot import PreparedPluginRuntime

from ..repositories import WorkspaceRepository


class WorkspacePluginRuntimeService:
    """Prepare the effective plugins of a workspace for one run."""

    def __init__(self, *, workspaces: type[WorkspaceRepository] | None = None) -> None:
        """Inject the workspace repository without constructing runner execution."""
        self.workspaces = workspaces or WorkspaceRepository

    def prepare(
        self, workspace_id: uuid.UUID, organization_id: uuid.UUID
    ) -> PreparedPluginRuntime:
        """Return the workspace's prepared plugin runtime in *organization_id*.

        Runs in the sync ORM context. A missing workspace or one owned by
        another organization fails closed with ``PluginCredentialConfigError``.
        """
        workspace = self.workspaces.get_by_id(workspace_id)
        return prepare_workspace_runtime(workspace=workspace, org_id=organization_id)
