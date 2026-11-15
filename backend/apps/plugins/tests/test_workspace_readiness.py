"""Workspace readiness contracts, fail-closed credentials and bounded SQL work."""

from __future__ import annotations

import json

import pytest
from django.contrib.auth import get_user_model

from apps.credentials.models import (
    Credential,
    CredentialService,
    McpOAuthClientRegistration,
)
from apps.credentials.services import CredentialSvc
from apps.organizations.models import Organization
from apps.plugins.models import (
    OrgPluginActivation,
    Plugin,
    PluginCredentialRequirement,
    PluginMcpServer,
    WorkspacePluginActivation,
)
from apps.plugins.services import PluginService
from apps.runners.models import Runner, Workspace
from common.exceptions import ConflictError, NotFoundError
from common.utils import encrypt_value


@pytest.fixture
def readiness(db: None) -> tuple:
    """Isolate plugin counts from seeds and supply both credential types."""
    Plugin.objects.all().delete()
    org = Organization.objects.create(name="Readiness", slug="readiness")
    foreign = Organization.objects.create(name="Foreign", slug="foreign")
    owner = get_user_model().objects.create_user(email="readiness@example.test")
    runner = Runner.objects.create(organization=org, api_token_hash="unused")
    workspace = Workspace.objects.create(runner=runner, created_by=owner)
    services = [
        CredentialService.objects.create(
            name="Token", slug="readiness-token", credential_type="env"
        ),
        CredentialService.objects.create(
            name="OAuth",
            slug="readiness-oauth",
            credential_type="mcp_oauth",
            oauth_server_url="https://example.test/mcp",
        ),
    ]
    return org, foreign, workspace, services


def _plugin(readiness: tuple, index: int, *, oauth: bool = False) -> Plugin:
    org, _, _, services = readiness
    plugin = Plugin.objects.create(
        organization=org if index % 2 else None,
        name=f"Plugin {index:03}",
        slug=f"readiness-{index}",
    )
    OrgPluginActivation.objects.create(organization=org, plugin=plugin)
    PluginCredentialRequirement.objects.create(
        plugin=plugin, key="token", credential_service=services[int(oauth)]
    )
    if oauth:
        for index in range(2):
            PluginMcpServer.objects.create(
                plugin=plugin,
                name=f"Tools {index}",
                slug=f"tools-{index}",
                auth_type="oauth",
                oauth_requirement_key="token",
                transport="streamable_http",
                url=services[1].oauth_server_url,
            )
    return plugin


def _attach(workspace: Workspace, service: CredentialService) -> Credential:
    """Create a synthetic attached token, with a validated grant for OAuth."""
    credential = Credential.objects.create(
        organization=workspace.runner.organization,
        service=service,
        name="Synthetic token",
        encrypted_value=encrypt_value("synthetic-token"),
    )
    if service.credential_type == "mcp_oauth":
        endpoint = service.oauth_server_url
        registration = McpOAuthClientRegistration.objects.create(
            server_url=endpoint,
            callback_url="https://example.test/callback",
            issuer="https://example.test",
            client_id="synthetic-client",
            authorization_endpoint="https://example.test/authorize",
            token_endpoint="https://example.test/token",
        )
        credential.oauth_registration = registration
        credential.oauth_status = "connected"
        credential.oauth_server_url = credential.oauth_resource = endpoint
        credential.encrypted_value = encrypt_value(
            json.dumps(
                {
                    "access_token": "synthetic-oauth-token",
                    "server_url": endpoint,
                    "resource": endpoint,
                    "service_id": str(service.id),
                    "registration_id": str(registration.id),
                }
            )
        )
        credential.save()
    workspace.credentials.add(credential)
    return credential


@pytest.mark.parametrize("count", [0, 1, 5, 15, 50])
@pytest.mark.parametrize("oauth", [False, True])
def test_readiness_queries_are_bounded(
    readiness: tuple, count: int, oauth: bool, django_assert_num_queries
) -> None:
    """Both public paths must avoid per-plugin and per-OAuth-binding queries."""
    org, _, workspace, services = readiness
    plugins = [_plugin(readiness, index, oauth=oauth) for index in range(count)]
    _attach(workspace, services[int(oauth)])
    credentials = CredentialSvc().list_workspace_credentials(workspace.id)
    service = PluginService()
    related_queries = int(count > 0) * (1 + int(oauth))

    with django_assert_num_queries(4 + related_queries):
        rows = service.list_workspace_plugins(workspace=workspace, org_id=org.id)
    assert len(rows) == count
    assert all(row["ready"] for row in rows)

    with django_assert_num_queries(2 + related_queries):
        assert (
            service.validate_workspace_plugin_selection(
                plugin_ids=[plugin.id for plugin in reversed(plugins)],
                credentials=credentials,
                owner=workspace.created_by,
                org_id=org.id,
            )
            == []
        )


def test_gap_order_and_workspace_state(readiness: tuple) -> None:
    """Listing uses plugin/key order; validation retains deduplicated input order."""
    org, _, workspace, services = readiness
    plugins = [_plugin(readiness, index) for index in range(3)]
    for plugin in plugins:
        PluginCredentialRequirement.objects.create(
            plugin=plugin, key="alpha", credential_service=services[1]
        )
    WorkspacePluginActivation.objects.create(workspace=workspace, plugin=plugins[1])
    service = PluginService()
    rows = service.list_workspace_plugins(workspace=workspace, org_id=org.id)
    assert [row["id"] for row in rows] == [plugin.id for plugin in plugins]
    assert [row["workspace_enabled"] for row in rows] == [False, True, False]
    assert [row["is_global"] for row in rows] == [True, False, True]
    assert all(not row["ready"] for row in rows)
    assert all(
        [gap["key"] for gap in row["missing_required_credentials"]]
        == ["alpha", "token"]
        for row in rows
    )
    gaps = service.validate_workspace_plugin_selection(
        plugin_ids=[plugins[2].id, plugins[0].id, plugins[2].id],
        credentials=[],
        owner=workspace.created_by,
        org_id=org.id,
    )
    assert gaps == (
        rows[2]["missing_required_credentials"]
        + rows[0]["missing_required_credentials"]
    )
    assert gaps[1] == {
        "plugin_id": str(plugins[2].id),
        "plugin_name": plugins[2].name,
        "key": "token",
        "service_id": str(services[0].id),
        "service_name": "Token",
        "credential_type": "env",
    }


@pytest.mark.parametrize(
    "scope",
    ["personal", "org", "foreign_user", "foreign_org", "foreign_service", "internal"],
)
def test_credential_ownership_is_fail_closed(readiness: tuple, scope: str) -> None:
    org, foreign, workspace, services = readiness
    plugin = _plugin(readiness, 0)
    credential = _attach(workspace, services[0])
    if scope in {"personal", "foreign_user"}:
        credential.organization = None
        credential.user = (
            workspace.created_by
            if scope == "personal"
            else get_user_model().objects.create_user(email="foreign@example.test")
        )
        credential.save()
    elif scope == "foreign_org":
        credential.organization = foreign
        credential.save()
    elif scope in {"foreign_service", "internal"}:
        services[0].organization = foreign if scope == "foreign_service" else None
        services[0].credential_type = (
            "provider_secret" if scope == "internal" else "env"
        )
        services[0].save()

    service = PluginService()
    row = service.list_workspace_plugins(workspace=workspace, org_id=org.id)[0]
    assert row["ready"] is (scope in {"personal", "org"})
    assert (
        service.validate_workspace_plugin_selection(
            plugin_ids=[plugin.id],
            credentials=CredentialSvc().list_workspace_credentials(
                workspace.id, include_internal=True
            ),
            owner=workspace.created_by,
            org_id=org.id,
        )
        == row["missing_required_credentials"]
    )


@pytest.mark.parametrize(
    "case",
    [
        "ready",
        "missing_servers",
        "wrong_key",
        "other_plugin",
        "server_mismatch",
        "credential_mismatch",
        "disconnected",
        "corrupt_grant",
        "registration_changed",
    ],
)
def test_oauth_requires_all_bound_servers_and_valid_grant(
    readiness: tuple, case: str
) -> None:
    org, _, workspace, services = readiness
    plugin = _plugin(readiness, 0, oauth=True)
    credential = _attach(workspace, services[1])
    servers = PluginMcpServer.objects.filter(plugin=plugin)
    if case == "missing_servers":
        servers.delete()
    elif case == "wrong_key":
        servers.update(oauth_requirement_key="unrelated")
    elif case == "other_plugin":
        other = Plugin.objects.create(name="Other", slug="other")
        servers.update(plugin=other)
    elif case == "server_mismatch":
        servers.filter(slug="tools-1").update(url="https://other.test/mcp")
    elif case == "credential_mismatch":
        credential.oauth_server_url = "https://other.test/mcp"
        credential.save()
    elif case == "disconnected":
        credential.oauth_status = "reconnect_required"
        credential.save()
    elif case == "corrupt_grant":
        credential.encrypted_value = encrypt_value("invalid grant")
        credential.save()
    elif case == "registration_changed":
        registration = credential.oauth_registration
        registration.server_url = "https://other.test/mcp"
        registration.save()

    service = PluginService()
    row = service.list_workspace_plugins(workspace=workspace, org_id=org.id)[0]
    assert row["ready"] is (case == "ready")
    assert (
        service.validate_workspace_plugin_selection(
            plugin_ids=[plugin.id],
            credentials=CredentialSvc().list_workspace_credentials(workspace.id),
            owner=workspace.created_by,
            org_id=org.id,
        )
        == row["missing_required_credentials"]
    )
    assert "synthetic-oauth-token" not in json.dumps(row, default=str)


def test_visibility_optional_requirements_and_unavailable_selection(
    readiness: tuple,
) -> None:
    org, foreign, workspace, _ = readiness
    available = _plugin(readiness, 0)
    available.credential_requirements.update(required=False)
    unavailable = []
    for index, field in [(1, "enabled"), (2, "published"), (3, "activation")]:
        plugin = _plugin(readiness, index)
        if field == "activation":
            OrgPluginActivation.objects.filter(plugin=plugin).update(
                organization=foreign
            )
        else:
            Plugin.objects.filter(pk=plugin.id).update(**{field: False})
        unavailable.append(plugin.id)
    hidden = _plugin(readiness, 4)
    hidden.organization = foreign
    hidden.save()
    service = PluginService()
    rows = service.list_workspace_plugins(workspace=workspace, org_id=org.id)
    assert len(rows) == 1 and rows[0]["id"] == available.id and rows[0]["ready"]
    selection = {"credentials": [], "owner": workspace.created_by, "org_id": org.id}
    with pytest.raises(NotFoundError):
        service.validate_workspace_plugin_selection(plugin_ids=[hidden.id], **selection)
    with pytest.raises(ConflictError) as error:
        service.validate_workspace_plugin_selection(plugin_ids=unavailable, **selection)
    assert error.value.code == "plugin_not_available"
    assert (
        service.validate_workspace_plugin_selection(
            plugin_ids=[available.id, *unavailable], allow_unavailable=True, **selection
        )
        == []
    )


def test_oauth_bindings_are_scoped_to_plugin_and_requirement(readiness: tuple) -> None:
    """Shared requirement keys cannot mix endpoints or optional server bindings."""
    org, _, workspace, services = readiness
    first = _plugin(readiness, 0, oauth=True)
    second = _plugin(readiness, 1, oauth=True)
    other_service = CredentialService.objects.create(
        name="Other OAuth",
        slug="other-oauth",
        credential_type="mcp_oauth",
        oauth_server_url="https://other.test/mcp",
    )
    second.credential_requirements.update(credential_service=other_service)
    second.mcp_servers.update(url=other_service.oauth_server_url)
    PluginCredentialRequirement.objects.create(
        plugin=first, key="optional", credential_service=other_service, required=False
    )
    PluginMcpServer.objects.create(
        plugin=first,
        name="Optional",
        slug="optional",
        auth_type="oauth",
        oauth_requirement_key="optional",
        transport="streamable_http",
        url=other_service.oauth_server_url,
    )
    _attach(workspace, services[1])
    service = PluginService()
    rows = service.list_workspace_plugins(workspace=workspace, org_id=org.id)
    assert [row["ready"] for row in rows] == [True, False]
    assert (
        service.validate_workspace_plugin_selection(
            plugin_ids=[second.id, first.id],
            credentials=CredentialSvc().list_workspace_credentials(workspace.id),
            owner=workspace.created_by,
            org_id=org.id,
        )
        == rows[1]["missing_required_credentials"]
    )


def test_repeated_calls_read_current_configuration(readiness: tuple) -> None:
    """A reused service never retains credentials, requirements or OAuth mappings."""
    org, _, workspace, services = readiness
    plugin = _plugin(readiness, 0, oauth=True)
    service = PluginService()
    assert not service.list_workspace_plugins(workspace=workspace, org_id=org.id)[0][
        "ready"
    ]
    credential = _attach(workspace, services[1])
    assert service.list_workspace_plugins(workspace=workspace, org_id=org.id)[0][
        "ready"
    ]
    workspace.credentials.remove(credential)
    assert not service.list_workspace_plugins(workspace=workspace, org_id=org.id)[0][
        "ready"
    ]
    plugin.credential_requirements.update(required=False)
    assert service.list_workspace_plugins(workspace=workspace, org_id=org.id)[0][
        "ready"
    ]
    plugin.credential_requirements.update(required=True)
    workspace.credentials.add(credential)
    plugin.mcp_servers.update(oauth_requirement_key="unrelated")
    assert not service.list_workspace_plugins(workspace=workspace, org_id=org.id)[0][
        "ready"
    ]
