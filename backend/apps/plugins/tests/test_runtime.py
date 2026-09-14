"""Unit tests for the plugin runtime snapshot + credential rendering.

Covers effective-plugin resolution (visible, enabled+published, org AND
workspace activation), credential templates (personal/org, missing,
org-deactivate, foreign rows ignored), and the guarantee that no secret
values are logged or returned through the API-shaped payloads.
"""

from __future__ import annotations

import uuid
import uuid as _uuid  # noqa: F401  (service ids arrive as strings)

import pytest
from django.contrib.auth import get_user_model

from apps.credentials.models import CredentialService
from apps.credentials.services import CredentialSvc
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.plugins import runtime as plugin_runtime
from apps.plugins.models import Plugin
from apps.plugins.services import PluginService
from apps.runners.models import Runner, Workspace
from common.utils import hash_token


@pytest.fixture
def org_ctx(db):
    user_model = get_user_model()
    user = user_model.objects.create_user(
        email=f"rt-{uuid.uuid4().hex[:8]}@example.com", password="secret"
    )
    org = Organization.objects.create(
        name=f"RT {uuid.uuid4().hex[:6]}", slug=f"rt-{uuid.uuid4().hex[:10]}"
    )
    Membership.objects.create(user=user, organization=org, role=MembershipRole.ADMIN)
    runner = Runner.objects.create(
        name="rt-runner",
        api_token_hash=hash_token(uuid.uuid4().hex),
        organization=org,
    )
    workspace = Workspace.objects.create(
        runner=runner, name="rt-ws", created_by=user
    )
    return {"user": user, "org": org, "workspace": workspace}


def _make_plugin(org, user, *, name="P", slug="p", published=True, enabled=True):
    return PluginService().create_org_plugin(
        org_id=org.id,
        user=user,
        name=name,
        slug=slug,
        enabled=enabled,
        published=published,
        skills=[{"name": "Guide", "body": "# guide", "position": 0}],
        mcp_servers=[
            {
                "name": "Tools",
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "thing"],
                "env": {"TOKEN": "{{credential.api_key}}"},
            }
        ],
        credential_requirements=[
            {
                "key": "api_key",
                "required": True,
                "credential_service": {
                    "name": "RT Service",
                    "credential_type": "env",
                    "env_var_name": "RT_TOKEN",
                },
            }
        ],
    )


@pytest.mark.django_db
def test_snapshot_only_effective_plugins(org_ctx):
    PluginService().create_org_plugin(
        org_id=org_ctx["org"].id,
        user=org_ctx["user"],
        name="Hidden",
        slug="hidden",
        published=False,
        skills=[],
        mcp_servers=[],
    )
    payload = _make_plugin(org_ctx["org"], org_ctx["user"])
    plugin_id = payload["id"]
    service_id = uuid.UUID(str(payload["credential_requirements"][0]["service_id"]))
    PluginService().set_org_activation(
        plugin_id, org_id=org_ctx["org"].id, user=org_ctx["user"], active=True
    )
    # No workspace activation yet -> no effective plugins.
    snapshot = plugin_runtime.build_workspace_plugin_snapshot(
        workspace=org_ctx["workspace"], org_id=org_ctx["org"].id
    )
    assert snapshot.plugins == ()

    credential = CredentialSvc().create_org_credential(
        organization_id=org_ctx["org"].id,
        service_id=service_id,
        name="tok",
        value="secret-value",
        user=org_ctx["user"],
    )
    org_ctx["workspace"].credentials.add(credential)
    PluginService().set_workspace_plugins(
        workspace=org_ctx["workspace"],
        org_id=org_ctx["org"].id,
        user=org_ctx["user"],
        plugin_ids=[plugin_id],
    )
    snapshot = plugin_runtime.build_workspace_plugin_snapshot(
        workspace=org_ctx["workspace"], org_id=org_ctx["org"].id
    )
    assert [p.slug for p in snapshot.plugins] == ["p"]
    assert len(snapshot.plugins[0].skills) == 1
    assert len(snapshot.plugins[0].mcp_servers) == 1
    assert snapshot.plugin_skills and "guide" in snapshot.plugin_skills[0].lower()


@pytest.mark.django_db
def test_org_deactivate_makes_workspace_activation_ineffective(org_ctx):
    payload = _make_plugin(org_ctx["org"], org_ctx["user"])
    plugin_id = payload["id"]
    service_id = uuid.UUID(str(payload["credential_requirements"][0]["service_id"]))
    PluginService().set_org_activation(
        plugin_id, org_id=org_ctx["org"].id, user=org_ctx["user"], active=True
    )
    credential = CredentialSvc().create_org_credential(
        organization_id=org_ctx["org"].id,
        service_id=service_id,
        name="tok",
        value="secret-value",
        user=org_ctx["user"],
    )
    org_ctx["workspace"].credentials.add(credential)
    PluginService().set_workspace_plugins(
        workspace=org_ctx["workspace"],
        org_id=org_ctx["org"].id,
        user=org_ctx["user"],
        plugin_ids=[plugin_id],
    )
    PluginService().set_org_activation(
        plugin_id, org_id=org_ctx["org"].id, user=org_ctx["user"], active=False
    )
    snapshot = plugin_runtime.build_workspace_plugin_snapshot(
        workspace=org_ctx["workspace"], org_id=org_ctx["org"].id
    )
    assert snapshot.plugins == ()


@pytest.mark.django_db
def test_foreign_plugin_rows_ignored(org_ctx):
    other_org = Organization.objects.create(
        name=f"Other {uuid.uuid4().hex[:6]}", slug=f"other-{uuid.uuid4().hex[:10]}"
    )
    Membership.objects.create(
        user=org_ctx["user"], organization=other_org, role=MembershipRole.ADMIN
    )
    payload = _make_plugin(other_org, org_ctx["user"], name="Foreign", slug="foreign")
    plugin_id = payload["id"]
    PluginService().set_org_activation(
        plugin_id, org_id=other_org.id, user=org_ctx["user"], active=True
    )
    snapshot = plugin_runtime.build_workspace_plugin_snapshot(
        workspace=org_ctx["workspace"], org_id=org_ctx["org"].id
    )
    assert all(str(p.id) != str(plugin_id) for p in snapshot.plugins)


@pytest.mark.django_db
def test_personal_and_org_credential_templates(org_ctx):
    payload = _make_plugin(org_ctx["org"], org_ctx["user"])
    plugin_id = payload["id"]
    service_id = uuid.UUID(str(payload["credential_requirements"][0]["service_id"]))
    PluginService().set_org_activation(
        plugin_id, org_id=org_ctx["org"].id, user=org_ctx["user"], active=True
    )
    credential = CredentialSvc().create_personal_credential(
        service_id=service_id,
        name="personal",
        value="personal-secret",
        user=org_ctx["user"],
        org_id=org_ctx["org"].id,
    )
    org_ctx["workspace"].credentials.add(credential)
    PluginService().set_workspace_plugins(
        workspace=org_ctx["workspace"],
        org_id=org_ctx["org"].id,
        user=org_ctx["user"],
        plugin_ids=[plugin_id],
    )
    snapshot = plugin_runtime.build_workspace_plugin_snapshot(
        workspace=org_ctx["workspace"], org_id=org_ctx["org"].id
    )
    plugin = snapshot.plugins[0]
    resolved = plugin_runtime.resolve_runtime_credentials(
        snapshot, workspace=org_ctx["workspace"]
    )
    assert resolved[plugin.id]["api_key"] == "personal-secret"
    env, _headers = plugin_runtime.render_server_config(
        plugin, plugin.mcp_servers[0], resolved[plugin.id]
    )
    assert env == {"TOKEN": "personal-secret"}


@pytest.mark.django_db
def test_missing_required_credential_fails_fast(org_ctx):
    payload = _make_plugin(org_ctx["org"], org_ctx["user"])
    plugin_id = payload["id"]
    PluginService().set_org_activation(
        plugin_id, org_id=org_ctx["org"].id, user=org_ctx["user"], active=True
    )
    # Bypass the attach-time guard: create the workspace activation row
    # directly so runtime resolution sees the gap.
    plugin = Plugin.objects.get(id=plugin_id)
    org_ctx["workspace"].plugin_activations.create(plugin=plugin)
    snapshot = plugin_runtime.build_workspace_plugin_snapshot(
        workspace=org_ctx["workspace"], org_id=org_ctx["org"].id
    )
    assert len(snapshot.plugins) == 1
    with pytest.raises(plugin_runtime.PluginCredentialConfigError, match="api_key"):
        plugin_runtime.resolve_runtime_credentials(
            snapshot, workspace=org_ctx["workspace"]
        )


@pytest.mark.django_db
def test_optional_placeholder_referenced_but_missing_is_config_error(org_ctx):
    created = PluginService().create_org_plugin(
        org_id=org_ctx["org"].id,
        user=org_ctx["user"],
        name="Opt",
        slug="opt",
        skills=[],
        mcp_servers=[
            {
                "name": "Tools",
                "transport": "stdio",
                "command": "npx",
                "env": {"OPTIONAL": "prefix-{{credential.opt_key}}-suffix"},
            }
        ],
        credential_requirements=[
            {
                "key": "opt_key",
                "required": False,
                "credential_service": {
                    "name": "Opt Service",
                    "credential_type": "env",
                    "env_var_name": "OPT_TOKEN",
                },
            }
        ],
    )
    plugin_id = created["id"]
    PluginService().set_org_activation(
        plugin_id, org_id=org_ctx["org"].id, user=org_ctx["user"], active=True
    )
    plugin = Plugin.objects.get(id=plugin_id)
    org_ctx["workspace"].plugin_activations.create(plugin=plugin)
    snapshot = plugin_runtime.build_workspace_plugin_snapshot(
        workspace=org_ctx["workspace"], org_id=org_ctx["org"].id
    )
    resolved = plugin_runtime.resolve_runtime_credentials(
        snapshot, workspace=org_ctx["workspace"]
    )
    assert resolved[plugin_id] == {}
    with pytest.raises(plugin_runtime.PluginCredentialConfigError, match="opt_key"):
        plugin_runtime.render_server_config(
            snapshot.plugins[0], snapshot.plugins[0].mcp_servers[0], {}
        )


@pytest.mark.django_db
def test_unknown_placeholder_key_is_config_error(org_ctx):
    created = PluginService().create_org_plugin(
        org_id=org_ctx["org"].id,
        user=org_ctx["user"],
        name="Bad",
        slug="bad",
        skills=[],
        mcp_servers=[
            {
                "name": "Tools",
                "transport": "stdio",
                "command": "npx",
                "env": {"TOKEN": "{{credential.nope}}"},
            }
        ],
    )
    plugin_id = created["id"]
    PluginService().set_org_activation(
        plugin_id, org_id=org_ctx["org"].id, user=org_ctx["user"], active=True
    )
    plugin = Plugin.objects.get(id=plugin_id)
    org_ctx["workspace"].plugin_activations.create(plugin=plugin)
    snapshot = plugin_runtime.build_workspace_plugin_snapshot(
        workspace=org_ctx["workspace"], org_id=org_ctx["org"].id
    )
    with pytest.raises(plugin_runtime.PluginCredentialConfigError, match="nope"):
        plugin_runtime.render_server_config(
            snapshot.plugins[0], snapshot.plugins[0].mcp_servers[0], {}
        )


@pytest.mark.django_db
def test_literals_preserved_and_unused_mapping_validated(org_ctx):
    created = PluginService().create_org_plugin(
        org_id=org_ctx["org"].id,
        user=org_ctx["user"],
        name="Lit",
        slug="lit",
        skills=[],
        mcp_servers=[
            {
                "name": "HTTP",
                "transport": "streamable_http",
                "url": "http://localhost:9000/mcp",
                "env": {"IGNORED": "plain"},
                "headers": {"X-Mode": "mode-{{credential.req_key}}-end"},
            }
        ],
        credential_requirements=[
            {
                "key": "req_key",
                "required": True,
                "credential_service": {
                    "name": "Lit Service",
                    "credential_type": "env",
                    "env_var_name": "LIT_TOKEN",
                },
            }
        ],
    )
    service_id = uuid.UUID(str(created["credential_requirements"][0]["service_id"]))
    credential = CredentialSvc().create_org_credential(
        organization_id=org_ctx["org"].id,
        service_id=service_id,
        name="lit",
        value="lit-secret",
        user=org_ctx["user"],
    )
    org_ctx["workspace"].credentials.add(credential)
    snapshot = plugin_runtime.build_workspace_plugin_snapshot(
        workspace=org_ctx["workspace"], org_id=org_ctx["org"].id
    )
    assert snapshot.plugins == ()
    PluginService().set_org_activation(
        created["id"],
        org_id=org_ctx["org"].id,
        user=org_ctx["user"],
        active=True,
    )
    plugin = Plugin.objects.get(id=created["id"])
    org_ctx["workspace"].plugin_activations.create(plugin=plugin)
    snapshot = plugin_runtime.build_workspace_plugin_snapshot(
        workspace=org_ctx["workspace"], org_id=org_ctx["org"].id
    )
    resolved = plugin_runtime.resolve_runtime_credentials(
        snapshot, workspace=org_ctx["workspace"]
    )
    env, headers = plugin_runtime.render_server_config(
        snapshot.plugins[0],
        snapshot.plugins[0].mcp_servers[0],
        resolved[snapshot.plugins[0].id],
    )
    assert env == {"IGNORED": "plain"}
    assert headers == {"X-Mode": "mode-lit-secret-end"}


@pytest.mark.django_db
def test_no_secret_values_in_snapshot(caplog, org_ctx):
    payload = _make_plugin(org_ctx["org"], org_ctx["user"])
    plugin_id = payload["id"]
    service_id = uuid.UUID(str(payload["credential_requirements"][0]["service_id"]))
    PluginService().set_org_activation(
        plugin_id, org_id=org_ctx["org"].id, user=org_ctx["user"], active=True
    )
    credential = CredentialSvc().create_org_credential(
        organization_id=org_ctx["org"].id,
        service_id=service_id,
        name="tok",
        value="top-secret-value",
        user=org_ctx["user"],
    )
    org_ctx["workspace"].credentials.add(credential)
    PluginService().set_workspace_plugins(
        workspace=org_ctx["workspace"],
        org_id=org_ctx["org"].id,
        user=org_ctx["user"],
        plugin_ids=[plugin_id],
    )
    with caplog.at_level("INFO"):
        snapshot = plugin_runtime.build_workspace_plugin_snapshot(
            workspace=org_ctx["workspace"], org_id=org_ctx["org"].id
        )
        plugin_runtime.resolve_runtime_credentials(
            snapshot, workspace=org_ctx["workspace"]
        )
    assert "top-secret-value" not in repr(snapshot)
    assert "top-secret-value" not in caplog.text
    assert isinstance(snapshot.plugins[0].requirements[0].service_id, uuid.UUID)
    assert not isinstance(CredentialService.objects.get(id=service_id), str)


@pytest.mark.django_db
def test_prepared_runtime_repr_hides_plaintexts(org_ctx):
    """PreparedPluginRuntime repr must never carry decrypted secrets."""
    from apps.plugins.runtime_snapshot import PreparedPluginRuntime

    payload = _make_plugin(org_ctx["org"], org_ctx["user"])
    plugin_id = payload["id"]
    service_id = uuid.UUID(str(payload["credential_requirements"][0]["service_id"]))
    PluginService().set_org_activation(
        plugin_id, org_id=org_ctx["org"].id, user=org_ctx["user"], active=True
    )
    credential = CredentialSvc().create_org_credential(
        organization_id=org_ctx["org"].id,
        service_id=service_id,
        name="tok",
        value="prepared-top-secret",
        user=org_ctx["user"],
    )
    org_ctx["workspace"].credentials.add(credential)
    PluginService().set_workspace_plugins(
        workspace=org_ctx["workspace"],
        org_id=org_ctx["org"].id,
        user=org_ctx["user"],
        plugin_ids=[plugin_id],
    )
    snapshot = plugin_runtime.build_workspace_plugin_snapshot(
        workspace=org_ctx["workspace"], org_id=org_ctx["org"].id
    )
    resolved = plugin_runtime.resolve_runtime_credentials(
        snapshot, workspace=org_ctx["workspace"]
    )
    prepared = PreparedPluginRuntime(
        snapshot=snapshot,
        workspace=org_ctx["workspace"],
        plaintexts=dict(resolved),
    )
    assert "prepared-top-secret" not in repr(prepared)
    assert "prepared-top-secret" not in repr(prepared.snapshot)
    assert prepared.plaintexts_for(plugin_id)["api_key"] == "prepared-top-secret"


@pytest.mark.django_db
def test_stale_workspace_org_relation_fails_closed_without_query(org_ctx):
    """A workspace without a loaded runner relation fails closed (no ORM)."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    payload = _make_plugin(org_ctx["org"], org_ctx["user"])
    plugin_id = payload["id"]
    PluginService().set_org_activation(
        plugin_id, org_id=org_ctx["org"].id, user=org_ctx["user"], active=True
    )
    from apps.runners.models import Workspace as _Workspace

    bare = _Workspace(id=org_ctx["workspace"].id)
    with CaptureQueriesContext(connection) as queries:
        with pytest.raises(plugin_runtime.PluginCredentialConfigError):
            plugin_runtime.build_workspace_plugin_snapshot(
                workspace=bare, org_id=org_ctx["org"].id
            )
    assert queries.captured_queries == []
