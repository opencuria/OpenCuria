"""Plugin management and workspace tools."""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock

import pytest
from asgiref.sync import sync_to_async

from apps.credentials.models import CredentialService, OrgCredentialServiceActivation
from apps.credentials.services import CredentialSvc
from apps.plugins.models import Plugin
from apps.runners.enums import WorkspaceStatus
from apps.runners.models import Workspace
from apps.runners.services import get_runner_service
from apps.runners.services.domains.workspace_lifecycle import (
    WorkspaceLifecycleMixin,
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


async def test_admin_creates_activates_and_edits_a_plugin(world):
    await sync_to_async(_token_service)(world)
    admin = world.ctx(role="admin")

    created = await ok(
        admin,
        "plugin_create",
        name="Acme tools",
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
    )

    assert created["active_in_organization"] is True
    assert created["editable"] is True
    assert created["credential_requirements"][0]["service"] == "Acme token"
    assert created["mcp_servers"][0]["headers"] == {
        "Authorization": "Bearer {{credential.ACME_TOKEN}}"
    }

    catalog = await ok(world.ctx(), "plugin_list", query="acme")
    assert catalog["plugins"][0]["required_credentials"] == ["Acme token"]

    updated = await ok(
        admin,
        "plugin_update",
        plugin="acme-tools",
        skills=[{"name": "Acme usage", "body": "Use v3."}],
    )
    assert updated["skills"] == [
        {"name": "Acme usage", "slug": "acme-usage", "body": "Use v3."}
    ]
    assert len(updated["mcp_servers"]) == 1  # untouched lists stay

    member_error = await fails(world.ctx(), "plugin_delete", plugin="Acme tools")
    assert "Only organization admins" in member_error
    assert (await ok(admin, "plugin_delete", plugin="Acme tools"))["deleted"] is True


async def test_global_plugins_are_read_only(world):
    await sync_to_async(Plugin.objects.create)(
        name="Browser", slug=f"browser-{uuid.uuid4().hex[:6]}", description="Global"
    )

    error = await fails(
        world.ctx(role="admin"), "plugin_update", plugin="Browser", name="Mine"
    )

    assert "global plugin" in error


async def test_enable_plugin_in_a_workspace(world):
    admin = world.ctx(role="admin")
    await ok(
        admin,
        "plugin_create",
        name="Docs helper",
        skills=[{"name": "Docs", "body": "Write docs."}],
        activate=False,
    )
    ctx = world.ctx()

    not_active = await fails(
        ctx,
        "plugin_set_active",
        plugin="Docs helper",
        active=True,
        workspace="billing-api",
    )
    assert "not activated for the organization" in not_active

    await ok(admin, "plugin_set_active", plugin="Docs helper", active=True)
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
    assert [p["name"] for p in detail["plugins"] if p["enabled"]] == ["Docs helper"]

    disabled = await ok(
        ctx,
        "plugin_set_active",
        plugin="Docs helper",
        active=False,
        workspace="billing-api",
    )
    assert disabled["enabled"] is False


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


async def test_power_tools_are_idempotent_and_delete_dispatches(world, monkeypatch):
    calls = []

    async def remove(self, workspace_id):
        calls.append(("remove", workspace_id))

    async def stop(self, workspace_id, **kwargs):
        calls.append(("stop", workspace_id))

    monkeypatch.setattr(WorkspaceLifecycleMixin, "remove_workspace", remove)
    monkeypatch.setattr(WorkspaceLifecycleMixin, "stop_workspace", stop)
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
    monkeypatch.setattr(get_runner_service(), "_emit_to_runner", emit)
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
    call_runner = AsyncMock(return_value={"ok": True, "credentials_present": True})
    monkeypatch.setattr(get_runner_service(), "_call_runner", call_runner)
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
        for call in call_runner.await_args_list
    ]
    assert injected == [{"ACME_TOKEN": "update-secret"}, {}]
