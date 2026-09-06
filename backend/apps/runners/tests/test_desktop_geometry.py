"""Tests for fixed workspace desktop geometry."""

from __future__ import annotations

import json
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.models import APIKey, APIKeyPermission
from apps.mcp_app.server import _call_get_workspace, _call_update_workspace
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.desktop import validate_desktop_dimension
from apps.runners.enums import RunnerStatus, WorkspaceStatus
from apps.runners.models import Runner, Workspace
from common.utils import generate_api_token, hash_token


def test_validate_desktop_dimension_accepts_even_in_range() -> None:
    """Even sizes inside the allowed range pass validation."""
    assert validate_desktop_dimension(1920, kind="width") == 1920
    assert validate_desktop_dimension(1080, kind="height") == 1080


def test_validate_desktop_dimension_rejects_odd_and_out_of_range() -> None:
    """Odd or out-of-range sizes raise ValueError."""
    with pytest.raises(ValueError, match="even"):
        validate_desktop_dimension(1921, kind="width")
    with pytest.raises(ValueError, match="between"):
        validate_desktop_dimension(200, kind="width")


def test_docker_desktop_block_disables_remote_resize() -> None:
    """Docker workspace images start Xvnc at a fixed size without remote resize."""
    from apps.runners.services import RunnerService

    block = RunnerService._desktop_session_dockerfile_block()
    assert "allow_resize: false" in block
    assert "AcceptSetDesktopSize" not in block
    assert "OPENCURIA_DESKTOP_GEOMETRY" in block


@pytest.fixture
def desktop_api(db):
    """Authenticated API client with workspace update permission."""
    user_model = get_user_model()
    user = user_model.objects.create_user(
        email=f"desktop-api-{uuid.uuid4().hex[:8]}@example.com",
        password="secret",
    )
    org = Organization.objects.create(
        name=f"Desktop API Org {uuid.uuid4().hex[:6]}",
        slug=f"desktop-api-org-{uuid.uuid4().hex[:10]}",
    )
    Membership.objects.create(user=user, organization=org, role=MembershipRole.ADMIN)
    token = generate_api_token()
    APIKey.objects.create(
        user=user,
        name="desktop-api-key",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=[
            APIKeyPermission.WORKSPACES_READ.value,
            APIKeyPermission.WORKSPACES_UPDATE.value,
        ],
    )
    runner = Runner.objects.create(
        name="desktop-runner",
        api_token_hash=hash_token("desktop-runner-token"),
        status=RunnerStatus.ONLINE,
        sid="desktop-sid",
        organization=org,
        available_runtimes=["docker"],
    )
    workspace = Workspace.objects.create(
        runner=runner,
        name="Desktop Workspace",
        status=WorkspaceStatus.RUNNING,
        created_by=user,
    )
    client = Client(
        HTTP_X_API_KEY=token,
        HTTP_X_ORGANIZATION_ID=str(org.id),
    )
    return {
        "client": client,
        "workspace": workspace,
        "user": user,
        "org": org,
    }


@pytest.mark.django_db
def test_patch_workspace_updates_desktop_geometry(desktop_api) -> None:
    """PATCH persists a new fixed desktop size without restarting Xvnc."""
    workspace = desktop_api["workspace"]
    response = desktop_api["client"].patch(
        f"/api/v1/workspaces/{workspace.id}/",
        data={"desktop_width": 1280, "desktop_height": 720},
        content_type="application/json",
    )
    assert response.status_code == 200
    body = response.json()
    assert body["desktop_width"] == 1280
    assert body["desktop_height"] == 720
    workspace.refresh_from_db()
    assert workspace.desktop_width == 1280
    assert workspace.desktop_height == 720


@pytest.mark.django_db
def test_patch_workspace_rejects_odd_desktop_width(desktop_api) -> None:
    """Invalid geometry is rejected before persistence."""
    workspace = desktop_api["workspace"]
    response = desktop_api["client"].patch(
        f"/api/v1/workspaces/{workspace.id}/",
        data={"desktop_width": 1281, "desktop_height": 720},
        content_type="application/json",
    )
    assert response.status_code == 422
    workspace.refresh_from_db()
    assert workspace.desktop_width == 1920


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_update_workspace_service_persists_geometry(desktop_api) -> None:
    """RunnerService stores desktop size for the next Xvnc start."""
    from unittest.mock import AsyncMock

    from apps.runners.services import RunnerService

    workspace = desktop_api["workspace"]
    service = RunnerService(sio_server=AsyncMock())
    updated = await service.update_workspace(
        workspace.id,
        desktop_width=1600,
        desktop_height=900,
    )
    assert updated.desktop_width == 1600
    assert updated.desktop_height == 900


@pytest.mark.django_db(transaction=True)
def test_mcp_get_and_update_workspace_desktop_size(desktop_api) -> None:
    """MCP get/update expose the fixed desktop framebuffer size."""
    from types import SimpleNamespace

    workspace = desktop_api["workspace"]
    api_key = SimpleNamespace(user=desktop_api["user"])
    org_id = desktop_api["org"].id

    listed = _call_get_workspace(
        api_key, org_id, {"workspace_id": str(workspace.id)}
    )
    payload = json.loads(listed[0].text)
    assert payload["desktop_width"] == 1920
    assert payload["desktop_height"] == 1080

    updated = _call_update_workspace(
        api_key,
        org_id,
        {
            "workspace_id": str(workspace.id),
            "desktop_width": 1280,
            "desktop_height": 720,
        },
    )
    body = json.loads(updated[0].text)
    assert body["desktop_width"] == 1280
    assert body["desktop_height"] == 720


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_start_desktop_payload_includes_geometry(desktop_api) -> None:
    """task:start_desktop sends the workspace framebuffer size to the runner."""
    from unittest.mock import AsyncMock

    from apps.runners.services import RunnerService

    workspace = desktop_api["workspace"]
    workspace.desktop_width = 1280
    workspace.desktop_height = 720
    workspace.save(update_fields=["desktop_width", "desktop_height", "updated_at"])

    service = RunnerService(sio_server=AsyncMock())
    service._emit_to_runner = AsyncMock()
    await service.start_desktop(workspace.id)
    event = service._emit_to_runner.await_args.args[1]
    payload = service._emit_to_runner.await_args.args[2]
    assert event == "task:start_desktop"
    assert payload["desktop_width"] == 1280
    assert payload["desktop_height"] == 720
