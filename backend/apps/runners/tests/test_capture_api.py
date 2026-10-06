"""REST/MCP capture contracts and desktop capture-fence regression tests."""

import json
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.models import APIKey, APIKeyPermission
from apps.mcp_app.server import _TOOLS, _call_create_image_artifact
from apps.organizations.models import Membership, MembershipRole
from apps.runners.enums import WorkspaceStatus
from apps.runners.models import CaptureRequest
from apps.runners.schemas import ImageArtifactCreateIn
from apps.runners.sio_server import get_runner_service
from common.utils import generate_api_token, hash_token

pytestmark = pytest.mark.django_db


@pytest.fixture
def capture_setup(user, organization, workspace):
    """An authorized owner of a running QEMU workspace."""
    Membership.objects.create(
        user=user, organization=organization, role=MembershipRole.MEMBER
    )
    workspace.runtime_type = "qemu"
    workspace.save(update_fields=["runtime_type"])
    return user, organization, workspace


def _headers(user, organization, permissions):
    token = generate_api_token()
    APIKey.objects.create(
        user=user,
        name="capture-test",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=[permission.value for permission in permissions],
    )
    return {
        "HTTP_X_API_KEY": token,
        "HTTP_X_ORGANIZATION_ID": str(organization.id),
    }


def _capture_url(workspace, global_endpoint):
    if global_endpoint:
        return "/api/v1/image-artifacts/"
    return f"/api/v1/workspaces/{workspace.id}/image-artifacts/"


def _capture(client, workspace, headers, global_endpoint):
    return client.post(
        _capture_url(workspace, global_endpoint),
        data=json.dumps({"name": "Capture", "workspace_id": str(workspace.id)}),
        content_type="application/json",
        **headers,
    )


@pytest.mark.parametrize("global_endpoint", [False, True])
@pytest.mark.parametrize("running", [False, True])
def test_capture_without_flag_preserves_source_running_state(
    capture_setup, global_endpoint, running
):
    user, org, workspace = capture_setup
    workspace.status = WorkspaceStatus.RUNNING if running else WorkspaceStatus.STOPPED
    workspace.save(update_fields=["status"])
    headers = _headers(user, org, [APIKeyPermission.IMAGES_CREATE])
    response = _capture(Client(), workspace, headers, global_endpoint)
    assert response.status_code == 202, response.content
    capture = CaptureRequest.objects.get(workspace=workspace)
    assert capture.prior_running is running
    assert capture.phase == ("stop" if running else "capture")
    assert response.json()["task_id"] == str(capture.child_id)
    assert response.json()["workspace_id"] == str(workspace.id)


@pytest.mark.parametrize("global_endpoint", [False, True])
def test_busy_capture_returns_declared_conflict(capture_setup, global_endpoint):
    user, org, workspace = capture_setup
    headers = _headers(user, org, [APIKeyPermission.IMAGES_CREATE])
    client = Client()
    assert _capture(client, workspace, headers, global_endpoint).status_code == 202
    response = _capture(client, workspace, headers, global_endpoint)
    assert response.status_code == 409, response.content
    assert response.json()["code"] == "conflict"
    assert CaptureRequest.objects.filter(workspace=workspace).count() == 1


@pytest.mark.parametrize("global_endpoint", [False, True])
def test_capture_requires_api_key_permission(capture_setup, global_endpoint):
    user, org, workspace = capture_setup
    response = _capture(Client(), workspace, _headers(user, org, []), global_endpoint)
    assert response.status_code == 403, response.content
    assert not CaptureRequest.objects.exists()


@pytest.mark.parametrize("global_endpoint", [False, True])
def test_capture_hides_another_members_workspace(capture_setup, global_endpoint):
    _, org, workspace = capture_setup
    other = get_user_model().objects.create_user(
        email="capture-outsider@example.com", password="secret"
    )
    Membership.objects.create(user=other, organization=org, role=MembershipRole.MEMBER)
    response = _capture(
        Client(),
        workspace,
        _headers(other, org, [APIKeyPermission.IMAGES_CREATE]),
        global_endpoint,
    )
    assert response.status_code == 404, response.content
    assert not CaptureRequest.objects.exists()


def test_capture_rest_and_mcp_schemas_have_no_restart_flag():
    fields = {"name", "workspace_id", "captured_image_id", "message"}
    assert set(ImageArtifactCreateIn.model_fields) == fields
    tool = next(tool for tool in _TOOLS if tool.name == "create_image_artifact")
    assert set(tool.inputSchema["properties"]) == fields


def test_mcp_capture_calls_service_without_restart_flag(capture_setup, monkeypatch):
    user, org, workspace = capture_setup
    calls = []
    task = SimpleNamespace(id=workspace.id)

    async def create(
        *, workspace_id, name, organization_id, captured_image_id, message
    ):
        calls.append((workspace_id, name, organization_id, captured_image_id))
        return workspace, task

    monkeypatch.setattr(get_runner_service(), "create_image_artifact", create)
    result = _call_create_image_artifact(
        SimpleNamespace(user=user),
        org.id,
        {"workspace_id": str(workspace.id), "name": "Capture"},
    )
    assert json.loads(result[0].text)["task_id"] == str(task.id)
    assert calls == [(workspace.id, "Capture", org.id, None)]


@pytest.mark.parametrize("action", ["status", "take-control"])
def test_desktop_routes_reject_capture_fence(capture_setup, action, monkeypatch):
    user, org, workspace = capture_setup
    workspace.active_operation = "create_image_artifact"
    workspace.save(update_fields=["active_operation"])
    service = get_runner_service()

    def forbidden(*args, **kwargs):
        raise AssertionError("Desktop side effects must not run during capture")

    monkeypatch.setattr(service, "get_desktop_info", forbidden)
    from apps.harness.harness_service import get_harness_service

    monkeypatch.setattr(
        get_harness_service(), "abort_busy_computeruse_for_workspace", forbidden
    )
    headers = _headers(
        user, org, [APIKeyPermission.TERMINAL_ACCESS, APIKeyPermission.HARNESS_RUN]
    )
    client = Client()
    method = client.get if action == "status" else client.post
    response = method(f"/api/v1/workspaces/{workspace.id}/desktop/{action}/", **headers)
    assert response.status_code == 409, response.content
    assert response.json()["code"] == "conflict"


@pytest.mark.parametrize("action", ["status", "take-control"])
def test_desktop_checks_ownership_before_availability(
    capture_setup, action, monkeypatch
):
    _, org, workspace = capture_setup
    other = get_user_model().objects.create_user(
        email="desktop-outsider@example.com", password="secret"
    )
    Membership.objects.create(user=other, organization=org, role=MembershipRole.MEMBER)

    def forbidden(*args, **kwargs):
        raise AssertionError("Availability must not be disclosed to another owner")

    monkeypatch.setattr(get_runner_service(), "_ensure_workspace_available", forbidden)
    headers = _headers(
        other, org, [APIKeyPermission.TERMINAL_ACCESS, APIKeyPermission.HARNESS_RUN]
    )
    client = Client()
    method = client.get if action == "status" else client.post
    response = method(f"/api/v1/workspaces/{workspace.id}/desktop/{action}/", **headers)
    assert response.status_code == 404, response.content
