"""Fixtures for MCP tool tests: an organization, a user, a runner, a workspace."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from asgiref.sync import sync_to_async

from apps.accounts.models import User
from apps.mcp_app.context import ToolContext
from apps.mcp_app.server import call_tool
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import RunnerStatus, WorkspaceStatus
from apps.runners.models import Runner, Workspace


@dataclass
class World:
    """Persisted fixture graph shared by the domain tests."""

    org: Organization
    user: User
    runner: Runner
    workspace: Workspace

    def ctx(
        self,
        *,
        user: User | None = None,
        role: str = "member",
    ) -> ToolContext:
        return ToolContext(
            user=user or self.user,
            organization_id=self.org.id,
            organization_name=self.org.name,
            role=role,
            workspace_id=self.workspace.id,
        )


def _build_world() -> World:
    org = Organization.objects.create(
        name="MCP Org", slug=f"mcp-org-{uuid.uuid4().hex[:10]}"
    )
    user = User.objects.create_user(email=f"mcp-{uuid.uuid4().hex[:8]}@example.com")
    Membership.objects.create(user=user, organization=org, role=MembershipRole.MEMBER)
    runner = Runner.objects.create(
        organization=org,
        name="local-qemu",
        api_token_hash=uuid.uuid4().hex,
        status=RunnerStatus.ONLINE,
    )
    workspace = Workspace.objects.create(
        runner=runner,
        created_by=user,
        name="billing-api",
        status=WorkspaceStatus.RUNNING,
    )
    return World(org=org, user=user, runner=runner, workspace=workspace)


@pytest.fixture
async def world(transactional_db) -> World:
    """Organization member with one online runner and one running workspace."""
    return await sync_to_async(_build_world)()


async def call(ctx: ToolContext, tool: str, /, **arguments: Any):
    """Run a tool through the real dispatcher (validation + error mapping)."""
    return await call_tool(ctx, tool, arguments)


async def ok(ctx: ToolContext, tool: str, /, **arguments: Any) -> dict[str, Any]:
    """Call a tool and return its structured result, failing on tool errors."""
    result = await call(ctx, tool, **arguments)
    assert not result.isError, result.content[0].text
    assert json.loads(result.content[0].text) == result.structuredContent
    return result.structuredContent


async def fails(ctx: ToolContext, tool: str, /, **arguments: Any) -> str:
    """Call a tool that must fail and return its error text."""
    result = await call(ctx, tool, **arguments)
    assert result.isError, result.content[0].text
    return result.content[0].text
