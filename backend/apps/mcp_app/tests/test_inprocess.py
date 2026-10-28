"""Built-in OpenCuria plugin: workspace agents use the MCP server in-process."""

from __future__ import annotations

import dataclasses
import json
import uuid
from importlib import import_module

import pytest
from asgiref.sync import sync_to_async
from django.apps import apps as django_apps

from apps.harness.mcp_client.runtime import McpRuntime
from apps.harness.tools.base import ToolRegistry
from apps.mcp_app.context import ContextError
from apps.mcp_app.inprocess import agent_context
from apps.mcp_app.tools import TOOLS
from apps.organizations.models import Membership
from apps.plugins.models import OrgPluginActivation, WorkspacePluginActivation
from apps.plugins.runtime_snapshot import PreparedPluginRuntime
from apps.plugins.services import OPENCURIA_PLUGIN_ID
from apps.runners.services.workspace_plugin_runtime import (
    WorkspacePluginRuntimeService,
)

from .conftest import World

seed = import_module("apps.plugins.migrations.0010_seed_opencuria_plugin")


def _enable_plugin(world: World) -> None:
    """Seed the plugin (DB flushes drop migration data) and switch it on."""
    seed.seed_opencuria_plugin(django_apps, None)
    OrgPluginActivation.objects.create(
        organization=world.org, plugin_id=OPENCURIA_PLUGIN_ID
    )
    WorkspacePluginActivation.objects.create(
        workspace=world.workspace, plugin_id=OPENCURIA_PLUGIN_ID
    )


def _prepare_run(world: World) -> PreparedPluginRuntime:
    """Build the plugin runtime exactly like a harness run does."""
    return WorkspacePluginRuntimeService().prepare(world.workspace.id, world.org.id)


async def test_agent_acts_as_workspace_owner(world):
    ctx = await sync_to_async(agent_context)(world.workspace.id, world.org.id)

    assert ctx.user.id == world.user.id
    assert ctx.role == "member"
    assert ctx.organization_id == world.org.id
    assert ctx.workspace_id == world.workspace.id


async def test_agent_context_fails_closed(world):
    with pytest.raises(ContextError):
        await sync_to_async(agent_context)(world.workspace.id, uuid.uuid4())
    with pytest.raises(ContextError):
        await sync_to_async(agent_context)(uuid.uuid4(), world.org.id)

    await sync_to_async(Membership.objects.filter(user=world.user).delete)()
    with pytest.raises(ContextError):
        await sync_to_async(agent_context)(world.workspace.id, world.org.id)


async def test_enabled_plugin_gives_the_agent_every_mcp_tool(world):
    await sync_to_async(_enable_plugin)(world)
    prepared = await sync_to_async(_prepare_run)(world)
    runtime = McpRuntime()
    try:
        await runtime.setup(prepared=prepared, accessor=None)
        names = runtime.register_tools(ToolRegistry())

        assert runtime.skipped == []
        assert len(names) == len(TOOLS)
        assert "mcp_opencuria_mcp_workspace_list" in names
        connection = runtime.connections[0]
        context = json.loads(
            (await connection.call_tool("opencuria_context", {})).output
        )
        assert context["current_workspace_id"] == str(world.workspace.id)
        assert context["organization"]["role"] == "member"
        listed = json.loads((await connection.call_tool("workspace_list", {})).output)
        assert [w["name"] for w in listed["workspaces"]] == ["billing-api"]
    finally:
        await runtime.aclose()


async def test_plugin_is_switched_per_organization_and_workspace(world):
    await sync_to_async(_enable_plugin)(world)
    assert (await sync_to_async(_prepare_run)(world)).snapshot.plugins

    await sync_to_async(
        WorkspacePluginActivation.objects.filter(workspace=world.workspace).delete
    )()
    assert not (await sync_to_async(_prepare_run)(world)).snapshot.plugins

    await sync_to_async(WorkspacePluginActivation.objects.create)(
        workspace=world.workspace, plugin_id=OPENCURIA_PLUGIN_ID
    )
    await sync_to_async(
        OrgPluginActivation.objects.filter(organization=world.org).delete
    )()
    assert not (await sync_to_async(_prepare_run)(world)).snapshot.plugins


async def test_unbound_server_is_skipped_without_failing_the_run(world):
    await sync_to_async(_enable_plugin)(world)
    prepared = await sync_to_async(_prepare_run)(world)
    foreign = dataclasses.replace(prepared.snapshot, organization_id=uuid.uuid4())
    runtime = McpRuntime()
    try:
        await runtime.setup(
            prepared=PreparedPluginRuntime(snapshot=foreign), accessor=None
        )

        assert runtime.connections == []
        assert [s["plugin"] for s in runtime.skipped] == ["opencuria"]
    finally:
        await runtime.aclose()
