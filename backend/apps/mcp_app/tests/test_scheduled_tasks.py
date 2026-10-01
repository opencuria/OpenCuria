from __future__ import annotations

import json
import uuid

import pytest
from asgiref.sync import sync_to_async

from apps.accounts.models import APIKeyPermission, User
from apps.mcp_app.server import (
    create_mcp_server,
    _TOOL_HANDLERS,
    _TOOL_PERMISSIONS,
    _call_create_scheduled_task,
    _call_get_scheduled_task,
    _call_list_scheduled_tasks,
    _call_update_scheduled_task,
)
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import RunnerStatus, WorkspaceStatus
from apps.runners.models import Runner, Workspace
from apps.scheduled_tasks.models import ScheduledTask


class FakeAPIKey:
    def __init__(self, user, permissions):
        self.user = user
        self.permissions = {permission.value for permission in permissions}

    def has_permission(self, permission):
        return permission.value in self.permissions


def parse(result):
    text = result[0].text
    if text.startswith("Error: "):
        return {"error": text[7:]}
    return json.loads(text)


@pytest.mark.django_db(transaction=True)
async def test_mcp_scheduled_task_crud_scope_and_tool_permissions(monkeypatch):
    org = await sync_to_async(Organization.objects.create)(
        name="MCP task", slug=f"mcp-task-{uuid.uuid4().hex}"
    )
    owner = await sync_to_async(User.objects.create_user)(
        email=f"mcp-{uuid.uuid4().hex}@example.com"
    )
    stranger = await sync_to_async(User.objects.create_user)(
        email=f"mcp-other-{uuid.uuid4().hex}@example.com"
    )
    await sync_to_async(Membership.objects.create)(
        user=owner, organization=org, role=MembershipRole.MEMBER
    )
    runner = await sync_to_async(Runner.objects.create)(
        organization=org,
        api_token_hash=uuid.uuid4().hex,
        status=RunnerStatus.ONLINE,
    )
    workspace = await sync_to_async(Workspace.objects.create)(
        runner=runner,
        created_by=owner,
        name="mcp workspace",
        status=WorkspaceStatus.RUNNING,
    )
    monkeypatch.setattr(
        "apps.mcp_app.server._get_owned_workspace_or_error",
        lambda api_key, org_id, workspace_id: (workspace, None),
    )
    monkeypatch.setattr(
        "apps.harness.harness_service.HarnessService.validate_provider_for_run",
        lambda self, organization_id, session, provider=None: "openrouter/test",
    )
    key = FakeAPIKey(
        owner, [APIKeyPermission.HARNESS_READ, APIKeyPermission.HARNESS_RUN]
    )
    created = parse(
        await _call_create_scheduled_task(
            key,
            org.id,
            {
                "workspace_id": str(workspace.id),
                "name": "MCP task",
                "prompt": "Review the latest changes",
                "local_time": "09:00",
                "timezone_name": "UTC",
            },
        )
    )
    task_id = created["id"]
    assert (
        parse(
            await sync_to_async(_call_get_scheduled_task)(
                key, org.id, {"task_id": task_id}
            )
        )["name"]
        == "MCP task"
    )
    assert _TOOL_PERMISSIONS["get_scheduled_task"] is APIKeyPermission.HARNESS_READ
    assert _TOOL_HANDLERS["get_scheduled_task"] is not None
    read_key = FakeAPIKey(owner, [APIKeyPermission.HARNESS_READ])
    import mcp.types as mcp_types

    listed_tools = await create_mcp_server(read_key).request_handlers[
        mcp_types.ListToolsRequest
    ](mcp_types.ListToolsRequest(method="tools/list"))
    tool_names = {tool.name for tool in listed_tools.root.tools}
    assert "get_scheduled_task" in tool_names
    assert "create_scheduled_task" not in tool_names

    updated = parse(
        await _call_update_scheduled_task(
            key, org.id, {"task_id": task_id, "name": "Renamed"}
        )
    )
    assert updated["name"] == "Renamed"
    rows = await sync_to_async(_call_list_scheduled_tasks)(key, org.id, {})
    assert parse(rows)[0]["id"] == task_id

    stranger_key = FakeAPIKey(stranger, [APIKeyPermission.HARNESS_READ])
    assert (
        "not found"
        in parse(
            await sync_to_async(_call_get_scheduled_task)(
                stranger_key, org.id, {"task_id": task_id}
            )
        )["error"].lower()
    )
    task = await sync_to_async(ScheduledTask.objects.get)(id=task_id)
    assert task.owner_id == owner.id
