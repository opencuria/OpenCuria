from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model

from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import RunnerStatus, WorkspaceStatus
from apps.runners.models import Runner, Workspace
from apps.mcp_app.server import (
    _TOOL_HANDLERS,
    _TOOLS,
    _call_get_workspace,
    _call_list_workspaces,
    _call_set_harness_session_mode,
)
from common.utils import hash_token


@pytest.fixture
def mcp_access_setup(db):
    user_model = get_user_model()
    org = Organization.objects.create(
        name=f"MCP Access Org {uuid.uuid4().hex[:6]}",
        slug=f"mcp-access-org-{uuid.uuid4().hex[:8]}",
    )
    owner = user_model.objects.create_user(
        email=f"mcp-owner-{uuid.uuid4().hex[:6]}@example.com",
        password="secret",
    )
    admin = user_model.objects.create_user(
        email=f"mcp-admin-{uuid.uuid4().hex[:6]}@example.com",
        password="secret",
    )
    Membership.objects.create(user=owner, organization=org, role=MembershipRole.MEMBER)
    Membership.objects.create(user=admin, organization=org, role=MembershipRole.ADMIN)
    runner = Runner.objects.create(
        name="mcp-access-runner",
        api_token_hash=hash_token("mcp-access-runner-token"),
        status=RunnerStatus.ONLINE,
        sid="mcp-access-sid",
        organization=org,
        available_runtimes=["docker"],
    )
    owner_workspace = Workspace.objects.create(
        runner=runner,
        name="Owner Workspace",
        status=WorkspaceStatus.RUNNING,
        created_by=owner,
    )
    admin_workspace = Workspace.objects.create(
        runner=runner,
        name="Admin Workspace",
        status=WorkspaceStatus.RUNNING,
        created_by=admin,
    )
    return {
        "org": org,
        "owner_workspace": owner_workspace,
        "admin_workspace": admin_workspace,
        "admin_api_key": SimpleNamespace(user=admin),
    }


def _parse_text_payload(result) -> str:
    assert len(result) == 1
    return result[0].text


@pytest.mark.django_db
def test_mcp_list_workspaces_is_owner_scoped(mcp_access_setup):
    result = _call_list_workspaces(
        mcp_access_setup["admin_api_key"],
        mcp_access_setup["org"].id,
        {},
    )

    payload = json.loads(_parse_text_payload(result))
    assert [entry["id"] for entry in payload] == [
        str(mcp_access_setup["admin_workspace"].id)
    ]


@pytest.mark.django_db
def test_mcp_get_workspace_rejects_foreign_admin_access(mcp_access_setup):
    result = _call_get_workspace(
        mcp_access_setup["admin_api_key"],
        mcp_access_setup["org"].id,
        {"workspace_id": str(mcp_access_setup["owner_workspace"].id)},
    )

    assert _parse_text_payload(result) == "Error: Workspace not found"


@pytest.mark.django_db
def test_mcp_harness_tools_require_permissions():
    """Harness MCP tools map to harness:* API key permissions."""
    from apps.accounts.models import APIKeyPermission
    from apps.mcp_app.server import _TOOL_PERMISSIONS

    assert _TOOL_PERMISSIONS["list_harness_sessions"] == APIKeyPermission.HARNESS_READ
    assert _TOOL_PERMISSIONS["create_harness_session"] == APIKeyPermission.HARNESS_RUN
    assert (
        _TOOL_PERMISSIONS["resolve_harness_permission"]
        == APIKeyPermission.HARNESS_PERMISSIONS
    )
    assert _TOOL_PERMISSIONS["delete_harness_session"] == APIKeyPermission.HARNESS_RUN
    assert _TOOL_PERMISSIONS["set_harness_session_mode"] == APIKeyPermission.HARNESS_RUN
    assert (
        _TOOL_PERMISSIONS["list_harness_conversations"] == APIKeyPermission.HARNESS_READ
    )
    assert _TOOL_PERMISSIONS["list_provider_models"] == APIKeyPermission.HARNESS_READ
    assert _TOOL_PERMISSIONS["take_desktop_control"] == APIKeyPermission.HARNESS_RUN


@pytest.mark.django_db
def test_mcp_legacy_agent_tools_are_removed():
    """Negative test: legacy agent/chat/conversation/prompt tools are gone."""
    names = {tool.name for tool in _TOOLS}
    for legacy in [
        "list_workspace_chats",
        "list_chat_sessions",
        "run_prompt",
        "cancel_prompt",
        "list_agents",
        "list_conversations",
        "list_org_agent_definitions",
        "create_org_agent_definition",
        "update_org_agent_definition",
        "delete_org_agent_definition",
        "duplicate_org_agent_definition",
        "toggle_org_agent_definition_activation",
    ]:
        assert legacy not in names
        assert legacy not in _TOOL_HANDLERS


@pytest.fixture
def mcp_harness_service(monkeypatch):
    """HarnessService wired into MCP handlers for session tool tests."""
    from apps.harness.harness_service import HarnessService
    from apps.harness.permissions.evaluator import PermissionEvaluator
    from apps.harness.permissions.service import PermissionService
    from apps.mcp_app import server as mcp_server

    async def _drop_emit(event: str, data: dict) -> None:
        return None

    service = HarnessService(
        permissions=PermissionService(
            evaluator=PermissionEvaluator(global_rules={"*": "allow"})
        ),
        emit=_drop_emit,
        provider_factory=lambda _org: None,
    )
    monkeypatch.setattr(mcp_server, "_get_harness_service", lambda: service)
    return service


@pytest.mark.django_db(transaction=True)
def test_mcp_set_harness_session_mode_happy_path(mcp_access_setup, mcp_harness_service):
    """Idle session mode switch aligns agent_name and registers the MCP tool."""
    tool_names = {tool.name for tool in _TOOLS}
    assert "set_harness_session_mode" in tool_names
    assert "set_harness_session_mode" in _TOOL_HANDLERS

    session = mcp_harness_service.create_session(
        workspace_id=mcp_access_setup["admin_workspace"].id,
        organization_id=mcp_access_setup["org"].id,
        prompt="mcp mode switch",
        mode="build",
    )
    assert session.mode == "build"

    result = _call_set_harness_session_mode(
        mcp_access_setup["admin_api_key"],
        mcp_access_setup["org"].id,
        {"session_id": str(session.id), "mode": "plan"},
    )

    payload = json.loads(_parse_text_payload(result))
    assert payload["mode"] == "plan"
    assert payload["agent_name"] == "plan"


@pytest.mark.django_db(transaction=True)
def test_mcp_set_harness_session_mode_rejects_busy_run(
    mcp_access_setup, mcp_harness_service, monkeypatch
):
    """Mode switch while a run is active returns the same error as REST 409."""
    session = mcp_harness_service.create_session(
        workspace_id=mcp_access_setup["admin_workspace"].id,
        organization_id=mcp_access_setup["org"].id,
        prompt="mcp mode busy",
    )
    monkeypatch.setattr(mcp_harness_service, "is_running", lambda _sid: True)

    result = _call_set_harness_session_mode(
        mcp_access_setup["admin_api_key"],
        mcp_access_setup["org"].id,
        {"session_id": str(session.id), "mode": "plan"},
    )

    assert (
        _parse_text_payload(result)
        == f"Error: Harness session '{session.id}' already has an active run"
    )
