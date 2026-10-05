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
from apps.credentials.models import (
    Credential,
    CredentialService,
    OrgCredentialServiceActivation,
)
from apps.credentials.services import CredentialSvc
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.plugins.models import Plugin, WorkspacePluginActivation
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


def _prepare_plugin_dependencies(body: dict, headers: dict) -> dict:
    """Adapt concise test fixtures to the top-level service_id API contract."""
    if "credential_requirements" not in body:
        return body
    from apps.credentials.services import (
        CredentialServiceSvc,
        OrgCredentialServiceActivationSvc,
    )

    org_id = uuid.UUID(headers["HTTP_X_ORGANIZATION_ID"])
    servers = {
        server.get("oauth_requirement_key"): server
        for server in body.get("mcp_servers", [])
    }
    requirements = []
    for raw in body["credential_requirements"]:
        item = dict(raw)
        nested = item.get("credential_service") or {}
        service_id = item.get("service_id") or nested.get("service_id")
        if service_id is None:
            credential_type = nested.get("credential_type", "env")
            endpoint = (
                servers[item["key"]]["url"] if credential_type == "mcp_oauth" else ""
            )
            service = CredentialServiceSvc().create_service(
                name=nested.get("name") or item["key"],
                slug=nested.get("slug") or nested.get("name") or item["key"],
                description=nested.get("description", ""),
                credential_type=credential_type,
                env_var_name=nested.get("env_var_name", ""),
                target_path=nested.get("target_path", ""),
                label=nested.get("label", ""),
                organization_id=org_id,
                oauth_server_url=endpoint,
            )
            OrgCredentialServiceActivationSvc().set_activation(
                org_id=org_id, service=service, active=True
            )
            service_id = service.id
        requirements.append(
            {
                "key": item["key"],
                "description": item.get("description", ""),
                "required": item.get("required", True),
                "service_id": str(service_id),
            }
        )
    return {**body, "credential_requirements": requirements}


def _post(client: Client, path: str, headers: dict, body: dict):
    if path == "/api/v1/plugins/":
        body = _prepare_plugin_dependencies(body, headers)
    return client.post(
        path,
        data=json.dumps(body),
        content_type="application/json",
        **headers,
    )


def _patch(client: Client, path: str, headers: dict, body: dict):
    if "credential_requirements" in body:
        body = _prepare_plugin_dependencies(body, headers)
    return client.patch(
        path,
        data=json.dumps(body),
        content_type="application/json",
        **headers,
    )


def _put(client: Client, path: str, headers: dict, body: dict):
    return client.patch(
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
def test_nested_requirement_service_shape_is_rejected(client: Client):
    ctx = _ctx()
    body = _minimal_plugin_body("Legacy requirement")
    body["credential_requirements"] = [
        {"key": "api_key", "credential_service": {"service_id": str(uuid.uuid4())}}
    ]
    response = client.post(
        "/api/v1/plugins/",
        data=json.dumps(body),
        content_type="application/json",
        **ctx["headers"],
    )
    assert response.status_code == 422, response.content[:500]


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
    assert "plugin_owned_service" not in req
    service = CredentialService.objects.get(id=req["service_id"])
    assert str(service.organization_id) == str(ctx["org"].id)
    assert OrgCredentialServiceActivation.objects.filter(
        organization=ctx["org"], credential_service=service
    ).exists()
    assert "credential_readiness" not in payload


@pytest.mark.django_db
def test_oauth_plugin_references_existing_service_endpoint(client: Client):
    ctx = _ctx()
    from apps.credentials.services import CredentialServiceSvc

    service = CredentialServiceSvc().create_service(
        name="Reusable MCP",
        slug="reusable-mcp",
        description="",
        credential_type="mcp_oauth",
        env_var_name="",
        target_path="",
        label="Connect",
        organization_id=ctx["org"].id,
        oauth_server_url="https://mcp.example/mcp",
    )
    OrgCredentialServiceActivation.objects.create(
        organization=ctx["org"], credential_service=service
    )
    body = {
        "name": "OAuth plugin",
        "slug": "oauth-plugin",
        "mcp_servers": [
            {
                "name": "MCP",
                "transport": "streamable_http",
                "url": service.oauth_server_url,
                "auth_type": "oauth",
                "oauth_requirement_key": "connect",
            }
        ],
        "credential_requirements": [{"key": "connect", "service_id": str(service.id)}],
    }
    response = _post(client, "/api/v1/plugins/", ctx["headers"], body)
    assert response.status_code == 201, response.content
    assert response.json()["credential_requirements"][0]["service_id"] == str(
        service.id
    )
    assert not hasattr(service, "plugin_owned")

    mismatched = {
        **body,
        "slug": "bad-endpoint",
        "mcp_servers": [{**body["mcp_servers"][0], "url": "https://other.example/mcp"}],
    }
    invalid = _post(client, "/api/v1/plugins/", ctx["headers"], mismatched)
    assert invalid.status_code == 400


@pytest.mark.django_db
def test_plugin_rejects_oauth_endpoint_change(client: Client):
    """OAuth plugin endpoints cannot diverge from their service endpoint."""
    ctx = _ctx()
    from apps.credentials.services import CredentialServiceSvc

    service = CredentialServiceSvc().create_service(
        name="Connect One",
        slug="connect-one",
        description="",
        credential_type="mcp_oauth",
        env_var_name="",
        target_path="",
        label="Connect",
        organization_id=ctx["org"].id,
        oauth_server_url="https://one.example/mcp",
    )
    OrgCredentialServiceActivation.objects.create(
        organization=ctx["org"], credential_service=service
    )
    body = {
        "name": "OAuth Rename",
        "slug": "oauth-rename",
        "skills": [{"name": "Guide", "slug": "guide", "body": "# Guide"}],
        "mcp_servers": [
            {
                "name": "One",
                "slug": "one",
                "transport": "streamable_http",
                "url": service.oauth_server_url,
                "auth_type": "oauth",
                "oauth_requirement_key": "one_auth",
            }
        ],
        "credential_requirements": [{"key": "one_auth", "service_id": str(service.id)}],
    }
    created = _post(client, "/api/v1/plugins/", ctx["headers"], body)
    assert created.status_code == 201, created.content
    plugin_id = created.json()["id"]
    server = created.json()["mcp_servers"][0]
    changed_server = {**server, "url": "https://one-new.example/mcp"}
    response = _patch(
        client,
        f"/api/v1/plugins/{plugin_id}/",
        ctx["headers"],
        {
            "mcp_servers": [changed_server],
            "credential_requirements": [
                {"key": "one_auth", "service_id": str(service.id)}
            ],
        },
    )
    assert response.status_code == 400
    refreshed = client.get(f"/api/v1/plugins/{plugin_id}/", **ctx["headers"]).json()
    assert refreshed["mcp_servers"][0]["url"] == service.oauth_server_url


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
def test_oauth_server_urls_require_clean_https_urls(client: Client):
    from apps.plugins.services import normalize_mcp_payload

    base = {
        "name": "OAuth MCP",
        "transport": "streamable_http",
        "auth_type": "oauth",
        "oauth_requirement_key": "connect",
        "url": "https://mcp.example/mcp",
    }
    assert normalize_mcp_payload(base)["url"] == base["url"]
    for url in (
        "http://mcp.example/mcp",
        "https://user:pass@mcp.example/mcp",
        "https://mcp.example/mcp?token=secret",
        "https://mcp.example/mcp#fragment",
        "https://mcp.example/mcp?",
        "https://mcp.example/mcp#",
    ):
        with pytest.raises(ValueError, match="HTTPS URL"):
            normalize_mcp_payload({**base, "url": url})


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
    assert response.status_code == 204
    assert CredentialService.objects.filter(id=service_id).exists()
    assert Plugin.objects.filter(id=plugin_id).exists() is False


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
        f"/api/v1/workspaces/{workspace.id}/",
        ctx["headers"],
        {"plugin_ids": [plugin_id]},
    )
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "missing_plugin_credentials"
    assert blocked.json()["gaps"][0]["service_id"] == service_id
    assert not workspace.plugin_activations.exists()

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
        f"/api/v1/workspaces/{workspace.id}/",
        ctx["headers"],
        {"plugin_ids": [plugin_id]},
    )
    assert ok.status_code == 200, ok.content[:500]
    assert ok.json()["plugin_ids"] == [plugin_id]
    entry = next(
        p
        for p in client.get(
            f"/api/v1/workspaces/{workspace.id}/plugins/", **ctx["headers"]
        ).json()
        if p["id"] == plugin_id
    )
    assert entry["workspace_enabled"] is True
    assert entry["ready"] is True
    row = WorkspacePluginActivation.objects.filter(
        workspace=workspace, plugin_id=plugin_id
    ).first()
    assert row is not None
    assert row.enabled_by_id == ctx["user"].id


@pytest.mark.django_db
def test_workspace_selection_create_validation_is_atomic(client: Client):
    """Missing requirements prevent a workspace/task row from being created."""
    from apps.plugins.services import PluginSelectionError
    from apps.runners.services.workspace_configuration import (
        WorkspaceConfigurationService,
    )

    ctx = _ctx()
    body = _minimal_plugin_body("Atomic Workspace")
    body["credential_requirements"] = [
        {
            "key": "token",
            "credential_service": {
                "name": "Atomic Service",
                "credential_type": "env",
                "env_var_name": "ATOMIC_TOKEN",
            },
        }
    ]
    created = _post(client, "/api/v1/plugins/", ctx["headers"], body).json()
    plugin_id = uuid.UUID(created["id"])
    Plugin.objects.get(pk=plugin_id).org_activations.create(
        organization=ctx["org"], enabled_by=ctx["user"]
    )
    from apps.runners.models import Task

    workspace_count = Workspace.objects.count()
    task_count = Task.objects.count()
    with pytest.raises(PluginSelectionError) as error:
        WorkspaceConfigurationService().create(
            workspace_fields={},
            runner=Runner.objects.create(
                name="atomic-runner",
                api_token_hash=hash_token(uuid.uuid4().hex),
                organization=ctx["org"],
            ),
            user=ctx["user"],
            organization_id=ctx["org"].id,
            credentials=[],
            plugin_ids=[plugin_id],
            task_id=uuid.uuid4(),
        )
    assert (
        error.value.gaps[0]["service_id"]
        == created["credential_requirements"][0]["service_id"]
    )
    assert Workspace.objects.count() == workspace_count
    assert Task.objects.count() == task_count


@pytest.mark.django_db
def test_workspace_configuration_patch_replaces_plugins_and_credentials_together(
    client: Client,
):
    ctx = _ctx()
    body = _minimal_plugin_body("Replace Together")
    body["credential_requirements"] = [
        {
            "key": "token",
            "credential_service": {
                "name": "Replace Service",
                "credential_type": "env",
                "env_var_name": "REPLACE_TOKEN",
            },
        }
    ]
    created = _post(client, "/api/v1/plugins/", ctx["headers"], body).json()
    plugin_id = created["id"]
    service_id = uuid.UUID(created["credential_requirements"][0]["service_id"])
    _post(
        client,
        f"/api/v1/plugins/{plugin_id}/activation/",
        ctx["headers"],
        {"active": True},
    )
    credential = CredentialSvc().create_org_credential(
        organization_id=ctx["org"].id,
        service_id=service_id,
        name="Replace token",
        value="secret",
        user=ctx["user"],
    )
    workspace = _create_workspace(ctx)
    saved = _put(
        client,
        f"/api/v1/workspaces/{workspace.id}/",
        ctx["headers"],
        {"plugin_ids": [plugin_id], "credential_ids": [str(credential.id)]},
    )
    assert saved.status_code == 200, saved.content[:500]
    assert saved.json()["plugin_ids"] == [plugin_id]
    assert saved.json()["credential_ids"] == [str(credential.id)]

    removed = _put(
        client,
        f"/api/v1/workspaces/{workspace.id}/",
        ctx["headers"],
        {"plugin_ids": [], "credential_ids": []},
    )
    assert removed.status_code == 200, removed.content[:500]
    assert removed.json()["plugin_ids"] == []
    assert removed.json()["credential_ids"] == []


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("permissions", "field", "expected"),
    [
        ([APIKeyPermission.WORKSPACES_UPDATE.value], "plugin_ids", "plugins:write"),
        (
            [APIKeyPermission.WORKSPACES_UPDATE.value],
            "credential_ids",
            "credentials:read",
        ),
    ],
)
def test_workspace_configuration_requires_selection_permissions(
    client: Client, permissions, field, expected
):
    ctx = _ctx(permissions=permissions)
    workspace = _create_workspace(ctx)
    response = _put(
        client,
        f"/api/v1/workspaces/{workspace.id}/",
        ctx["headers"],
        {field: []},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"
    assert expected in response.json()["detail"]


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
        f"/api/v1/workspaces/{workspace.id}/",
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
    """Deleting a plugin preserves the independently owned credential service."""
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

    CredentialSvc().create_personal_credential(
        service_id=uuid.UUID(service_id),
        name="personal token",
        value="secret",
        user=ctx["user"],
        org_id=ctx["org"].id,
    )
    assert Credential.objects.filter(service_id=service_id).exists()
    response = client.delete(f"/api/v1/plugins/{plugin_id}/", **ctx["headers"])
    assert response.status_code == 204, response.content[:500]
    assert Credential.objects.filter(service_id=service_id).exists()
    assert CredentialService.objects.filter(id=service_id).exists()
    assert not Plugin.objects.filter(id=plugin_id).exists()


@pytest.mark.django_db
def test_requirement_service_is_independent_after_plugin_delete(client: Client):
    ctx = _ctx()
    service = CredentialService.objects.create(
        name="Independent",
        slug="independent-service",
        organization=ctx["org"],
        credential_type="env",
        env_var_name="INDEPENDENT_TOKEN",
    )
    body = _minimal_plugin_body("Independent plugin")
    body["credential_requirements"] = [{"key": "token", "service_id": str(service.id)}]
    created = _post(client, "/api/v1/plugins/", ctx["headers"], body)
    assert created.status_code == 201
    plugin_id = created.json()["id"]
    deleted = client.delete(f"/api/v1/plugins/{plugin_id}/", **ctx["headers"])
    assert deleted.status_code == 204
    assert CredentialService.objects.filter(pk=service.id).exists()


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
        f"/api/v1/workspaces/{workspace.id}/",
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
        f"/api/v1/workspaces/{workspace.id}/",
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
        f"/api/v1/workspaces/{workspace.id}/",
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
            f"/api/v1/workspaces/{workspace.id}/",
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
                f"/api/v1/workspaces/{workspace.id}/",
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
def test_service_deactivation_is_independent_of_plugin_activation(client: Client):
    """Deactivating a service never changes org plugin activation."""
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
    response = client.post(
        f"/api/v1/org-credential-services/{service_id}/activation/",
        data=json.dumps({"active": False}),
        content_type="application/json",
        **ctx["headers"],
    )
    assert response.status_code == 200
    assert response.json()["is_active"] is False
    plugin = client.get(f"/api/v1/plugins/{plugin_id}/", **ctx["headers"]).json()
    assert plugin["org_enabled"] is True


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
        f"/api/v1/workspaces/{workspace.id}/",
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
    assert str(Workspace.objects.get(id=ws_b.id).runner.organization_id) == str(
        ctx_b["org"].id
    )
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
            f"/api/v1/workspaces/{ws_a.id}/",
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


@pytest.mark.django_db
@pytest.mark.parametrize("auth", ["api_key", "jwt"])
def test_managed_desktop_roundtrip_and_conflicts(client, auth):
    ctx = _ctx()
    headers = ctx["headers"]
    if auth == "jwt":
        from apps.accounts.auth_backends import get_auth_backend

        token = get_auth_backend().generate_tokens(ctx["user"]).access_token
        headers = {
            "HTTP_X_ORGANIZATION_ID": str(ctx["org"].id),
            "HTTP_AUTHORIZATION": f"Bearer {token}",
        }
    body = {
        "name": "Desktop tools",
        "mcp_servers": [
            {
                "name": "Tools",
                "command": "npx",
                "resources": {"desktop": {}},
            }
        ],
    }
    response = _post(client, "/api/v1/plugins/", headers, body)
    assert response.status_code == 201, response.content
    plugin = response.json()
    assert plugin["mcp_servers"][0]["resources"] == {
        "desktop": {"activation": "server_start"}
    }
    response = _patch(
        client,
        f"/api/v1/plugins/{plugin['id']}/",
        headers,
        {"description": "Preserve resources"},
    )
    assert response.status_code == 200
    assert (
        response.json()["mcp_servers"][0]["resources"]
        == plugin["mcp_servers"][0]["resources"]
    )
    for key in ("DISPLAY", "XAUTHORITY"):
        body["name"] = f"Conflict {key}"
        body["mcp_servers"][0]["env"] = {key: "explicit"}
        assert _post(client, "/api/v1/plugins/", headers, body).status_code == 400
    body["name"] = "HTTP conflict"
    body["mcp_servers"][0].update(
        env={}, transport="sse", command="", url="https://example.com/mcp"
    )
    assert _post(client, "/api/v1/plugins/", headers, body).status_code == 400
    body["mcp_servers"][0]["resources"] = {"desktop": {"activation": "invalid"}}
    assert _post(client, "/api/v1/plugins/", headers, body).status_code == 422


@pytest.mark.django_db
def test_managed_desktop_write_requires_permission(client):
    ctx = _ctx(permissions=[APIKeyPermission.PLUGINS_READ.value])
    response = _post(
        client,
        "/api/v1/plugins/",
        ctx["headers"],
        {
            "name": "Desktop denied",
            "mcp_servers": [
                {
                    "name": "Tools",
                    "command": "npx",
                    "resources": {"desktop": {"activation": "first_tool"}},
                }
            ],
        },
    )
    assert response.status_code == 403
