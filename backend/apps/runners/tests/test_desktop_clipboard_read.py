"""Desktop clipboard-read options, authorization and failure safety."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from django.test import Client
from pydantic import ValidationError

from apps.accounts.models import APIKey, APIKeyPermission
from apps.harness.repositories import HarnessSessionRepository
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.schemas import DesktopClipboardReadIn
from apps.runners.services import RunnerService
from common.exceptions import ConflictError
from common.utils import generate_api_token, hash_token


@pytest.fixture
def clipboard_api(capture_workspace, monkeypatch):
    """Authorized clipboard endpoint with an isolated RunnerService mock."""
    user, org, workspace = capture_workspace
    service = Mock()
    service.get_workspace.return_value = workspace
    service.read_desktop_clipboard = AsyncMock(return_value="clipboard text")
    monkeypatch.setattr("apps.runners.api._get_service", lambda: service)

    def headers(*, identity=user, organization=org):
        token = generate_api_token()
        APIKey.objects.create(
            user=identity,
            name="clipboard-test",
            key_hash=hash_token(token),
            key_prefix=token[:12],
            permissions=[APIKeyPermission.TERMINAL_ACCESS.value],
        )
        return {
            "HTTP_X_API_KEY": token,
            "HTTP_X_ORGANIZATION_ID": str(organization.id),
        }

    return Client(), headers, service, user, org, workspace


@pytest.fixture
def capture_workspace(user, organization, workspace):
    Membership.objects.create(
        user=user, organization=organization, role=MembershipRole.ADMIN
    )
    return user, organization, workspace


def _post(client: Client, workspace_id, headers, payload=None):
    kwargs = dict(headers)
    if payload is None:
        return client.generic(
            "POST",
            f"/api/v1/workspaces/{workspace_id}/desktop/clipboard/read/",
            b"",
            content_type="application/json",
            **kwargs,
        )
    return client.post(
        f"/api/v1/workspaces/{workspace_id}/desktop/clipboard/read/",
        data=json.dumps(payload),
        content_type="application/json",
        **kwargs,
    )


@pytest.mark.django_db
@pytest.mark.parametrize("role", [MembershipRole.ADMIN, MembershipRole.MEMBER])
def test_read_clipboard_preserves_bodyless_path_for_authorized_admin_and_member(
    clipboard_api, role
):
    client, headers, service, user, org, workspace = clipboard_api
    Membership.objects.filter(user=user, organization=org).update(role=role)

    response = _post(client, workspace.id, headers())

    assert response.status_code == 200, response.content
    assert response.json() == {"text": "clipboard text"}
    service.read_desktop_clipboard.assert_awaited_once_with(workspace.id)


@pytest.mark.django_db
def test_org_admin_can_read_another_members_workspace(clipboard_api):
    client, headers, service, _, org, workspace = clipboard_api
    other = type(workspace.created_by).objects.create_user(
        email=f"clipboard-owner-{uuid.uuid4().hex[:8]}@example.com", password="secret"
    )
    workspace.created_by = other
    workspace.save(update_fields=["created_by"])

    response = _post(client, workspace.id, headers())

    assert response.status_code == 200, response.content
    service.read_desktop_clipboard.assert_awaited_once()


@pytest.mark.django_db
def test_same_org_member_cannot_read_another_owners_clipboard(clipboard_api):
    client, headers, service, user, org, workspace = clipboard_api
    another_owner = type(user).objects.create_user(
        email=f"clipboard-other-{uuid.uuid4().hex[:8]}@example.com", password="secret"
    )
    Membership.objects.create(
        user=another_owner, organization=org, role=MembershipRole.MEMBER
    )

    response = _post(
        client,
        workspace.id,
        headers(identity=another_owner),
        {"copy_selection": True},
    )

    assert response.status_code == 404, response.content
    service.read_desktop_clipboard.assert_not_awaited()


@pytest.mark.django_db
def test_terminal_permission_is_required(clipboard_api):
    client, _, service, user, org, workspace = clipboard_api
    token = generate_api_token()
    APIKey.objects.create(
        user=user,
        name="clipboard-no-terminal",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=[],
    )

    response = _post(
        client,
        workspace.id,
        {
            "HTTP_X_API_KEY": token,
            "HTTP_X_ORGANIZATION_ID": str(org.id),
        },
    )

    assert response.status_code == 403, response.content
    service.read_desktop_clipboard.assert_not_awaited()


@pytest.mark.django_db
def test_other_org_cannot_read_clipboard(clipboard_api):
    client, headers, service, user, _, workspace = clipboard_api
    other_org = Organization.objects.create(
        name="Other clipboard org", slug=f"other-clipboard-{uuid.uuid4().hex[:8]}"
    )
    Membership.objects.create(
        user=user, organization=other_org, role=MembershipRole.ADMIN
    )

    response = _post(
        client, workspace.id, headers(organization=other_org), {"copy_selection": True}
    )

    assert response.status_code == 404, response.content
    service.read_desktop_clipboard.assert_not_awaited()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "payload",
    [
        {"copy_selection": "true"},
        {"copy_selection": 1},
        {"shift": True},
        {"copy_selection": True, "unrecognized": False},
    ],
)
def test_invalid_clipboard_options_are_rejected(clipboard_api, payload):
    client, headers, service, _, _, workspace = clipboard_api

    response = _post(client, workspace.id, headers(), payload)

    assert response.status_code == 422, response.content
    service.read_desktop_clipboard.assert_not_awaited()


def test_clipboard_read_options_schema_is_strict():
    assert DesktopClipboardReadIn().model_dump() == {
        "copy_selection": False,
        "shift": False,
    }
    with pytest.raises(ValidationError):
        DesktopClipboardReadIn(copy_selection="true")
    with pytest.raises(ValidationError):
        DesktopClipboardReadIn(copy_selection=True, shift=1)
    with pytest.raises(ValidationError):
        DesktopClipboardReadIn(shift=True)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"copy_selection": True}, {"copy_selection": True, "shift": False}),
        (
            {"copy_selection": True, "shift": True},
            {"copy_selection": True, "shift": True},
        ),
    ],
)
def test_selection_options_forward_to_service(clipboard_api, payload, expected):
    client, headers, service, _, _, workspace = clipboard_api

    response = _post(client, workspace.id, headers(), payload)

    assert response.status_code == 200, response.content
    service.read_desktop_clipboard.assert_awaited_once_with(workspace.id, **expected)


@pytest.fixture
def selection_service(monkeypatch):
    """RunnerService with lightweight ports for selection-copy tests."""
    service = RunnerService(sio_server=AsyncMock())
    workspace_id = uuid.uuid4()
    workspace = SimpleNamespace(
        id=workspace_id,
        active_operation="",
        current_task_id=None,
        runner=SimpleNamespace(id=uuid.uuid4(), is_online=True),
    )
    service.workspaces = Mock()
    service.workspaces.get_by_id.return_value = workspace
    service.workspaces.touch_activity = Mock()
    service._ensure_workspace_available = Mock()
    service._call_runner = AsyncMock(return_value={"ok": True, "text": "selected"})
    service._active_desktops[str(workspace_id)] = {"computer_use": False}
    monkeypatch.setattr(
        HarnessSessionRepository,
        "list_busy_computeruse_for_workspace",
        Mock(return_value=[]),
    )
    yield service, workspace
    service._active_desktops.pop(str(workspace_id), None)


@pytest.mark.asyncio
async def test_selection_rpc_uses_finite_timeout_and_omits_false_options(
    selection_service,
):
    service, workspace = selection_service

    result = await service.read_desktop_clipboard(workspace.id, copy_selection=True)

    assert result == "selected"
    service._call_runner.assert_awaited_once_with(
        workspace.runner,
        "desktop:clipboard_read",
        {"workspace_id": str(workspace.id), "copy_selection": True},
        timeout=3.5,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        pytest.param(None, id="null"),
        pytest.param("malformed", id="non-dict"),
        pytest.param({"ok": True, "text": 42}, id="numeric-text"),
        pytest.param({"ok": True}, id="missing-text"),
        pytest.param({"ok": False, "text": "sensitive secret"}, id="false-ok"),
        pytest.param({"ok": "true", "text": "selection"}, id="non-true-ok"),
    ],
)
async def test_selection_rejects_malformed_rpc_responses_without_details(
    selection_service, response
):
    service, workspace = selection_service
    service._call_runner.return_value = response

    with pytest.raises(ConflictError) as error:
        await service.read_desktop_clipboard(workspace.id, copy_selection=True)

    assert str(error.value) == "Unable to copy the desktop selection. Try again."
    assert "sensitive secret" not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("sensitive secret"), TimeoutError()])
async def test_selection_rpc_failures_are_sanitized(selection_service, failure):
    service, workspace = selection_service
    service._call_runner.side_effect = failure

    with pytest.raises(ConflictError) as error:
        await service.read_desktop_clipboard(workspace.id, copy_selection=True)

    assert str(error.value) == "Unable to copy the desktop selection. Try again."
    assert "sensitive secret" not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "busy_kind", ["capture", "desktop-computeruse", "session-computeruse"]
)
async def test_selection_copy_rejected_without_rpc_during_capture_or_computeruse(
    selection_service, busy_kind, monkeypatch
):
    service, workspace = selection_service
    if busy_kind == "capture":
        workspace.active_operation = "capturing_image"
        service._ensure_workspace_available.side_effect = ConflictError("capture lock")
    elif busy_kind == "desktop-computeruse":
        service._active_desktops[str(workspace.id)]["computer_use"] = True
    else:
        monkeypatch.setattr(
            HarnessSessionRepository,
            "list_busy_computeruse_for_workspace",
            Mock(return_value=[SimpleNamespace(id=uuid.uuid4())]),
        )

    with pytest.raises(ConflictError):
        await service.read_desktop_clipboard(workspace.id, copy_selection=True)

    service._call_runner.assert_not_awaited()


@pytest.mark.django_db
def test_selection_rpc_error_does_not_expose_runner_detail(clipboard_api):
    client, headers, service, _, _, workspace = clipboard_api
    service.read_desktop_clipboard.side_effect = RuntimeError(
        "copied private clipboard payload"
    )

    response = _post(client, workspace.id, headers(), {"copy_selection": True})

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Unable to copy the desktop selection. Try again."
    )
    assert "copied private clipboard payload" not in response.content.decode()


@pytest.mark.asyncio
async def test_selection_timeout_bounds_full_operation(selection_service):
    service, workspace = selection_service
    entered_rpc = asyncio.Event()

    async def wait_forever(*args, **kwargs):
        entered_rpc.set()
        await asyncio.Event().wait()

    service._call_runner.side_effect = wait_forever
    start = time.monotonic()
    with pytest.raises(ConflictError) as error:
        await service.read_desktop_clipboard(workspace.id, copy_selection=True)

    assert entered_rpc.is_set()
    assert time.monotonic() - start < 4.5
    assert str(error.value) == "Unable to copy the desktop selection. Try again."
    assert service._call_runner.await_args.kwargs["timeout"] == 3.5
