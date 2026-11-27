"""Plugin management and workspace tools."""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock

import pytest
from asgiref.sync import sync_to_async

from apps.accounts.models import User
from apps.credentials.models import CredentialService, OrgCredentialServiceActivation
from apps.credentials.services import CredentialSvc
from apps.organizations.models import Membership, MembershipRole
from apps.plugins.models import OrgPluginActivation, Plugin
from apps.runners.enums import WorkspaceStatus
from apps.runners.models import ImageDefinition, ImageInstance, Workspace
from apps.runners.services import get_runner_service
from apps.runners.services.domains.workspace_lifecycle import (
    WorkspaceLifecycleService,
)
from apps.runners.tests.test_image_versions import make_line

from .conftest import fails, ok

pytestmark = pytest.mark.django_db(transaction=True)


def _token_service(world):
    return CredentialService.objects.create(
        organization=world.org,
        name="Acme token",
        slug=f"acme-token-{uuid.uuid4().hex[:6]}",
        credential_type="env",
        env_var_name="ACME_TOKEN",
    )


async def test_admin_creates_releases_and_edits_an_organization_plugin(world):
    await sync_to_async(_token_service)(world)
    admin = world.ctx(role="admin")

    created = await ok(
        admin,
        "plugin_create",
        name="Acme tools",
        scope="organization",
        description="Acme API helpers.",
        skills=[{"name": "Acme usage", "body": "Prefer the v2 API."}],
        mcp_servers=[
            {
                "name": "Acme",
                "transport": "streamable_http",
                "url": "https://mcp.acme.test/mcp",
                "headers": {"Authorization": "Bearer {{credential.ACME_TOKEN}}"},
            }
        ],
        credential_requirements=[{"key": "ACME_TOKEN", "service": "Acme token"}],
        enable=False,
    )

    assert created["scope"] == "organization"
    assert created["active_in_organization"] is True  # auto-released
    assert created["editable"] is True
    assert "workspace" not in created
    assert created["credential_requirements"][0]["service"] == "Acme token"
    assert created["mcp_servers"][0]["headers"] == {
        "Authorization": "Bearer {{credential.ACME_TOKEN}}"
    }
    skill_id = created["skills"][0]["skill_id"]

    catalog = await ok(world.ctx(), "plugin_list", query="acme")
    assert catalog["plugins"][0]["required_credentials"] == ["Acme token"]
    assert catalog["plugins"][0]["scope"] == "organization"
    assert catalog["plugins"][0]["enabled_here"] is False
    assert "editable" not in catalog["plugins"][0]

    updated = await ok(
        admin,
        "plugin_update",
        plugin="acme-tools",
        skills=[{"name": "Acme usage", "body": "Use v3."}],
    )
    # The skill keeps its ID (and thereby chat selections) across edits.
    assert updated["skills"] == [
        {
            "skill_id": skill_id,
            "name": "Acme usage",
            "description": "Use v3.",
            "body": "Use v3.",
        }
    ]
    assert len(updated["mcp_servers"]) == 1  # untouched lists stay

    # Members cannot change or delete organization plugins.
    for tool, extra in (("plugin_update", {"name": "Mine"}), ("plugin_delete", {})):
        error = await fails(world.ctx(), tool, plugin="Acme tools", **extra)
        assert "Create a personal plugin instead" in error
    assert (await ok(admin, "plugin_delete", plugin="Acme tools"))["deleted"] is True


async def test_builtin_plugins_are_read_only(world):
    def released_builtin() -> None:
        plugin = Plugin.objects.create(
            name="Browser", slug=f"browser-{uuid.uuid4().hex[:6]}", description="Global"
        )
        OrgPluginActivation.objects.create(organization=world.org, plugin=plugin)

    await sync_to_async(released_builtin)()

    for role in ("admin", "member"):
        error = await fails(
            world.ctx(role=role), "plugin_update", plugin="Browser", name="Mine"
        )
        assert "built-in plugin" in error
    admin_error = await fails(
        world.ctx(role="admin"), "plugin_delete", plugin="Browser"
    )
    assert "built-in plugin" in admin_error


async def test_member_creates_a_personal_plugin_enabled_in_the_workspace(world):
    ctx = world.ctx()

    created = await ok(
        ctx,
        "plugin_create",
        name="Acme",
        skills=[{"name": "Acme usage", "body": "Prefer the v2 API."}],
        mcp_servers=[
            {
                "name": "Acme",
                "command": "npx -y @acme/mcp --flag 'a b'",
                "secrets": {"ACME_API_KEY": "sk-member-secret"},
            }
        ],
    )

    assert created["scope"] == "personal"
    assert created["editable"] is True
    assert "active_in_organization" not in created
    server = created["mcp_servers"][0]
    assert server["command"] == "npx -y @acme/mcp --flag 'a b'"
    assert server["secrets"] == {"ACME_API_KEY": "credential 'Acme: ACME_API_KEY'"}
    assert "env" not in server
    assert "sk-member-secret" not in json.dumps(created)
    assert created["workspace"]["enabled"] is True
    assert created["workspace"]["current_workspace"] is True
    assert created["workspace"]["note"]

    def stored() -> tuple:
        plugin = Plugin.objects.get(id=created["plugin_id"])
        mcp = plugin.mcp_servers.get()
        return (
            plugin.owner_id,
            plugin.organization_id,
            list(
                world.workspace.plugin_activations.values_list("plugin_id", flat=True)
            ),
            mcp.command,
            mcp.args,
        )

    owner_id, org_id, enabled, command, args = await sync_to_async(stored)()
    assert (owner_id, org_id) == (world.user.id, None)
    assert [str(value) for value in enabled] == [created["plugin_id"]]
    assert (command, args) == ("npx", ["-y", "@acme/mcp", "--flag", "a b"])

    listed = await ok(ctx, "plugin_list", query="acme")
    assert listed["plugins"][0]["scope"] == "personal"
    assert listed["plugins"][0]["enabled_here"] is True

    # Updating with the same server name keeps the saved secret (null).
    updated = await ok(
        ctx,
        "plugin_update",
        plugin="Acme",
        mcp_servers=[
            {
                "name": "Acme",
                "command": "npx -y @acme/mcp@2",
                "secrets": {"ACME_API_KEY": None},
            }
        ],
    )
    assert updated["mcp_servers"][0]["secrets"] == server["secrets"]
    assert updated["mcp_servers"][0]["command"] == "npx -y @acme/mcp@2"

    def secret() -> tuple[str, str, int]:
        from apps.credentials.models import Credential
        from common.utils import decrypt_value

        requirement = (
            Plugin.objects.get(id=created["plugin_id"])
            .credential_requirements.select_related("credential")
            .get()
        )
        return (
            requirement.credential.name,
            decrypt_value(requirement.credential.encrypted_value),
            Credential.objects.filter(created_by=world.user).count(),
        )

    assert await sync_to_async(secret)() == (
        "Acme: ACME_API_KEY",
        "sk-member-secret",
        1,
    )

    # A new value replaces the stored one; a credential can be named instead.
    await ok(
        ctx,
        "plugin_update",
        plugin="Acme",
        mcp_servers=[
            {
                "name": "Acme",
                "command": "npx -y @acme/mcp@2",
                "secrets": {"ACME_API_KEY": "sk-rotated"},
            }
        ],
    )
    assert (await sync_to_async(secret)())[1:] == ("sk-rotated", 1)
    per_user = await ok(
        ctx,
        "plugin_update",
        plugin="Acme",
        mcp_servers=[
            {
                "name": "Acme",
                "command": "npx -y @acme/mcp@2",
                "secrets": {"ACME_API_KEY": {"per_user": True}},
            }
        ],
    )
    assert "each workspace" in per_user["mcp_servers"][0]["secrets"]["ACME_API_KEY"]
    assert (await ok(ctx, "plugin_delete", plugin="Acme"))["deleted"] is True


async def test_personal_plugins_are_private_to_their_owner(world):
    await ok(
        world.ctx(),
        "plugin_create",
        name="Private",
        skills=[{"name": "Notes", "body": "Mine."}],
        enable=False,
    )

    def make_member(role: str) -> User:
        user = User.objects.create_user(email=f"{role}-{uuid.uuid4().hex[:8]}@x.com")
        Membership.objects.create(user=user, organization=world.org, role=role)
        return user

    stranger = world.ctx(user=await sync_to_async(make_member)(MembershipRole.MEMBER))
    admin = world.ctx(
        user=await sync_to_async(make_member)(MembershipRole.ADMIN), role="admin"
    )
    for other in (stranger, admin):
        assert (await ok(other, "plugin_list", query="private"))["plugins"] == []
        for tool in ("plugin_get", "plugin_update", "plugin_delete"):
            error = await fails(other, tool, plugin="Private")
            assert "No plugin 'Private' found" in error

    # Personal plugins are never released organization-wide.
    owner_as_admin = world.ctx(role="admin")
    error = await fails(
        owner_as_admin,
        "plugin_set_active",
        plugin="Private",
        active=True,
        organization=True,
    )
    assert "Personal plugins" in error


async def test_enable_plugin_in_a_workspace(world):
    admin = world.ctx(role="admin")
    await ok(
        admin,
        "plugin_create",
        name="Docs helper",
        scope="organization",
        skills=[{"name": "Docs", "body": "Write docs."}],
        enable=False,
    )
    withdrawn = await ok(
        admin,
        "plugin_set_active",
        plugin="Docs helper",
        active=False,
        organization=True,
    )
    assert withdrawn["active_in_organization"] is False
    ctx = world.ctx()

    # Members only see organization plugins released for the organization.
    not_active = await fails(
        ctx,
        "plugin_set_active",
        plugin="Docs helper",
        active=True,
        workspace="billing-api",
    )
    assert "No plugin 'Docs helper' found" in not_active
    assert (await ok(ctx, "plugin_list", query="docs"))["plugins"] == []
    member_release = await fails(
        ctx, "plugin_set_active", plugin="Docs helper", active=True, organization=True
    )
    assert "'organization' was unexpected" in member_release

    await ok(
        admin, "plugin_set_active", plugin="Docs helper", active=True, organization=True
    )
    enabled = await ok(
        ctx,
        "plugin_set_active",
        plugin="Docs helper",
        active=True,
        workspace="billing-api",
    )
    listed = await ok(ctx, "plugin_list", workspace="billing-api", query="docs")
    detail = await ok(ctx, "workspace_get", workspace="billing-api")

    assert enabled["enabled"] is True and enabled["ready"] is True
    assert listed["plugins"][0]["enabled"] is True
    assert listed["plugins"][0]["scope"] == "organization"
    assert [p["name"] for p in detail["plugins"] if p["enabled"]] == ["Docs helper"]

    # Without a workspace the current workspace is used.
    disabled = await ok(ctx, "plugin_set_active", plugin="Docs helper", active=False)
    assert disabled["enabled"] is False
    assert disabled["current_workspace"] is True


async def test_workspace_list_get_and_rename(world):
    ctx = world.ctx()

    listed = await ok(ctx, "workspace_list", status="running")
    detail = await ok(ctx, "workspace_get", workspace="BILLING-API")
    renamed = await ok(ctx, "workspace_update", workspace="billing-api", name="billing")

    assert listed["workspaces"][0]["name"] == "billing-api"
    assert listed["workspaces"][0]["runner_online"] is True
    assert detail["runner"]["name"] == "local-qemu"
    assert detail["recent_chats"] == [] and detail["total_chats"] == 0
    assert renamed["name"] == "billing"
    assert renamed["updated_fields"] == ["name"]
    assert "Pass at least one field" in await fails(
        ctx, "workspace_update", workspace="billing"
    )


@pytest.mark.parametrize("recipe_name", ["Recipe name", ""])
async def test_workspace_summary_preserves_image_name_and_live_presence(
    world, recipe_name: str
) -> None:
    """MCP uses the recipe/image name and current runner state for discovery."""
    from apps.mcp_app.tools.workspaces import _summary

    def describe() -> dict:
        definition = ImageDefinition.objects.create(name=recipe_name)
        image = ImageInstance.objects.create(
            is_legacy=True,
            runner=world.runner,
            origin_type="definition_build",
            origin_definition=definition,
            name="Build name",
            status="ready",
        )
        world.workspace.base_image_instance = image
        # MCP historically reads the runner state even if an annotation differs.
        world.workspace.runner_is_online = False
        return _summary(world.workspace)

    data = await sync_to_async(describe)()

    assert data["image"] == (recipe_name or "Build name")
    assert data["runner_online"] is True
    assert "base_image_name" not in data


async def test_power_tools_are_idempotent_and_delete_dispatches(world, monkeypatch):
    calls = []

    async def remove(self, workspace_id):
        calls.append(("remove", workspace_id))

    async def stop(self, workspace_id, **kwargs):
        calls.append(("stop", workspace_id))

    monkeypatch.setattr(WorkspaceLifecycleService, "remove_workspace", remove)
    monkeypatch.setattr(WorkspaceLifecycleService, "stop_workspace", stop)
    ctx = world.ctx()

    started = await ok(ctx, "workspace_start", workspace="billing-api")
    assert started["status"] == "running"
    assert "chats can be started" in started["next_step"]

    await ok(ctx, "workspace_stop", workspace="billing-api")
    world.workspace.status = WorkspaceStatus.STOPPED
    await sync_to_async(world.workspace.save)()
    await ok(ctx, "workspace_stop", workspace="billing-api")  # already stopped

    deleted = await ok(ctx, "workspace_delete", workspace="billing-api")
    assert deleted["deleted"] is True
    assert calls == [("stop", world.workspace.id), ("remove", world.workspace.id)]


async def test_workspace_create_without_ready_image_explains_next_step(world):
    error = await fails(world.ctx(), "workspace_create", name="new-one")

    assert "No image is ready" in error


def _selected_token(world, value: str):
    service = _token_service(world)
    OrgCredentialServiceActivation.objects.create(
        organization=world.org, credential_service=service
    )
    return CredentialSvc().create_personal_credential(
        service_id=service.id,
        name="Acme credential",
        value=value,
        user=world.user,
        org_id=world.org.id,
    )


async def test_workspace_create_attaches_credentials_by_name(world, monkeypatch):
    emit = AsyncMock()
    monkeypatch.setattr(get_runner_service().runner_transport, "emit_to_runner", emit)
    credential = await sync_to_async(_selected_token)(world, "create-secret")
    await sync_to_async(make_line)(world.runner, world.user, name="Captured base")

    created = await ok(
        world.ctx(),
        "workspace_create",
        name="with-credentials",
        credentials=["Acme credential"],
    )

    workspace = await sync_to_async(Workspace.objects.get)(pk=created["workspace_id"])
    attached = await sync_to_async(list)(
        workspace.credentials.values_list("id", flat=True)
    )
    assert attached == [credential.id]
    assert "create-secret" not in json.dumps(created, default=str)
    payload = next(arg for arg in emit.await_args.args if isinstance(arg, dict))
    assert payload["env_vars"] == {"ACME_TOKEN": "create-secret"}


async def test_workspace_update_replaces_credentials_by_name(world, monkeypatch):
    async def acknowledge(_event, payload, **_kwargs):
        return {"ok": True, "credentials_present": bool(payload["env_vars"])}

    transport = AsyncMock()
    transport.call.side_effect = acknowledge
    world.runner.sid = "mcp-credential-runner"
    await sync_to_async(world.runner.save)(update_fields=["sid"])
    monkeypatch.setattr(get_runner_service().runner_transport, "sio", transport)
    credential = await sync_to_async(_selected_token)(world, "update-secret")
    ctx = world.ctx()

    attached = await ok(
        ctx, "workspace_update", workspace="billing-api", credentials=[credential.name]
    )
    detached = await ok(
        ctx, "workspace_update", workspace="billing-api", credentials=[]
    )

    assert [c["name"] for c in attached["credentials"]] == ["Acme credential"]
    assert "credentials" not in detached
    injected = [
        next(arg for arg in call.args if isinstance(arg, dict))["env_vars"]
        for call in transport.call.await_args_list
    ]
    assert injected == [{"ACME_TOKEN": "update-secret"}, {}]


async def test_workspace_options_and_create_report_the_guest_os(world, monkeypatch):
    monkeypatch.setattr(
        get_runner_service().runner_transport, "emit_to_runner", AsyncMock()
    )

    def captured_lines() -> None:
        make_line(world.runner, world.user, name="Ubuntu base")
        _, (windows,) = make_line(world.runner, world.user, name="Windows base")
        ImageInstance.objects.filter(pk=windows.pk).update(
            guest_os="windows", min_disk_size_gb=80
        )
        from apps.licensing.models import WindowsLicensePolicy

        WindowsLicensePolicy.objects.update_or_create(
            organization_id=world.runner.organization_id,
            defaults={"mode": "unlicensed"},
        )

    await sync_to_async(captured_lines)()
    ctx = world.ctx()

    options = await ok(ctx, "workspace_options")
    created = await ok(ctx, "workspace_create", name="win", image="Windows base")

    images = {image["name"]: image for image in options["images"]}
    assert images["Ubuntu base"]["guest_os"] == "linux"
    assert "minimums" not in images["Ubuntu base"]
    assert images["Windows base"]["guest_os"] == "windows"
    assert images["Windows base"]["minimums"] == {
        "vcpus": 2,
        "memory_mb": 4096,
        "disk_gb": 80,
    }
    assert created["guest_os"] == "windows"
    workspace = await sync_to_async(Workspace.objects.get)(pk=created["workspace_id"])
    assert workspace.qemu_disk_size_gb == 80
    detail = await ok(ctx, "workspace_get", workspace=str(workspace.id))
    assert detail["image"]["guest_os"] == "windows"
    assert detail["resources"] == {"vcpus": 2, "memory_mb": 4096, "disk_gb": 80}
    too_small = await fails(
        ctx, "workspace_create", name="small", image="Windows base", memory_mb=2048
    )
    assert "Windows workspaces need at least 4096 MiB RAM" in too_small
