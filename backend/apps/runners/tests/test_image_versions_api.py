"""REST/MCP contracts for versioned images, recreate and retention policy."""

import json
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.models import APIKey, APIKeyPermission
from apps.mcp_app.server import (
    _call_list_captured_images,
    _call_recreate_workspace,
    _call_update_workspace_policy,
)
from apps.organizations.models import Membership, MembershipRole
from apps.runners.models import WorkspaceRecreateRequest
from apps.runners.sio_server import get_runner_service
from apps.runners.tests.test_image_versions import make_line, qemu_workspace
from common.utils import generate_api_token, hash_token

pytestmark = pytest.mark.django_db


def headers(user, organization, permissions):
    token = generate_api_token()
    APIKey.objects.create(
        user=user,
        name="versions-test",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=[permission.value for permission in permissions],
    )
    return {"HTTP_X_API_KEY": token, "HTTP_X_ORGANIZATION_ID": str(organization.id)}


@pytest.fixture
def member(user, organization):
    Membership.objects.create(
        user=user, organization=organization, role=MembershipRole.MEMBER
    )
    return user


def test_captured_images_list_versions_and_latest(member, runner, organization):
    line, (v1, v2) = make_line(runner, member, ("ready", "ready"))
    qemu_workspace(runner, member, v1)
    response = Client().get(
        "/api/v1/captured-images/",
        **headers(member, organization, [APIKeyPermission.IMAGES_READ]),
    )
    assert response.status_code == 200, response.content
    [image] = response.json()
    assert image["id"] == str(line.id) and image["latest_version"] == 2
    assert image["total_size_bytes"] == 300 and image["workspace_count"] == 1
    versions = {v["version"]: v for v in image["versions"]}
    assert versions[2]["is_latest"] and versions[2]["retention"] == "latest"
    assert versions[1]["retention"] == "kept"
    assert versions[1]["workspaces"][0]["name"] == "Versioned"


def test_captured_images_are_owner_scoped(member, runner, organization):
    line, _ = make_line(runner, member)
    other = get_user_model().objects.create_user(email="x@example.com", password="x")
    Membership.objects.create(
        user=other, organization=organization, role=MembershipRole.MEMBER
    )
    auth = headers(other, organization, [APIKeyPermission.IMAGES_READ])
    assert Client().get("/api/v1/captured-images/", **auth).json() == []
    response = Client().get(f"/api/v1/captured-images/{line.id}/", **auth)
    assert response.status_code == 404


def test_rename_captured_image(member, runner, organization):
    line, _ = make_line(runner, member)
    response = Client().patch(
        f"/api/v1/captured-images/{line.id}/",
        data=json.dumps({"name": "Renamed"}),
        content_type="application/json",
        **headers(member, organization, [APIKeyPermission.IMAGES_CREATE]),
    )
    assert response.status_code == 200 and response.json()["name"] == "Renamed"


def test_new_workspace_from_captured_image_uses_latest(
    member, runner, organization, monkeypatch
):
    line, (_, v2) = make_line(runner, member, ("ready", "ready"))
    workspace = qemu_workspace(runner, member, v2)
    calls = []

    async def create(**kwargs):
        calls.append(kwargs["image_artifact_id"])
        return workspace, SimpleNamespace(id=workspace.id)

    monkeypatch.setattr("apps.runners.api._workspace_plugin_ids", lambda workspace: [])

    monkeypatch.setattr(
        get_runner_service(), "create_workspace_from_image_artifact", create
    )
    response = Client().post(
        f"/api/v1/captured-images/{line.id}/workspaces/",
        data=json.dumps({"name": "From latest"}),
        content_type="application/json",
        **headers(member, organization, [APIKeyPermission.IMAGES_CLONE]),
    )
    assert response.status_code == 202, response.content
    assert calls == [v2.id]


def test_workspace_out_exposes_version_and_update(member, runner, organization):
    _, (v1, v2) = make_line(runner, member, ("ready", "ready"))
    ws = qemu_workspace(runner, member, v1)
    response = Client().get(
        f"/api/v1/workspaces/{ws.id}/",
        **headers(member, organization, [APIKeyPermission.WORKSPACES_READ]),
    )
    assert response.status_code == 200, response.content
    base = response.json()["base_image"]
    assert base["version"] == 1 and base["line_kind"] == "captured"
    assert base["latest_id"] == str(v2.id) and base["update_available"] is True


def test_recreate_requires_owner_and_delete_permission(member, runner, organization):
    _, (v1,) = make_line(runner, member)
    ws = qemu_workspace(runner, member, v1)
    url = f"/api/v1/workspaces/{ws.id}/recreate/"
    body = {
        "data": json.dumps({"image_id": str(v1.id)}),
        "content_type": "application/json",
    }
    denied = Client().post(
        url,
        **body,
        **headers(member, organization, [APIKeyPermission.WORKSPACES_UPDATE]),
    )
    assert denied.status_code == 403
    other = get_user_model().objects.create_user(email="y@example.com", password="x")
    Membership.objects.create(
        user=other, organization=organization, role=MembershipRole.MEMBER
    )
    hidden = Client().post(
        url,
        **body,
        **headers(other, organization, [APIKeyPermission.WORKSPACES_DELETE]),
    )
    assert hidden.status_code == 404
    assert not WorkspaceRecreateRequest.objects.exists()
    accepted = Client().post(
        url,
        **body,
        **headers(member, organization, [APIKeyPermission.WORKSPACES_DELETE]),
    )
    assert accepted.status_code == 202, accepted.content
    assert accepted.json()["active_operation"] == "resetting"
    conflict = Client().post(
        url,
        **body,
        **headers(member, organization, [APIKeyPermission.WORKSPACES_DELETE]),
    )
    assert conflict.status_code == 409


def test_recreate_refuses_offline_runner(member, runner, organization):
    _, (v1,) = make_line(runner, member)
    ws = qemu_workspace(runner, member, v1)
    runner.status = "offline"
    runner.save(update_fields=["status"])
    response = Client().post(
        f"/api/v1/workspaces/{ws.id}/recreate/",
        data=json.dumps({"image_id": str(v1.id)}),
        content_type="application/json",
        **headers(member, organization, [APIKeyPermission.WORKSPACES_DELETE]),
    )
    assert response.status_code == 409 and response.json()["code"] == "runner_offline"
    assert not WorkspaceRecreateRequest.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_mcp_recreate_and_list_captured_images(member, runner, organization):
    line, (v1, v2) = make_line(runner, member, ("ready", "ready"))
    ws = qemu_workspace(runner, member, v1)
    key = SimpleNamespace(user=member)
    [listed] = json.loads(_call_list_captured_images(key, organization.id, {})[0].text)
    assert listed["id"] == str(line.id) and listed["latest_id"] == str(v2.id)
    payload = json.loads(
        _call_recreate_workspace(
            key,
            organization.id,
            {"workspace_id": str(ws.id), "image_id": str(v2.id)},
        )[0].text
    )
    assert payload["active_operation"] == "updating"
    assert WorkspaceRecreateRequest.objects.get(workspace=ws).reason == "update"


def test_policy_patch_is_partial_and_validated(user, organization):
    Membership.objects.create(
        user=user, organization=organization, role=MembershipRole.ADMIN
    )
    organization.workspace_auto_stop_timeout_minutes = 30
    organization.save()
    auth = headers(user, organization, [APIKeyPermission.ORGANIZATIONS_WRITE])
    url = f"/api/v1/organizations/{organization.id}/workspace-policy/"
    response = Client().patch(
        url,
        data=json.dumps({"image_versions_to_keep": 4}),
        content_type="application/json",
        **auth,
    )
    assert response.status_code == 200, response.content
    organization.refresh_from_db()
    assert organization.image_versions_to_keep == 4
    assert organization.workspace_auto_stop_timeout_minutes == 30
    invalid = Client().patch(
        url,
        data=json.dumps({"image_versions_to_keep": 0}),
        content_type="application/json",
        **auth,
    )
    assert invalid.status_code == 400


def test_policy_update_requires_admin(member, organization):
    result = _call_update_workspace_policy(
        SimpleNamespace(user=member), organization.id, {"image_versions_to_keep": 5}
    )
    assert result[0].text.startswith("Error:")
    organization.refresh_from_db()
    assert organization.image_versions_to_keep == 2
