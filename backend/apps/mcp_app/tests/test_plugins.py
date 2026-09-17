"""MCP access-control tests for the plugin control-plane tools."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model

from apps.accounts.models import APIKeyPermission
from apps.mcp_app.server import (
    _TOOL_HANDLERS,
    _TOOL_PERMISSIONS,
    _TOOLS,
    _call_list_workspace_plugins,
    _call_set_workspace_plugins,
    _call_toggle_org_plugin_activation,
    _plugin_payload,
)
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.models import Runner, Workspace
from common.utils import hash_token


def _perm_key(user, *perms: APIKeyPermission) -> SimpleNamespace:
    values = {p.value for p in perms}

    class _Key:
        def has_permission(self, permission) -> bool:
            value = getattr(permission, "value", permission)
            return value in values

    key = _Key()
    key.user = user
    return key  # type: ignore[return-value]


def _setup():
    user_model = get_user_model()
    admin = user_model.objects.create_user(
        email=f"plug-mcp-admin-{uuid.uuid4().hex[:6]}@example.com",
        password="secret",
    )
    member = user_model.objects.create_user(
        email=f"plug-mcp-member-{uuid.uuid4().hex[:6]}@example.com",
        password="secret",
    )
    org = Organization.objects.create(
        name=f"Plug MCP {uuid.uuid4().hex[:6]}",
        slug=f"plug-mcp-{uuid.uuid4().hex[:8]}",
    )
    Membership.objects.create(user=admin, organization=org, role=MembershipRole.ADMIN)
    Membership.objects.create(
        user=member, organization=org, role=MembershipRole.MEMBER
    )
    runner = Runner.objects.create(
        name="plug-mcp-runner",
        api_token_hash=hash_token(f"plug-mcp-{uuid.uuid4().hex}"),
        organization=org,
    )
    workspace = Workspace.objects.create(
        runner=runner, name="plug-mcp-ws", created_by=member
    )
    return {"admin": admin, "member": member, "org": org, "workspace": workspace}


def _text(result) -> str:
    assert len(result) == 1
    return result[0].text


def test_plugin_tools_registered_with_expected_permissions() -> None:
    names = {tool.name for tool in _TOOLS if "plugin" in tool.name}
    assert names == {
        "list_plugins",
        "toggle_org_plugin_activation",
        "list_workspace_plugins",
        "set_workspace_plugins",
    }
    assert _TOOL_PERMISSIONS["list_plugins"] == APIKeyPermission.PLUGINS_READ
    assert (
        _TOOL_PERMISSIONS["toggle_org_plugin_activation"]
        == APIKeyPermission.PLUGINS_WRITE
    )
    assert (
        _TOOL_PERMISSIONS["list_workspace_plugins"] == APIKeyPermission.PLUGINS_READ
    )
    assert _TOOL_PERMISSIONS["set_workspace_plugins"] == APIKeyPermission.PLUGINS_WRITE
    for name in (
        "list_plugins",
        "toggle_org_plugin_activation",
        "list_workspace_plugins",
        "set_workspace_plugins",
    ):
        assert name in _TOOL_HANDLERS
    for tool in _TOOLS:
        if "plugin" not in tool.name:
            continue
        assert tool.inputSchema.get("additionalProperties") is False


def test_plugin_tool_visibility_follows_api_key_permissions() -> None:
    read_names = [
        tool.name
        for tool in _TOOLS
        if tool.name.startswith("list_")
        and "plugin" in tool.name
        or tool.name in ("list_plugins", "list_workspace_plugins")
    ]
    assert set(read_names) == {"list_plugins", "list_workspace_plugins"}


@pytest.mark.django_db
def test_plugin_payload_carries_no_secrets() -> None:
    from apps.plugins.services import PluginService

    ctx = _setup()
    created = PluginService().create_org_plugin(
        org_id=ctx["org"].id,
        user=ctx["admin"],
        name="Secret Payload",
        slug="secret-payload",
        skills=[{"name": "Guide", "body": "# guide", "position": 0}],
        mcp_servers=[
            {
                "name": "Tools",
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "thing"],
                "env": {"TOKEN": "{{credential.api_key}}"},
                "headers": {"X-T": "prefix-{{credential.api_key}}"},
            }
        ],
        credential_requirements=[
            {
                "key": "api_key",
                "required": True,
                "credential_service": {
                    "name": "Payload Service",
                    "credential_type": "env",
                    "env_var_name": "PAYLOAD_TOKEN",
                },
            }
        ],
    )
    payload = _plugin_payload(created)
    dumped = json.dumps(payload)
    assert "{{credential.api_key}}" not in dumped  # templates never leave the API
    assert "PAYLOAD_TOKEN" not in dumped  # no injection metadata beyond slugs
    for server in payload["mcp_servers"]:
        assert set(server) == {"id", "name", "slug", "transport"}
    assert set(payload["credential_requirements"][0]) == {
        "id",
        "key",
        "required",
        "service_id",
        "service_slug",
    }


@pytest.mark.django_db
def test_mcp_toggle_requires_admin_and_effective_plugin() -> None:
    from apps.plugins.services import PluginService

    ctx = _setup()
    created = PluginService().create_org_plugin(
        org_id=ctx["org"].id,
        user=ctx["admin"],
        name="Toggle",
        slug="toggle",
        skills=[],
        mcp_servers=[],
    )
    plugin_id = str(created["id"])
    member_key = _perm_key(ctx["member"], APIKeyPermission.PLUGINS_WRITE)
    assert "Admin role required" in _text(
        _call_toggle_org_plugin_activation(
            member_key, ctx["org"].id, {"plugin_id": plugin_id, "active": True}
        )
    )
    admin_key = _perm_key(ctx["admin"], APIKeyPermission.PLUGINS_WRITE)
    PluginService().update_org_plugin(
        created["id"], org_id=ctx["org"].id, user=ctx["admin"], published=False
    )
    assert "not available" in _text(
        _call_toggle_org_plugin_activation(
            admin_key, ctx["org"].id, {"plugin_id": plugin_id, "active": True}
        )
    )


@pytest.mark.django_db
def test_mcp_workspace_plugin_flow_is_owner_scoped() -> None:
    from apps.plugins.services import PluginService

    ctx = _setup()
    created = PluginService().create_org_plugin(
        org_id=ctx["org"].id,
        user=ctx["admin"],
        name="Flow",
        slug="flow",
        skills=[],
        mcp_servers=[],
    )
    plugin_id = str(created["id"])
    PluginService().set_org_activation(
        created["id"], org_id=ctx["org"].id, user=ctx["admin"], active=True
    )
    member_key = _perm_key(ctx["member"], APIKeyPermission.PLUGINS_WRITE)
    listed = json.loads(
        _text(
            _call_list_workspace_plugins(
                member_key,
                ctx["org"].id,
                {"workspace_id": str(ctx["workspace"].id)},
            )
        )
    )
    assert any(p["id"] == plugin_id for p in listed)
    updated = json.loads(
        _text(
            _call_set_workspace_plugins(
                member_key,
                ctx["org"].id,
                {
                    "workspace_id": str(ctx["workspace"].id),
                    "plugin_ids": [plugin_id],
                },
            )
        )
    )
    assert any(p["id"] == plugin_id and p["workspace_enabled"] for p in updated)

    # A stranger (no workspace ownership) gets an error, not the data.
    stranger_model = get_user_model()
    stranger = stranger_model.objects.create_user(
        email=f"plug-mcp-stranger-{uuid.uuid4().hex[:6]}@example.com",
        password="secret",
    )
    Membership.objects.create(
        user=stranger, organization=ctx["org"], role=MembershipRole.MEMBER
    )
    stranger_key = _perm_key(stranger, APIKeyPermission.PLUGINS_WRITE)
    assert "Workspace not found" in _text(
        _call_list_workspace_plugins(
            stranger_key, ctx["org"].id, {"workspace_id": str(ctx["workspace"].id)}
        )
    )
