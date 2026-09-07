"""MCP access-control tests for workspace background processes."""

from __future__ import annotations

import inspect
import json
import os
import uuid
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model

from apps.accounts.models import APIKeyPermission
from apps.mcp_app.server import (
    _TOOL_HANDLERS,
    _TOOL_PERMISSIONS,
    _TOOLS,
    _call_get_process,
    _call_list_processes,
    _call_start_process,
    _call_stop_process,
)
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import RunnerStatus, WorkspaceStatus
from apps.runners.models import Runner, Workspace
from common.utils import hash_token

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")


def _make_process_setup():
    """Create an org with owner + stranger and one owned workspace (sync)."""
    user_model = get_user_model()
    org = Organization.objects.create(
        name=f"Proc Org {uuid.uuid4().hex[:6]}",
        slug=f"proc-org-{uuid.uuid4().hex[:8]}",
    )
    owner = user_model.objects.create_user(
        email=f"proc-owner-{uuid.uuid4().hex[:6]}@example.com",
        password="secret",
    )
    stranger = user_model.objects.create_user(
        email=f"proc-stranger-{uuid.uuid4().hex[:6]}@example.com",
        password="secret",
    )
    Membership.objects.create(user=owner, organization=org, role=MembershipRole.MEMBER)
    Membership.objects.create(
        user=stranger, organization=org, role=MembershipRole.MEMBER
    )
    runner = Runner.objects.create(
        name="proc-runner",
        api_token_hash=hash_token(f"proc-{uuid.uuid4().hex}"),
        status=RunnerStatus.ONLINE,
        sid=f"proc-sid-{uuid.uuid4().hex[:8]}",
        organization=org,
        available_runtimes=["docker"],
    )
    workspace = Workspace.objects.create(
        runner=runner,
        name="Proc Workspace",
        status=WorkspaceStatus.RUNNING,
        created_by=owner,
    )
    return {
        "org": org,
        "owner": owner,
        "stranger": stranger,
        "workspace": workspace,
    }


def _text(result) -> str:
    assert len(result) == 1
    return result[0].text


def test_process_tool_permissions() -> None:
    """list/get need read; start/stop need run."""
    assert _TOOL_PERMISSIONS["list_processes"] == (
        APIKeyPermission.WORKSPACES_PROCESSES_READ
    )
    assert _TOOL_PERMISSIONS["get_process"] == (
        APIKeyPermission.WORKSPACES_PROCESSES_READ
    )
    assert _TOOL_PERMISSIONS["start_process"] == (
        APIKeyPermission.WORKSPACES_PROCESSES_RUN
    )
    assert _TOOL_PERMISSIONS["stop_process"] == (
        APIKeyPermission.WORKSPACES_PROCESSES_RUN
    )


def test_process_tools_registered_and_async() -> None:
    """All four tools are exposed and async (awaited, no nested loop)."""
    names = {tool.name for tool in _TOOLS}
    assert {"list_processes", "get_process", "start_process", "stop_process"} <= names
    for name in ("list_processes", "get_process", "start_process", "stop_process"):
        assert name in _TOOL_HANDLERS
        assert inspect.iscoroutinefunction(_TOOL_HANDLERS[name])


@pytest.mark.django_db(transaction=True)
async def test_mcp_list_processes_rejects_foreign_workspace() -> None:
    """A member cannot list processes of a workspace they do not own."""
    from asgiref.sync import sync_to_async

    setup = await sync_to_async(_make_process_setup)()
    api_key = SimpleNamespace(user=setup["stranger"])
    result = await _call_list_processes(
        api_key,
        setup["org"].id,
        {"workspace_id": str(setup["workspace"].id)},
    )
    assert _text(result) == "Error: Workspace not found"


@pytest.mark.django_db(transaction=True)
async def test_mcp_process_handlers_validate_args() -> None:
    """Missing/invalid UUIDs fail fast before the owner check."""
    from asgiref.sync import sync_to_async

    setup = await sync_to_async(_make_process_setup)()
    api_key = SimpleNamespace(user=setup["owner"])
    org_id = setup["org"].id
    workspace_id = str(setup["workspace"].id)
    result = await _call_list_processes(api_key, org_id, {})
    assert "workspace_id is required" in _text(result)
    result = await _call_get_process(api_key, org_id, {"workspace_id": workspace_id})
    assert "process_id is required" in _text(result)
    result = await _call_start_process(
        api_key, org_id, {"workspace_id": workspace_id, "command": "  "}
    )
    assert "command is required" in _text(result)
    result = await _call_stop_process(
        api_key,
        org_id,
        {"workspace_id": workspace_id, "process_id": "not-a-uuid"},
    )
    assert "Invalid process_id UUID" in _text(result)


@pytest.mark.django_db(transaction=True)
async def test_mcp_start_process_happy_path(monkeypatch) -> None:
    """start_process calls RunnerService with the owned workspace + user."""
    from asgiref.sync import sync_to_async

    process_setup = await sync_to_async(_make_process_setup)()
    from datetime import datetime, timezone

    calls: list[tuple] = []

    class _FakeService:
        def get_workspace_for_user(self, workspace_id, *, user, organization_id):
            assert str(workspace_id) == str(process_setup["workspace"].id)
            assert user == process_setup["owner"]
            return process_setup["workspace"]

        async def start_process(
            self, workspace_id, command, *, workdir="/workspace", env=None,
            name="", user=None,
        ):
            calls.append((workspace_id, command, workdir, env, name, user))
            return SimpleNamespace(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                name=name,
                command=command,
                workdir=workdir,
                pid=99,
                log_path="/workspace/.opencuria/processes/x.log",
                status="running",
                exit_code=None,
                started_at=datetime.now(timezone.utc),
                ended_at=None,
                updated_at=datetime.now(timezone.utc),
            )

    monkeypatch.setattr(
        "apps.mcp_app.server._runner_service", lambda: _FakeService()
    )
    api_key = SimpleNamespace(user=process_setup["owner"])
    result = await _call_start_process(
        api_key,
        process_setup["org"].id,
        {
            "workspace_id": str(process_setup["workspace"].id),
            "command": "python server.py",
            "name": "web",
        },
    )
    payload = json.loads(_text(result))
    assert payload["status"] == "running"
    assert payload["pid"] == 99
    assert calls and calls[0][1] == "python server.py"
    assert calls[0][4] == "web"
    assert calls[0][5] == process_setup["owner"]
