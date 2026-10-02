from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from apps.accounts.models import APIKeyPermission
from apps.mcp_app.server import _call_create_workspace
from apps.organizations.models import Organization


def _key(*permissions: APIKeyPermission) -> SimpleNamespace:
    allowed = {permission.value for permission in permissions}

    class Key:
        user = object()

        @staticmethod
        def has_permission(permission) -> bool:
            return getattr(permission, "value", permission) in allowed

    return Key()


@pytest.mark.django_db
def test_create_workspace_empty_plugin_ids_does_not_require_plugins_write(
    monkeypatch,
):
    organization = Organization.objects.create(
        name="MCP permission org", slug=f"mcp-permission-{uuid.uuid4().hex[:8]}"
    )
    from apps.credentials.services import CredentialSvc, ResolvedCredentials

    class FakeRunnerService:
        async def create_workspace(self, **kwargs):
            assert kwargs["plugin_ids"] == []
            return (
                SimpleNamespace(id="workspace-id", status="creating"),
                SimpleNamespace(id="task-id"),
            )

    monkeypatch.setattr(
        CredentialSvc,
        "resolve_credentials",
        lambda self, *_args, **_kwargs: ResolvedCredentials(),
    )
    monkeypatch.setattr(
        "apps.runners.sio_server.get_runner_service", lambda: FakeRunnerService()
    )
    monkeypatch.setattr(
        "apps.plugins.repositories.WorkspacePluginActivationRepository.enabled_plugin_ids",
        lambda _workspace_id: set(),
    )

    result = _call_create_workspace(
        _key(APIKeyPermission.WORKSPACES_CREATE),
        organization.id,
        {"name": "No plugins", "plugin_ids": []},
    )

    payload = json.loads(result[0].text)
    assert payload["workspace_id"] == "workspace-id"
    assert payload["plugin_ids"] == []


@pytest.mark.django_db
def test_create_workspace_nonempty_plugin_ids_still_require_plugins_write():
    organization = Organization.objects.create(
        name="MCP permission org", slug=f"mcp-permission-{uuid.uuid4().hex[:8]}"
    )
    result = _call_create_workspace(
        _key(APIKeyPermission.WORKSPACES_CREATE),
        organization.id,
        {"name": "With plugin", "plugin_ids": ["00000000-0000-0000-0000-000000000001"]},
    )

    assert "plugins:write required" in result[0].text
