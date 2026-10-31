"""Characterize desktop authorization and transport contracts across actions."""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, Mock

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.test import Client

from apps.accounts.models import APIKeyPermission
from apps.organizations.models import Membership, Organization
from apps.runners.enums import RunnerStatus
from apps.runners.services import RunnerService
from apps.runners.tests.test_workspace_access_controls import _make_client
from common.exceptions import NotFoundError


@pytest.fixture
def desktop_access(workspace, user, organization, monkeypatch):
    """Use real service policy, replacing only runner and harness transport."""
    Membership.objects.create(user=user, organization=organization, role="member")
    gateway = Mock(
        abort_busy_computeruse=AsyncMock(return_value=[]),
        busy_workspace_ids=Mock(return_value=set()),
    )
    service = RunnerService(harness_gateway=gateway)
    service._call_runner = AsyncMock(return_value={"ok": True, "text": "clipboard"})
    key = str(workspace.id)
    monkeypatch.setitem(service._active_desktops, key, {"computer_use": False})
    monkeypatch.setattr("apps.runners.api._get_service", lambda: service)
    return service, gateway, workspace, user, organization


def _desktop_request(
    client: Client, workspace_id: uuid.UUID, action: str
) -> HttpResponse:
    """Send the existing bodyless read/control or text-write contract."""
    payload = {"text": "new clipboard"} if action == "clipboard/write" else None
    return client.generic(
        "POST",
        f"/api/v1/workspaces/{workspace_id}/desktop/{action}/",
        json.dumps(payload) if payload is not None else b"",
        content_type="application/json",
    )


@pytest.mark.django_db
@pytest.mark.parametrize("interface", ["rest", "service"])
@pytest.mark.parametrize(
    "action", ["clipboard/read", "clipboard/write", "take-control"]
)
@pytest.mark.parametrize(
    "actor,expected_status",
    [
        ("owner", 200),
        ("admin", 200),
        ("member", 404),
        ("nonmember", None),
        ("other_org_admin", 404),
        ("missing_org", None),
        ("missing_workspace", 404),
    ],
)
def test_desktop_action_access_contract(
    desktop_access, action, actor, expected_status, interface
):
    """All actions share owner/admin access and conceal foreign workspaces."""
    service, gateway, workspace, user, org = desktop_access
    target_id = workspace.id
    if actor in {"admin", "member", "nonmember"}:
        user = get_user_model().objects.create_user(
            email=f"desktop-{uuid.uuid4().hex[:8]}@example.com", password="secret"
        )
        if actor != "nonmember":
            Membership.objects.create(user=user, organization=org, role=actor)
    elif actor == "other_org_admin":
        org = Organization.objects.create(
            name="Other", slug=f"other-{uuid.uuid4().hex}"
        )
        Membership.objects.create(user=user, organization=org, role="admin")
    elif actor == "missing_org":
        org.id = uuid.uuid4()
    elif actor == "missing_workspace":
        target_id = uuid.uuid4()

    if interface == "service":
        methods = {
            "clipboard/read": service.read_desktop_clipboard,
            "clipboard/write": service.write_desktop_clipboard,
            "take-control": service.take_desktop_control,
        }
        args = ("new clipboard",) if action == "clipboard/write" else ()
        context = {"user": user, "organization_id": org.id}
        call = async_to_sync(methods[action])
        if expected_status != 200:
            resource = "Organization" if expected_status is None else "Workspace"
            identifier = org.id if expected_status is None else target_id
            with pytest.raises(NotFoundError, match=f"{resource} '{identifier}'"):
                call(target_id, *args, **context)
            service._call_runner.assert_not_awaited()
            gateway.abort_busy_computeruse.assert_not_awaited()
        else:
            assert (
                call(target_id, *args, **context)
                == {
                    "clipboard/read": "clipboard",
                    "clipboard/write": None,
                    "take-control": [],
                }[action]
            )
            if action == "take-control":
                gateway.abort_busy_computeruse.assert_awaited_once_with(workspace.id)
            else:
                service._call_runner.assert_awaited_once()
        return

    client = _make_client(
        user=user,
        org=org,
        permissions=[
            APIKeyPermission.TERMINAL_ACCESS.value,
            APIKeyPermission.HARNESS_RUN.value,
        ],
    )

    if expected_status is None:
        # The existing membership gate runs before the route's error mapping.
        with pytest.raises(NotFoundError, match=f"Organization '{org.id}'"):
            _desktop_request(client, target_id, action)
        service._call_runner.assert_not_awaited()
        gateway.abort_busy_computeruse.assert_not_awaited()
        return

    response = _desktop_request(client, target_id, action)

    assert response.status_code == expected_status, response.content
    if expected_status == 404:
        assert response.json() == {
            "detail": f"Workspace '{target_id}' not found",
            "code": "not_found",
            "gaps": None,
        }
        service._call_runner.assert_not_awaited()
        gateway.abort_busy_computeruse.assert_not_awaited()
    elif action == "take-control":
        assert response.json() == {"aborted_session_ids": []}
        gateway.abort_busy_computeruse.assert_awaited_once_with(workspace.id)
        service._call_runner.assert_not_awaited()
    else:
        assert response.json() == {"text": "clipboard"}
        events = [call.args[1] for call in service._call_runner.await_args_list]
        assert events == (
            ["desktop:clipboard_read"]
            if action == "clipboard/read"
            else ["desktop:clipboard_write", "desktop:clipboard_read"]
        )
        gateway.abort_busy_computeruse.assert_not_awaited()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "action", ["clipboard/read", "clipboard/write", "take-control"]
)
@pytest.mark.parametrize("blocked_by", ["capture", "offline"])
def test_desktop_action_availability_contract(desktop_access, action, blocked_by):
    """Capture fences every action; aborting computer-use also works offline."""
    service, gateway, workspace, user, org = desktop_access
    if blocked_by == "capture":
        workspace.active_operation = "capturing_image"
        workspace.save(update_fields=["active_operation"])
    else:
        workspace.runner.status = RunnerStatus.OFFLINE
        workspace.runner.save(update_fields=["status"])
    client = _make_client(
        user=user,
        org=org,
        permissions=[
            APIKeyPermission.TERMINAL_ACCESS.value,
            APIKeyPermission.HARNESS_RUN.value,
        ],
    )

    response = _desktop_request(client, workspace.id, action)

    if blocked_by == "offline" and action == "take-control":
        assert response.status_code == 200, response.content
        gateway.abort_busy_computeruse.assert_awaited_once_with(workspace.id)
    else:
        assert response.status_code == 409, response.content
        assert response.json()["code"] == "conflict"
        gateway.abort_busy_computeruse.assert_not_awaited()
    service._call_runner.assert_not_awaited()
