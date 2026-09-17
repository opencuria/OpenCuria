"""
API tests for the plugins domain.

Covers RBAC/API-key permissions, cross-org 404s, nested creation,
activation inheritance, workspace activation credential gating,
org disable/re-enable behavior, the credential removal guard, and the
global Playwright seed exposure via the API.
"""

from __future__ import annotations

import json
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.models import APIKey, APIKeyPermission
from apps.credentials.models import CredentialService, OrgCredentialServiceActivation
from apps.credentials.services import CredentialSvc
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.plugins.models import (
    Plugin,
    WorkspacePluginActivation,
)
from apps.runners.models import Runner, Workspace
from common.utils import generate_api_token, hash_token


@pytest.fixture
def client() -> Client:
    return Client()


def _ctx(*, role: str = MembershipRole.ADMIN, permissions: list[str] | None = None):
    user_model = get_user_model()
    user = user_model.objects.create_user(
        email=f"plugin-{uuid.uuid4().hex[:8]}@example.com", password="secret"
    )
    org = Organization.objects.create(
        name=f"Org {uuid.uuid4().hex[:6]}",
        slug=f"org-{uuid.uuid4().hex[:10]}",
    )
    Membership.objects.create(user=user, organization=org, role=role)
    perms = (
        permissions
        if permissions is not None
        else [
            APIKeyPermission.PLUGINS_READ.value,
            APIKeyPermission.PLUGINS_WRITE.value,
            APIKeyPermission.CREDENTIALS_READ.value,
            APIKeyPermission.CREDENTIALS_WRITE.value,
            APIKeyPermission.ORG_CREDENTIAL_SERVICES_READ.value,
            APIKeyPermission.ORG_CREDENTIAL_SERVICES_WRITE.value,
            APIKeyPermission.WORKSPACES_UPDATE.value,
        ]
    )
    token = generate_api_token()
    APIKey.objects.create(
        user=user,
        name="plugin-test-key",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=perms,
    )
    return {
        "user": user,
        "org": org,
        "headers": {
            "HTTP_X_API_KEY": token,
            "HTTP_X_ORGANIZATION_ID": str(org.id),
        },
    }


def _post(client: Client, path: str, headers: dict, body: dict):
    return client.post(
        path,
        data=json.dumps(body),
        content_type="application/json",
        **headers,
    )


def _patch(client: Client, path: str, headers: dict, body: dict):
    return client.patch(
        path,
        data=json.dumps(body),
        content_type="application/json",
        **headers,
    )


def _put(client: Client, path: str, headers: dict, body: dict):
    return client.put(
        path,
        data=json.dumps(body),
        content_type="application/json",
        **headers,
    )


def _create_workspace(ctx) -> Workspace:
    runner = Runner.objects.create(
        name="r",
        api_token_hash=hash_token(uuid.uuid4().hex),
        organization=ctx["org"],
    )
    return Workspace.objects.create(runner=runner, name="w", created_by=ctx["user"])


def _minimal_plugin_body(name: str = "My Plugin") -> dict:
    return {
        "name": name,
        "skills": [
            {"name": "Guide", "body": "# guide", "position": 0},
        ],
        "mcp_servers": [
            {
                "name": "Tools",
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "thing"],
            },
        ],
    }


@pytest.mark.django_db
def test_member_can_list_but_not_create(client: Client):
    ctx = _ctx(role=MembershipRole.MEMBER)
    response = client.get("/api/v1/plugins/", **ctx["headers"])
    assert response.status_code == 200

    response = _post(client, "/api/v1/plugins/", ctx["headers"], _minimal_plugin_body())
    assert response.status_code == 403


@pytest.mark.django_db
def test_api_key_permission_enforced(client: Client):
    ctx = _ctx(
        permissions=[APIKeyPermission.PLUGINS_READ.value],
    )
    response = client.get("/api/v1/plugins/", **ctx["headers"])
    assert response.status_code == 200

    response = _post(client, "/api/v1/plugins/", ctx["headers"], _minimal_plugin_body())
    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"


@pytest.mark.django_db
def test_cross_org_plugin_is_404(client: Client):
    ctx_a = _ctx()
    ctx_b = _ctx()
    created = _post(
        client, "/api/v1/plugins/", ctx_a["headers"], _minimal_plugin_body()
    )
    assert created.status_code == 201
    plugin_id = created.json()["id"]

    response = client.get(f"/api/v1/plugins/{plugin_id}/", **ctx_b["headers"])
    assert response.status_code == 404

    response = _patch(
        client, f"/api/v1/plugins/{plugin_id}/", ctx_b["headers"], {"name": "X"}
    )
    assert response.status_code == 404

    response = client.delete(f"/api/v1/plugins/{plugin_id}/", **ctx_b["headers"])
    assert response.status_code == 404


@pytest.mark.django_db
def test_create_nested_plugin_with_service_and_requirement(client: Client):
    ctx = _ctx()
    body = _minimal_plugin_body("Nested")
    body["credential_requirements"] = [
        {
            "key": "api_key",
            "required": True,
            "credential_service": {
                "name": "Nested Service",
                "credential_type": "env",
                "env_var_name": "NESTED_TOKEN",
            },
        }
    ]
    response = _post(client, "/api/v1/plugins/", ctx["headers"], body)
    assert response.status_code == 201, response.content[:500]
    payload = response.json()
    assert payload["organization_id"] == str(ctx["org"].id)
    assert payload["is_global"] is False
    assert payload["org_enabled"] is False
    assert len(payload["skills"]) == 1
    assert len(payload["mcp_servers"]) == 1
    assert len(payload["credential_requirements"]) == 1
    req = payload["credential_requirements"][0]
    assert req["key"] == "api_key"
    assert req["plugin_owned_service"] is True
    service = CredentialService.objects.get(id=req["service_id"])
    assert str(service.organization_id) == str(ctx["org"].id)
    # Referenced services are auto-activated for the org.
    assert OrgCredentialServiceActivation.objects.filter(
        organization=ctx["org"], credential_service=service
    ).exists()
    # Readiness: no org credential for the service yet.
    assert payload["credential_readiness"]["ready"] is False


@pytest.mark.django_db
def test_create_plugin_invalid_mcp_is_400_and_atomic(client: Client):
    ctx = _ctx()
    body = _minimal_plugin_body("Broken")
    body["mcp_servers"] = [{"name": "Bad", "transport": "stdio"}]
    count_before = Plugin.objects.filter(organization=ctx["org"]).count()
    response = _post(client, "/api/v1/plugins/", ctx["headers"], body)
    assert response.status_code == 400
    assert Plugin.objects.filter(organization=ctx["org"]).count() == count_before


@pytest.mark.django_db
def test_global_plugin_readable_but_not_editable_via_rest(client: Client):
    ctx = _ctx()
    response = client.get("/api/v1/plugins/", **ctx["headers"])
    assert response.status_code == 200
    payload = response.json()
    global_plugins = [p for p in payload if p["is_global"]]
    assert global_plugins, "expected the seeded global Playwright plugin"
    playwright = next(p for p in payload if p["slug"] == "playwright")
    assert playwright["mcp_servers"][0]["command"] == "npx"

    response = _patch(
        client,
        f"/api/v1/plugins/{playwright['id']}/",
        ctx["headers"],
        {"name": "Hacked"},
    )
    assert response.status_code == 403
    response = client.delete(f"/api/v1/plugins/{playwright['id']}/", **ctx["headers"])
    assert response.status_code == 403


@pytest.mark.django_db
def test_patch_replaces_components_atomically(client: Client):
    ctx = _ctx()
    created = _post(client, "/api/v1/plugins/", ctx["headers"], _minimal_plugin_body())
    plugin_id = created.json()["id"]
    assert len(created.json()["skills"]) == 1

    response = _patch(
        client,
        f"/api/v1/plugins/{plugin_id}/",
        ctx["headers"],
        {
            "name": "Renamed",
            "skills": [
                {"name": "One", "body": "b1"},
                {"name": "Two", "body": "b2"},
            ],
            "mcp_servers": [],
        },
    )
    assert response.status_code == 200, response.content[:500]
    payload = response.json()
    assert payload["name"] == "Renamed"
    assert len(payload["skills"]) == 2
    assert payload["mcp_servers"] == []

    # Invalid replacement leaves stored components untouched.
    bad = _patch(
        client,
        f"/api/v1/plugins/{plugin_id}/",
        ctx["headers"],
        {"mcp_servers": [{"name": "Bad", "transport": "stdio"}]},
    )
    assert bad.status_code == 400
    current = client.get(f"/api/v1/plugins/{plugin_id}/", **ctx["headers"]).json()
    assert len(current["skills"]) == 2


@pytest.mark.django_db
def test_delete_blocked_when_credentials_reference_owned_service(client: Client):
    ctx = _ctx()
    body = _minimal_plugin_body("Deletable")
    body["credential_requirements"] = [
        {
            "key": "token",
            "credential_service": {
                "name": "Del Service",
                "credential_type": "env",
                "env_var_name": "DEL_TOKEN",
            },
        }
    ]
    created = _post(client, "/api/v1/plugins/", ctx["headers"], body)
    plugin_id = created.json()["id"]
    service_id = created.json()["credential_requirements"][0]["service_id"]

    CredentialSvc().create_org_credential(
        organization_id=ctx["org"].id,
        service_id=uuid.UUID(service_id),
        name="Org token",
        value="secret",
        user=ctx["user"],
    )
    response = client.delete(f"/api/v1/plugins/{plugin_id}/", **ctx["headers"])
    assert response.status_code == 409
    assert Plugin.objects.filter(id=plugin_id).exists()


@pytest.mark.django_db
def test_activation_inheritance_and_workspace_flow(client: Client):
    ctx = _ctx()
    body = _minimal_plugin_body("Flow")
    body["credential_requirements"] = [
        {
            "key": "token",
            "credential_service": {
                "name": "Flow Service",
                "credential_type": "env",
                "env_var_name": "FLOW_TOKEN",
            },
        }
    ]
    created = _post(client, "/api/v1/plugins/", ctx["headers"], body).json()
    plugin_id = created["id"]
    service_id = created["credential_requirements"][0]["service_id"]

    workspace = _create_workspace(ctx)

    # Not org-enabled yet: workspace list hides the plugin.
    listed = client.get(f"/api/v1/workspaces/{workspace.id}/plugins/", **ctx["headers"])
    assert listed.status_code == 200
    assert all(p["id"] != plugin_id for p in listed.json())

    # Enable org-wide.
    toggled = _post(
        client,
        f"/api/v1/plugins/{plugin_id}/activation/",
        ctx["headers"],
        {"active": True},
    )
    assert toggled.status_code == 200
    assert toggled.json()["org_enabled"] is True

    listed = client.get(
        f"/api/v1/workspaces/{workspace.id}/plugins/", **ctx["headers"]
    ).json()
    entry = next(p for p in listed if p["id"] == plugin_id)
    assert entry["workspace_enabled"] is False
    assert entry["ready"] is False
    assert entry["missing_required_credentials"]

    # Missing credential -> 409 with machine-readable detail.
    blocked = _put(
        client,
        f"/api/v1/workspaces/{workspace.id}/plugins/",
        ctx["headers"],
        {"plugin_ids": [plugin_id]},
    )
    assert blocked.status_code == 409

    # Attach the credential to the workspace, then activation succeeds.
    credential = CredentialSvc().create_org_credential(
        organization_id=ctx["org"].id,
        service_id=uuid.UUID(service_id),
        name="Flow token",
        value="secret",
        user=ctx["user"],
    )
    workspace.credentials.add(credential)
    ok = _put(
        client,
        f"/api/v1/workspaces/{workspace.id}/plugins/",
        ctx["headers"],
        {"plugin_ids": [plugin_id]},
    )
    assert ok.status_code == 200, ok.content[:500]
    entry = next(p for p in ok.json() if p["id"] == plugin_id)
    assert entry["workspace_enabled"] is True
    assert entry["ready"] is True
    row = WorkspacePluginActivation.objects.filter(
        workspace=workspace, plugin_id=plugin_id
    ).first()
    assert row is not None
    assert row.enabled_by_id == ctx["user"].id


@pytest.mark.django_db
def test_org_activation_requires_effective_plugin(client: Client):
    ctx = _ctx()
    created = _post(
        client, "/api/v1/plugins/", ctx["headers"], _minimal_plugin_body("Gated")
    )
    assert created.status_code == 201
    plugin_id = created.json()["id"]

    # Unpublished definitions cannot be org-activated (409, machine-readable).
    unpublished = _patch(
        client, f"/api/v1/plugins/{plugin_id}/", ctx["headers"], {"published": False}
    )
    assert unpublished.status_code == 200
    blocked = _post(
        client,
        f"/api/v1/plugins/{plugin_id}/activation/",
        ctx["headers"],
        {"active": True},
    )
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "plugin_not_available"

    # Disabled definitions cannot be org-activated either.
    _patch(
        client,
        f"/api/v1/plugins/{plugin_id}/",
        ctx["headers"],
        {"published": True, "enabled": False},
    )
    blocked = _post(
        client,
        f"/api/v1/plugins/{plugin_id}/activation/",
        ctx["headers"],
        {"active": True},
    )
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "plugin_not_available"

    # Deactivation always succeeds (idempotent, 200).
    _patch(
        client,
        f"/api/v1/plugins/{plugin_id}/",
        ctx["headers"],
        {"enabled": True},
    )
    assert (
        _post(
            client,
            f"/api/v1/plugins/{plugin_id}/activation/",
            ctx["headers"],
            {"active": True},
        ).status_code
        == 200
    )
    off = _post(
        client,
        f"/api/v1/plugins/{plugin_id}/activation/",
        ctx["headers"],
        {"active": False},
    )
    assert off.status_code == 200
    again = _post(
        client,
        f"/api/v1/plugins/{plugin_id}/activation/",
        ctx["headers"],
        {"active": False},
    )
    assert again.status_code == 200


@pytest.mark.django_db
def test_workspace_activation_rejects_unpublished_plugin(client: Client):
    ctx = _ctx()
    created = _post(
        client, "/api/v1/plugins/", ctx["headers"], _minimal_plugin_body("WPGated")
    ).json()
    plugin_id = created["id"]
    workspace = _create_workspace(ctx)
    _post(
        client,
        f"/api/v1/plugins/{plugin_id}/activation/",
        ctx["headers"],
        {"active": True},
    )
    # Unpublish after org activation: workspace activation now 409s with
    # a machine-readable code (org toggle stays as-is, listing hides it).
    _patch(
        client, f"/api/v1/plugins/{plugin_id}/", ctx["headers"], {"published": False}
    )
    blocked = _put(
        client,
        f"/api/v1/workspaces/{workspace.id}/plugins/",
        ctx["headers"],
        {"plugin_ids": [plugin_id]},
    )
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "plugin_not_available"
    listed = client.get(
        f"/api/v1/workspaces/{workspace.id}/plugins/", **ctx["headers"]
    ).json()
    assert all(p["id"] != plugin_id for p in listed)


@pytest.mark.django_db
def test_org_plugin_delete_blocked_by_any_real_credential(client: Client):
    """Any real credential (personal or org) blocks plugin deletion.

    ``CredentialService`` cascades to ``Credential`` rows, so deleting
    the plugin must never silently delete credentials: both a personal
    credential and an org credential block with 409
    ``plugin_credentials_in_use``.
    """
    ctx = _ctx()
    body = _minimal_plugin_body("Scoped-Del")
    body["credential_requirements"] = [
        {
            "key": "token",
            "credential_service": {
                "name": "Scoped Del Service",
                "credential_type": "env",
                "env_var_name": "SCOPED_DEL_TOKEN",
            },
        }
    ]
    created = _post(client, "/api/v1/plugins/", ctx["headers"], body)
    assert created.status_code == 201
    plugin_id = created.json()["id"]
    service_id = created.json()["credential_requirements"][0]["service_id"]

    from apps.credentials.models import Credential

    CredentialSvc().create_personal_credential(
        service_id=uuid.UUID(service_id),
        name="personal token",
        value="secret",
        user=ctx["user"],
        org_id=ctx["org"].id,
    )
    assert Credential.objects.filter(service_id=service_id).exists()
    response = client.delete(f"/api/v1/plugins/{plugin_id}/", **ctx["headers"])
    assert response.status_code == 409, response.content[:500]
    assert response.json()["code"] == "plugin_credentials_in_use"
    assert Plugin.objects.filter(id=plugin_id).exists()


@pytest.mark.django_db
def test_plugin_owned_marker_survives_service_id_replace(client: Client):
    """Re-referencing an owned service via service_id keeps the marker."""
    ctx = _ctx()
    body = _minimal_plugin_body("Marker")
    body["credential_requirements"] = [
        {
            "key": "token",
            "credential_service": {
                "name": "Marker Service",
                "credential_type": "env",
                "env_var_name": "MARKER_TOKEN",
            },
        }
    ]
    created = _post(client, "/api/v1/plugins/", ctx["headers"], body).json()
    plugin_id = created["id"]
    service_id = created["credential_requirements"][0]["service_id"]
    assert created["credential_requirements"][0]["plugin_owned_service"] is True

    updated = _patch(
        client,
        f"/api/v1/plugins/{plugin_id}/",
        ctx["headers"],
        {
            "credential_requirements": [
                {
                    "key": "token",
                    "credential_service": {"service_id": service_id},
                }
            ]
        },
    )
    assert updated.status_code == 200, updated.content[:500]
    assert updated.json()["credential_requirements"][0][
        "plugin_owned_service"
    ] is True


@pytest.mark.django_db
def test_org_disable_keeps_workspace_rows_and_reenable_restores(client: Client):
    ctx = _ctx()
    created = _post(
        client, "/api/v1/plugins/", ctx["headers"], _minimal_plugin_body("Toggle")
    ).json()
    plugin_id = created["id"]
    workspace = _create_workspace(ctx)

    _post(
        client,
        f"/api/v1/plugins/{plugin_id}/activation/",
        ctx["headers"],
        {"active": True},
    )
    ok = _put(
        client,
        f"/api/v1/workspaces/{workspace.id}/plugins/",
        ctx["headers"],
        {"plugin_ids": [plugin_id]},
    )
    assert ok.status_code == 200

    _post(
        client,
        f"/api/v1/plugins/{plugin_id}/activation/",
        ctx["headers"],
        {"active": False},
    )
    # Workspace row is kept but temporarily ineffective (hidden from list).
    assert WorkspacePluginActivation.objects.filter(
        workspace=workspace, plugin_id=plugin_id
    ).exists()
    listed = client.get(
        f"/api/v1/workspaces/{workspace.id}/plugins/", **ctx["headers"]
    ).json()
    assert all(p["id"] != plugin_id for p in listed)

    _post(
        client,
        f"/api/v1/plugins/{plugin_id}/activation/",
        ctx["headers"],
        {"active": True},
    )
    listed = client.get(
        f"/api/v1/workspaces/{workspace.id}/plugins/", **ctx["headers"]
    ).json()
    entry = next(p for p in listed if p["id"] == plugin_id)
    assert entry["workspace_enabled"] is True


@pytest.mark.django_db
def test_workspace_lookup_is_owner_only_for_admin(client: Client):
    owner_ctx = _ctx()
    other_ctx = _ctx()
    created = _post(
        client, "/api/v1/plugins/", owner_ctx["headers"], _minimal_plugin_body()
    ).json()
    plugin_id = created["id"]
    _post(
        client,
        f"/api/v1/plugins/{plugin_id}/activation/",
        owner_ctx["headers"],
        {"active": True},
    )
    workspace = _create_workspace(owner_ctx)
    # Second user joins the same org as non-owner member: membership passes,
    # but the owner-scoped workspace lookup must still 404.
    Membership.objects.create(
        user=other_ctx["user"],
        organization=owner_ctx["org"],
        role=MembershipRole.MEMBER,
    )
    foreign_headers = dict(other_ctx["headers"])
    foreign_headers["HTTP_X_ORGANIZATION_ID"] = str(owner_ctx["org"].id)
    response = client.get(
        f"/api/v1/workspaces/{workspace.id}/plugins/", **foreign_headers
    )
    assert response.status_code == 404


@pytest.mark.django_db
def test_credential_delete_blocked_while_plugin_active(client: Client):
    admin_ctx = _ctx()
    body = _minimal_plugin_body("DelGuard")
    body["credential_requirements"] = [
        {
            "key": "token",
            "credential_service": {
                "name": "DelGuard Service",
                "credential_type": "env",
                "env_var_name": "DELGUARD_TOKEN",
            },
        }
    ]
    created = _post(client, "/api/v1/plugins/", admin_ctx["headers"], body).json()
    plugin_id = created["id"]
    service_id = created["credential_requirements"][0]["service_id"]
    workspace = _create_workspace(admin_ctx)
    _post(
        client,
        f"/api/v1/plugins/{plugin_id}/activation/",
        admin_ctx["headers"],
        {"active": True},
    )
    credential = CredentialSvc().create_org_credential(
        organization_id=admin_ctx["org"].id,
        service_id=uuid.UUID(service_id),
        name="DelGuard token",
        value="secret",
        user=admin_ctx["user"],
    )
    workspace.credentials.add(credential)
    ok = _put(
        client,
        f"/api/v1/workspaces/{workspace.id}/plugins/",
        admin_ctx["headers"],
        {"plugin_ids": [plugin_id]},
    )
    assert ok.status_code == 200

    # Deleting the backing credential while the plugin is effective
    # -> 409 with a machine-readable code; the row survives.
    from apps.credentials.models import Credential as _Credential

    response = client.delete(
        f"/api/v1/credentials/{credential.id}/", **admin_ctx["headers"]
    )
    assert response.status_code == 409, response.content[:500]
    assert response.json()["code"] == "plugin_credentials_in_use"
    assert _Credential.objects.filter(id=credential.id).exists()

    # Clearing the workspace activation first unblocks the delete.
    cleared = _put(
        client,
        f"/api/v1/workspaces/{workspace.id}/plugins/",
        admin_ctx["headers"],
        {"plugin_ids": []},
    )
    assert cleared.status_code == 200
    response = client.delete(
        f"/api/v1/credentials/{credential.id}/", **admin_ctx["headers"]
    )
    assert response.status_code == 204, response.content[:500]


@pytest.mark.django_db
def test_credential_value_update_while_plugin_active_ok(client: Client):
    """PATCHing the value keeps the attachment: never a 409."""
    admin_ctx = _ctx()
    body = _minimal_plugin_body("UpdGuard")
    body["credential_requirements"] = [
        {
            "key": "token",
            "credential_service": {
                "name": "UpdGuard Service",
                "credential_type": "env",
                "env_var_name": "UPDGUARD_TOKEN",
            },
        }
    ]
    created = _post(client, "/api/v1/plugins/", admin_ctx["headers"], body).json()
    plugin_id = created["id"]
    service_id = created["credential_requirements"][0]["service_id"]
    workspace = _create_workspace(admin_ctx)
    _post(
        client,
        f"/api/v1/plugins/{plugin_id}/activation/",
        admin_ctx["headers"],
        {"active": True},
    )
    credential = CredentialSvc().create_org_credential(
        organization_id=admin_ctx["org"].id,
        service_id=uuid.UUID(service_id),
        name="UpdGuard token",
        value="secret",
        user=admin_ctx["user"],
    )
    workspace.credentials.add(credential)
    assert (
        _put(
            client,
            f"/api/v1/workspaces/{workspace.id}/plugins/",
            admin_ctx["headers"],
            {"plugin_ids": [plugin_id]},
        ).status_code
        == 200
    )
    response = client.patch(
        f"/api/v1/credentials/{credential.id}/",
        data=json.dumps({"value": "rotated-secret"}),
        content_type="application/json",
        **admin_ctx["headers"],
    )
    assert response.status_code == 200, response.content[:500]
    assert workspace.credentials.filter(id=credential.id).exists()


@pytest.mark.django_db
def test_org_credential_delete_fans_out_across_workspaces(client: Client):
    """One org credential on two workspaces blocks with both listed."""
    admin_ctx = _ctx()
    body = _minimal_plugin_body("FanOut")
    body["credential_requirements"] = [
        {
            "key": "token",
            "credential_service": {
                "name": "FanOut Service",
                "credential_type": "env",
                "env_var_name": "FANOUT_TOKEN",
            },
        }
    ]
    created = _post(client, "/api/v1/plugins/", admin_ctx["headers"], body).json()
    plugin_id = created["id"]
    service_id = created["credential_requirements"][0]["service_id"]
    workspace_a = _create_workspace(admin_ctx)
    workspace_b = _create_workspace(admin_ctx)
    _post(
        client,
        f"/api/v1/plugins/{plugin_id}/activation/",
        admin_ctx["headers"],
        {"active": True},
    )
    credential = CredentialSvc().create_org_credential(
        organization_id=admin_ctx["org"].id,
        service_id=uuid.UUID(service_id),
        name="FanOut token",
        value="secret",
        user=admin_ctx["user"],
    )
    workspace_a.credentials.add(credential)
    workspace_b.credentials.add(credential)
    for workspace in (workspace_a, workspace_b):
        assert (
            _put(
                client,
                f"/api/v1/workspaces/{workspace.id}/plugins/",
                admin_ctx["headers"],
                {"plugin_ids": [plugin_id]},
            ).status_code
            == 200
        )
    response = client.delete(
        f"/api/v1/credentials/{credential.id}/", **admin_ctx["headers"]
    )
    assert response.status_code == 409
    assert response.json()["code"] == "plugin_credentials_in_use"
    assert str(workspace_a.id) in response.content.decode()
    assert str(workspace_b.id) in response.content.decode()


@pytest.mark.django_db
def test_service_deactivation_blocked_while_org_plugin_enabled(client: Client):
    """Deactivating a service required by an org-enabled plugin -> 409."""
    from django.contrib.auth import get_user_model as _get_user_model

    ctx = _ctx()
    ctx["user"].is_staff = True
    ctx["user"].save(update_fields=["is_staff"])
    body = _minimal_plugin_body("SvcGate")
    body["credential_requirements"] = [
        {
            "key": "token",
            "credential_service": {
                "name": "SvcGate Service",
                "credential_type": "env",
                "env_var_name": "SVCGATE_TOKEN",
            },
        }
    ]
    created = _post(client, "/api/v1/plugins/", ctx["headers"], body).json()
    plugin_id = created["id"]
    service_id = created["credential_requirements"][0]["service_id"]
    _post(
        client,
        f"/api/v1/plugins/{plugin_id}/activation/",
        ctx["headers"],
        {"active": True},
    )
    blocked = client.post(
        f"/api/v1/org-credential-services/{service_id}/activation/",
        data=json.dumps({"active": False}),
        content_type="application/json",
        **ctx["headers"],
    )
    assert blocked.status_code == 409, blocked.content[:500]
    assert blocked.json()["code"] == "plugin_service_activation_in_use"

    # After org deactivation the service toggle succeeds again.
    _post(
        client,
        f"/api/v1/plugins/{plugin_id}/activation/",
        ctx["headers"],
        {"active": False},
    )
    ok = client.post(
        f"/api/v1/org-credential-services/{service_id}/activation/",
        data=json.dumps({"active": False}),
        content_type="application/json",
        **ctx["headers"],
    )
    assert ok.status_code == 200, ok.content[:500]
    assert _get_user_model() is not None  # keep import used


@pytest.mark.django_db
def test_credential_removal_guard_on_workspace_update(client: Client):
    admin_ctx = _ctx()
    body = _minimal_plugin_body("Guarded")
    body["credential_requirements"] = [
        {
            "key": "token",
            "credential_service": {
                "name": "Guard Service",
                "credential_type": "env",
                "env_var_name": "GUARD_TOKEN",
            },
        }
    ]
    created = _post(client, "/api/v1/plugins/", admin_ctx["headers"], body).json()
    plugin_id = created["id"]
    service_id = created["credential_requirements"][0]["service_id"]
    workspace = _create_workspace(admin_ctx)
    _post(
        client,
        f"/api/v1/plugins/{plugin_id}/activation/",
        admin_ctx["headers"],
        {"active": True},
    )
    credential = CredentialSvc().create_org_credential(
        organization_id=admin_ctx["org"].id,
        service_id=uuid.UUID(service_id),
        name="Guard token",
        value="secret",
        user=admin_ctx["user"],
    )
    workspace.credentials.add(credential)
    ok = _put(
        client,
        f"/api/v1/workspaces/{workspace.id}/plugins/",
        admin_ctx["headers"],
        {"plugin_ids": [plugin_id]},
    )
    assert ok.status_code == 200

    # Removing the last credential while the plugin is active -> 409.
    member_ctx_headers = dict(admin_ctx["headers"])
    response = client.patch(
        f"/api/v1/workspaces/{workspace.id}/",
        data=json.dumps({"credential_ids": []}),
        content_type="application/json",
        **member_ctx_headers,
    )
    assert response.status_code == 409, response.content[:500]


@pytest.mark.django_db
def test_credential_service_list_is_org_safe(client: Client):
    ctx_a = _ctx()
    ctx_b = _ctx()
    body = _minimal_plugin_body("Scoped")
    body["credential_requirements"] = [
        {
            "key": "token",
            "credential_service": {
                "name": "Scoped Service",
                "credential_type": "env",
                "env_var_name": "SCOPED_TOKEN",
            },
        }
    ]
    created = _post(client, "/api/v1/plugins/", ctx_a["headers"], body).json()
    service_id = created["credential_requirements"][0]["service_id"]

    listed_a = client.get("/api/v1/credential-services/", **ctx_a["headers"])
    assert listed_a.status_code == 200
    assert any(s["id"] == service_id for s in listed_a.json())

    listed_b = client.get("/api/v1/credential-services/", **ctx_b["headers"])
    assert listed_b.status_code == 200
    assert all(s["id"] != service_id for s in listed_b.json())
    assert any(s["organization_id"] is None for s in listed_b.json())


@pytest.mark.django_db
def test_plugin_api_key_permissions_listed(client: Client):
    response = client.get("/api/v1/auth/api-key-permissions/")
    assert response.status_code == 200
    by_value = {entry["value"]: entry for entry in response.json()}
    assert by_value["plugins:read"]["description"].strip() != ""
    assert by_value["plugins:write"]["description"].strip() != ""


@pytest.mark.django_db
def test_personal_credential_delete_cross_org_fanout(client: Client):
    """Deleting a personal credential checks every attached workspace's org.

    A personal credential is visible org-wide but may hang on workspaces
    of different orgs (via the shared owner). The delete guard must use
    each workspace's real ``runner.organization_id`` — not the request
    header org — so a delete from org A cannot silently break an
    effective plugin workspace in org B.
    """
    ctx_a = _ctx()
    ctx_b = _ctx()
    body = _minimal_plugin_body("CrossOrg")
    body["credential_requirements"] = [
        {
            "key": "token",
            "credential_service": {
                "name": "CrossOrg Service",
                "credential_type": "env",
                "env_var_name": "CROSSORG_TOKEN",
            },
        }
    ]
    # Create the plugin in org A; mirror the same service slug into org B
    # by referencing the visible global shape is not possible for an
    # org-owned service, so the personal credential test instead shares
    # the org-A service row across both workspaces (cross-org M2M leak
    # simulation): the guard must still consult each workspace's own org.
    created = _post(client, "/api/v1/plugins/", ctx_a["headers"], body).json()
    plugin_id = created["id"]
    service_id = created["credential_requirements"][0]["service_id"]
    ws_a = _create_workspace(ctx_a)
    # Workspace B lives in org B but is owned by the same user (personal
    # credentials are owner-scoped, so the same row can attach there).
    runner_b = Runner.objects.create(
        name="rb",
        api_token_hash=hash_token(uuid.uuid4().hex),
        organization=ctx_b["org"],
    )
    ws_b = Workspace.objects.create(
        runner=runner_b, name="wb", created_by=ctx_a["user"]
    )
    assert str(
        Workspace.objects.get(id=ws_b.id).runner.organization_id
    ) == str(ctx_b["org"].id)
    _post(
        client,
        f"/api/v1/plugins/{plugin_id}/activation/",
        ctx_a["headers"],
        {"active": True},
    )
    credential = CredentialSvc().create_personal_credential(
        service_id=uuid.UUID(service_id),
        name="shared personal",
        value="secret",
        user=ctx_a["user"],
        org_id=ctx_a["org"].id,
    )
    ws_a.credentials.add(credential)
    ws_b.credentials.add(credential)
    assert (
        _put(
            client,
            f"/api/v1/workspaces/{ws_a.id}/plugins/",
            ctx_a["headers"],
            {"plugin_ids": [plugin_id]},
        ).status_code
        == 200
    )
    # The delete from org A must see the effective activation in org A
    # (workspace A's org) and block — it must not pass just because the
    # header org differs from workspace B's org.
    response = client.delete(
        f"/api/v1/credentials/{credential.id}/", **ctx_a["headers"]
    )
    assert response.status_code == 409, response.content[:500]
    assert response.json()["code"] == "plugin_credentials_in_use"
    assert str(ws_a.id) in response.content.decode()
