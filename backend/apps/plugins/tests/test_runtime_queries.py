"""Runtime snapshot selection, freshness and bounded database work."""

from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model

from apps.credentials.models import Credential, CredentialService
from apps.harness.harness_service import HarnessService
from apps.harness.models import HarnessSession
from apps.organizations.models import Organization
from apps.plugins.models import (
    OrgPluginActivation,
    Plugin,
    PluginCredentialRequirement,
    PluginMcpServer,
    PluginSkill,
    WorkspacePluginActivation,
)
from apps.plugins.runtime import (
    PluginCredentialConfigError,
    build_workspace_plugin_snapshot,
)
from apps.runners.models import Runner, Workspace
from common.utils import encrypt_value


@pytest.fixture
def runtime_catalogue(
    db: None,
) -> tuple[Organization, Organization, Workspace, list[CredentialService]]:
    """Keep seeded plugins out of query-count and selection assertions."""
    Plugin.objects.all().delete()
    org = Organization.objects.create(name="Runtime", slug="runtime")
    foreign = Organization.objects.create(name="Foreign", slug="foreign")
    owner = get_user_model().objects.create_user(email="runtime@example.test")
    runner = Runner.objects.create(organization=org, api_token_hash=uuid.uuid4().hex)
    workspace = Workspace.objects.create(runner=runner, created_by=owner)
    services = [
        CredentialService.objects.create(
            name="Token", slug="runtime-token", credential_type="env"
        ),
        CredentialService.objects.create(
            name="OAuth",
            slug="runtime-oauth",
            credential_type="mcp_oauth",
            oauth_server_url="https://example.test/mcp",
        ),
    ]
    workspace.credentials.add(
        Credential.objects.create(
            organization=org,
            service=services[0],
            name="Runtime token",
            encrypted_value=encrypt_value("synthetic-token"),
        )
    )
    return org, foreign, workspace, services


def _plugin(
    workspace: Workspace,
    services: list[CredentialService],
    index: int,
    *,
    org: Organization | None = None,
) -> Plugin:
    """Create effective plugins with interleaved component names and bindings."""
    plugin = Plugin.objects.create(
        organization=org, name=f"Plugin {index:03}", slug=f"runtime-{index}"
    )
    OrgPluginActivation.objects.create(
        organization=workspace.runner.organization, plugin=plugin
    )
    WorkspacePluginActivation.objects.create(workspace=workspace, plugin=plugin)
    for position, name in [(0, "Zulu"), (1, "Middle"), (0, "Alpha")]:
        PluginSkill.objects.create(
            plugin=plugin,
            name=name,
            slug=name.lower(),
            position=position,
            body=f"Guide for {plugin.slug}: {name}",
        )
    PluginMcpServer.objects.create(
        plugin=plugin,
        name="Zulu",
        slug="stdio",
        command="synthetic-mcp",
        args=["--example"],
        cwd="/workspace/tools",
        env={"TOKEN": "{{credential.api_key}}"},
        resources={"desktop": {"activation": "first_tool"}},
        startup_timeout_seconds=45,
        request_timeout_seconds=90,
    )
    PluginMcpServer.objects.create(
        plugin=plugin,
        name="Alpha",
        slug="oauth",
        transport="streamable_http",
        url=services[1].oauth_server_url,
        auth_type="oauth",
        oauth_requirement_key="oauth",
        headers={"X-Example": plugin.slug},
    )
    for index, key in [(1, "oauth"), (0, "api_key")]:
        PluginCredentialRequirement.objects.create(
            plugin=plugin,
            key=key,
            credential_service=services[index],
            required=index == 0,
            description=f"Requirement for {plugin.slug}: {key}",
        )
    return plugin


def test_snapshot_preserves_selection_order_and_bindings(runtime_catalogue) -> None:
    """Batching must preserve visibility, activation, ordering and OAuth binding."""
    org, foreign, workspace, services = runtime_catalogue
    global_plugin = _plugin(workspace, services, 2)
    owned = _plugin(workspace, services, 1, org=org)
    _plugin(workspace, services, 0, org=foreign)
    for index, field in [(3, "enabled"), (4, "published")]:
        plugin = _plugin(workspace, services, index)
        Plugin.objects.filter(pk=plugin.id).update(**{field: False})
    inactive = _plugin(workspace, services, 5)
    OrgPluginActivation.objects.filter(plugin=inactive, organization=org).delete()
    OrgPluginActivation.objects.create(plugin=inactive, organization=foreign)
    unselected = _plugin(workspace, services, 6)
    WorkspacePluginActivation.objects.filter(plugin=unselected).delete()
    empty = Plugin.objects.create(name="Empty", slug="empty", organization=org)
    OrgPluginActivation.objects.create(plugin=empty, organization=org)
    WorkspacePluginActivation.objects.create(plugin=empty, workspace=workspace)

    snapshot = build_workspace_plugin_snapshot(workspace=workspace, org_id=org.id)

    assert [p.id for p in snapshot.plugins] == [empty.id, owned.id, global_plugin.id]
    assert snapshot.plugins[0].skills == snapshot.plugins[0].mcp_servers == ()
    assert snapshot.plugins[0].requirements == ()
    for plugin in snapshot.plugins[1:]:
        assert plugin.is_global == (plugin.id == global_plugin.id)
        assert [s.name for s in plugin.skills] == ["Alpha", "Zulu", "Middle"]
        assert all(plugin.slug in s.body for s in plugin.skills)
        assert [s.name for s in plugin.mcp_servers] == ["Alpha", "Zulu"]
        oauth, stdio = plugin.mcp_servers
        assert oauth.oauth_service_id == services[1].id
        assert oauth.headers == {"X-Example": plugin.slug}
        assert oauth.auth_type == "oauth"
        assert oauth.url == services[1].oauth_server_url
        assert stdio.oauth_service_id is None
        assert stdio.env == {"TOKEN": "{{credential.api_key}}"}
        assert stdio.args == ("--example",)
        assert stdio.cwd == "/workspace/tools"
        assert stdio.resources.desktop.activation == "first_tool"
        assert (stdio.startup_timeout_seconds, stdio.request_timeout_seconds) == (
            45,
            90,
        )
        assert [r.key for r in plugin.requirements] == ["api_key", "oauth"]
        assert [r.service_id for r in plugin.requirements] == [s.id for s in services]
        assert [r.required for r in plugin.requirements] == [True, False]
        assert all(plugin.slug in r.description for r in plugin.requirements)


def test_snapshot_refreshes_edits_and_revoked_activation(runtime_catalogue) -> None:
    """Later calls see edits without changing previously returned snapshots."""
    org, _, workspace, services = runtime_catalogue
    plugin = _plugin(workspace, services, 0)
    before = build_workspace_plugin_snapshot(workspace=workspace, org_id=org.id)
    PluginSkill.objects.filter(plugin=plugin).update(body="Changed instructions")
    PluginMcpServer.objects.filter(plugin=plugin).delete()
    PluginCredentialRequirement.objects.filter(plugin=plugin).delete()
    after = build_workspace_plugin_snapshot(workspace=workspace, org_id=org.id)
    assert all(s.body == "Changed instructions" for s in after.plugins[0].skills)
    assert after.plugins[0].mcp_servers == after.plugins[0].requirements == ()
    assert len(before.plugins[0].mcp_servers) == 2
    assert len(before.plugins[0].requirements) == 2
    OrgPluginActivation.objects.filter(plugin=plugin, organization=org).delete()
    assert not build_workspace_plugin_snapshot(
        workspace=workspace, org_id=org.id
    ).plugins


@pytest.mark.parametrize("count", [1, 5, 15, 50])
def test_snapshot_queries_are_bounded(
    runtime_catalogue, count: int, django_assert_num_queries
) -> None:
    """One query per component kind, independent of effective plugin count."""
    org, _, workspace, services = runtime_catalogue
    for index in range(count):
        _plugin(workspace, services, index)
    with django_assert_num_queries(6):
        snapshot = build_workspace_plugin_snapshot(workspace=workspace, org_id=org.id)
    assert len(snapshot.plugins) == count


@pytest.mark.parametrize("count", [1, 15])
def test_run_preparation_queries_are_bounded(
    runtime_catalogue, count: int, django_assert_num_queries
) -> None:
    """Exercise the harness caller including workspace reads and real decryption."""
    org, _, workspace, services = runtime_catalogue
    for index in range(count):
        _plugin(workspace, services, index)
    session = HarnessSession(workspace_id=workspace.id)
    with django_assert_num_queries(11):
        prepared = HarnessService._prepare_mcp_snapshot_for_run(session, org.id)
    assert len(prepared.snapshot.plugins) == count
    assert all(
        row == {"api_key": "synthetic-token"} for row in prepared.plaintexts.values()
    )
    assert prepared.oauth_credentials == {}


@pytest.mark.parametrize("visible", [False, True])
def test_empty_snapshot_skips_component_queries(
    runtime_catalogue, visible: bool, django_assert_num_queries
) -> None:
    """Visible but ineffective plugins must not load component payloads."""
    org, foreign, workspace, _ = runtime_catalogue
    Plugin.objects.create(
        name="Inactive", slug="inactive", organization=org if visible else foreign
    )
    with django_assert_num_queries(3 if visible else 1):
        assert not build_workspace_plugin_snapshot(
            workspace=workspace, org_id=org.id
        ).plugins


def test_mismatched_org_fails_before_loading_rows(
    runtime_catalogue, django_assert_num_queries
) -> None:
    """Batch reads must remain behind the existing tenant check."""
    _, foreign, workspace, _ = runtime_catalogue
    with django_assert_num_queries(0):
        with pytest.raises(PluginCredentialConfigError, match="organization mismatch"):
            build_workspace_plugin_snapshot(workspace=workspace, org_id=foreign.id)
