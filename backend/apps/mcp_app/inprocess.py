"""In-process access to the management MCP server for workspace agents.

The built-in OpenCuria plugin (MCP transport ``builtin``) connects a harness
run to the same server ``/mcp`` serves, over in-memory streams: no HTTP, no
API key and no secret inside the workspace. The agent acts as the workspace
owner with every MCP permission, bounded by the owner's role in the
workspace's organization.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import anyio
from asgiref.sync import sync_to_async
from mcp.shared.memory import MessageStream, create_client_server_memory_streams

from apps.accounts.models import APIKeyPermission
from apps.organizations.services import OrganizationService
from apps.runners.services import get_runner_service

from .context import ContextError, ToolContext
from .server import create_mcp_server

#: Every MCP permission; the owner's organization role still gates admin tools.
AGENT_PERMISSIONS = frozenset(permission.value for permission in APIKeyPermission)


@dataclass(frozen=True)
class WorkspaceAgent:
    """Principal of an agent running inside a workspace, acting as its owner."""

    user: Any
    permissions: frozenset[str] = AGENT_PERMISSIONS

    def has_permission(self, permission: APIKeyPermission | str) -> bool:
        """Return whether *permission* is granted (all MCP permissions are)."""
        return getattr(permission, "value", permission) in self.permissions


def agent_context(workspace_id: uuid.UUID, organization_id: uuid.UUID) -> ToolContext:
    """Return the context the agent in *workspace_id* calls tools with.

    Raises:
        ContextError: When the workspace is gone or belongs to another
            organization, or its owner is no longer a member.
    """
    workspace = get_runner_service().get_workspace_or_none(workspace_id)
    runner = getattr(workspace, "runner", None)
    org_id = getattr(runner, "organization_id", None)
    if workspace is None or str(org_id) != str(organization_id):
        raise ContextError("The workspace does not belong to the organization.")
    owner = workspace.created_by
    memberships = OrganizationService().list_user_organizations(owner) if owner else []
    membership = next((m for m in memberships if m["id"] == org_id), None)
    if membership is None:
        raise ContextError("The workspace owner is not a member of its organization.")
    return ToolContext(
        principal=WorkspaceAgent(user=owner),
        organization_id=org_id,
        organization_name=membership["name"],
        role=str(membership["role"]),
        workspace_id=workspace.id,
    )


@asynccontextmanager
async def connect(
    workspace_id: uuid.UUID, organization_id: uuid.UUID
) -> AsyncIterator[MessageStream]:
    """Yield client ``(read, write)`` streams of a server bound to the agent."""
    ctx = await sync_to_async(agent_context)(workspace_id, organization_id)
    server = create_mcp_server(ctx)
    async with create_client_server_memory_streams() as (client, (read, write)):
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(
                lambda: server.run(read, write, server.create_initialization_options())
            )
            try:
                yield client
            finally:
                task_group.cancel_scope.cancel()
