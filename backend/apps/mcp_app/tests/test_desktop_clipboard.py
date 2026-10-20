"""Access-control and option-parity tests for the MCP desktop clipboard tool."""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, Mock

import pytest

from apps.accounts.models import APIKeyPermission
from apps.mcp_app.server import (
    _TOOL_HANDLERS,
    _TOOL_PERMISSIONS,
    _TOOLS,
    _call_read_desktop_clipboard,
)
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import RunnerStatus, WorkspaceStatus
from apps.runners.services import RunnerService
from common.utils import hash_token


def _setup_workspace():
    from django.contrib.auth import get_user_model

    org = Organization.objects.create(
        name=f"Clipboard MCP {uuid.uuid4().hex[:6]}",
        slug=f"clipboard-mcp-{uuid.uuid4().hex[:10]}",
    )
    user_model = get_user_model()
    owner = user_model.objects.create_user(
        email=f"clipboard-owner-{uuid.uuid4().hex[:8]}@example.com",
        password="secret",
    )
    member = user_model.objects.create_user(
        email=f"clipboard-member-{uuid.uuid4().hex[:8]}@example.com",
        password="secret",
    )
    Membership.objects.create(user=owner, organization=org, role=MembershipRole.MEMBER)
    Membership.objects.create(user=member, organization=org, role=MembershipRole.MEMBER)
    from apps.runners.models import Runner

    runner = Runner.objects.create(
        name="clipboard-runner",
        api_token_hash=hash_token(f"clipboard-{uuid.uuid4().hex}"),
        status=RunnerStatus.ONLINE,
        sid=f"clipboard-sid-{uuid.uuid4().hex[:8]}",
        organization=org,
        available_runtimes=["docker"],
    )
    from apps.runners.models import Workspace

    workspace = Workspace.objects.create(
        runner=runner,
        name="Clipboard workspace",
        status=WorkspaceStatus.RUNNING,
        created_by=owner,
    )
    return org, owner, member, workspace


def _key(user, *permissions):
    permitted = {permission.value for permission in permissions}

    class Key:
        def has_permission(self, permission):
            return getattr(permission, "value", permission) in permitted

    key = Key()
    key.user = user
    return key


def _install_runner_service(
    monkeypatch, workspace, *, clipboard_text="private clipboard text"
):
    service = RunnerService()
    service.workspaces = Mock()
    service.workspaces.get_by_id.return_value = workspace
    service.read_desktop_clipboard = AsyncMock(return_value=clipboard_text)
    monkeypatch.setattr("apps.mcp_app.server._runner_service", lambda: service)
    return service


def _payload(result):
    assert len(result) == 1
    return json.loads(result[0].text)


def test_clipboard_tool_is_registered_with_terminal_permission():
    tool = next(tool for tool in _TOOLS if tool.name == "read_desktop_clipboard")
    assert tool.inputSchema["required"] == ["workspace_id"]
    assert tool.inputSchema["additionalProperties"] is False
    assert (
        _TOOL_PERMISSIONS["read_desktop_clipboard"]
        == APIKeyPermission.TERMINAL_ACCESS
    )
    assert "read_desktop_clipboard" in _TOOL_HANDLERS


@pytest.mark.django_db
def test_terminal_permission_is_required_and_prevents_workspace_lookup(monkeypatch):
    org, owner, _, workspace = _setup_workspace()
    service = _install_runner_service(monkeypatch, workspace)

    result = _call_read_desktop_clipboard(
        _key(owner), org.id, {"workspace_id": str(workspace.id)}
    )

    assert "terminal:access required" in result[0].text
    service.workspaces.get_by_id.assert_not_called()
    service.read_desktop_clipboard.assert_not_awaited()


@pytest.mark.django_db
def test_member_cannot_read_another_members_clipboard(monkeypatch):
    org, _, member, workspace = _setup_workspace()
    service = _install_runner_service(monkeypatch, workspace)

    result = _call_read_desktop_clipboard(
        _key(member, APIKeyPermission.TERMINAL_ACCESS),
        org.id,
        {"workspace_id": str(workspace.id)},
    )

    assert result[0].text == "Error: Workspace not found"
    service.read_desktop_clipboard.assert_not_awaited()


@pytest.mark.django_db
def test_org_admin_can_read_another_members_workspace(monkeypatch):
    org, _, _, workspace = _setup_workspace()
    from django.contrib.auth import get_user_model

    admin = get_user_model().objects.create_user(
        email=f"clipboard-admin-{uuid.uuid4().hex[:8]}@example.com",
        password="secret",
    )
    Membership.objects.create(user=admin, organization=org, role=MembershipRole.ADMIN)
    service = _install_runner_service(monkeypatch, workspace)

    result = _call_read_desktop_clipboard(
        _key(admin, APIKeyPermission.TERMINAL_ACCESS),
        org.id,
        {"workspace_id": str(workspace.id)},
    )

    assert _payload(result) == {"text": "private clipboard text"}
    service.read_desktop_clipboard.assert_awaited_once_with(workspace.id)


@pytest.mark.django_db
def test_workspace_from_another_org_is_not_visible(monkeypatch):
    org, owner, _, workspace = _setup_workspace()
    other_org = Organization.objects.create(
        name=f"Other Clipboard {uuid.uuid4().hex[:6]}",
        slug=f"other-clipboard-{uuid.uuid4().hex[:10]}",
    )
    Membership.objects.create(
        user=owner, organization=other_org, role=MembershipRole.ADMIN
    )
    service = _install_runner_service(monkeypatch, workspace)

    result = _call_read_desktop_clipboard(
        _key(owner, APIKeyPermission.TERMINAL_ACCESS),
        other_org.id,
        {"workspace_id": str(workspace.id)},
    )

    assert result[0].text == "Error: Workspace not found"
    service.read_desktop_clipboard.assert_not_awaited()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "args,expected_call",
    [
        ({}, "default"),
        ({"copy_selection": True}, "selection-default-shift"),
        ({"copy_selection": True, "shift": True}, "selection-shift"),
        ({"copy_selection": True, "shift": False}, "selection-default-shift"),
    ],
)
def test_clipboard_options_forward_with_rest_parity(monkeypatch, args, expected_call):
    org, owner, _, workspace = _setup_workspace()
    service = _install_runner_service(monkeypatch, workspace)

    result = _call_read_desktop_clipboard(
        _key(owner, APIKeyPermission.TERMINAL_ACCESS),
        org.id,
        {"workspace_id": str(workspace.id), **args},
    )

    assert _payload(result) == {"text": "private clipboard text"}
    if expected_call == "default":
        service.read_desktop_clipboard.assert_awaited_once_with(workspace.id)
    else:
        service.read_desktop_clipboard.assert_awaited_once_with(
            workspace.id,
            copy_selection=True,
            shift=expected_call == "selection-shift",
        )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "options",
    [
        {"copy_selection": "true"},
        {"copy_selection": 1},
        {"shift": True},
        {"copy_selection": True, "shift": 1},
        {"copy_selection": True, "unexpected": False},
    ],
)
def test_invalid_options_are_rejected_without_clipboard_capture(monkeypatch, options):
    org, owner, _, workspace = _setup_workspace()
    service = _install_runner_service(monkeypatch, workspace)

    result = _call_read_desktop_clipboard(
        _key(owner, APIKeyPermission.TERMINAL_ACCESS),
        org.id,
        {"workspace_id": str(workspace.id), **options},
    )

    assert result[0].text == "Error: Invalid clipboard read options"
    service.read_desktop_clipboard.assert_not_awaited()


@pytest.mark.django_db
def test_runner_error_is_sanitized_without_logging_clipboard_text(monkeypatch, caplog):
    org, owner, _, workspace = _setup_workspace()
    service = _install_runner_service(monkeypatch, workspace)
    service.read_desktop_clipboard.side_effect = RuntimeError("secret runner detail")

    result = _call_read_desktop_clipboard(
        _key(owner, APIKeyPermission.TERMINAL_ACCESS),
        org.id,
        {"workspace_id": str(workspace.id)},
    )

    assert result[0].text == "Error: Unable to read desktop clipboard"
    assert "secret runner detail" not in result[0].text
    assert "secret runner detail" not in caplog.text
