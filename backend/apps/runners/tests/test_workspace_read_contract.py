"""Characterize workspace read responses before moving them out of REST."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.models import APIKeyPermission
from apps.credentials.models import Credential, CredentialService
from apps.organizations.models import Membership, MembershipRole
from apps.plugins.models import Plugin, WorkspacePluginActivation
from apps.runners.models import ImageDefinition, ImageInstance
from apps.runners.tests.test_image_versions import make_line
from apps.runners.tests.test_image_versions_api import headers

pytestmark = pytest.mark.django_db
OBSERVED_AT = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)


@pytest.fixture
def auth(user, organization) -> dict[str, str]:
    """Authorize the workspace owner to read workspace metadata."""
    Membership.objects.create(
        user=user, organization=organization, role=MembershipRole.MEMBER
    )
    return headers(user, organization, [APIKeyPermission.WORKSPACES_READ])


def _read_both(workspace, auth: dict[str, str]) -> dict:
    """List and detail must expose the same workspace representation."""
    client = Client()
    detail = client.get(f"/api/v1/workspaces/{workspace.id}/", **auth)
    listing = client.get("/api/v1/workspaces/", **auth)
    assert detail.status_code == listing.status_code == 200
    [listed] = listing.json()
    assert listed == detail.json()
    return listed


def test_workspace_selection_metadata_is_ordered_and_never_decrypted(
    workspace, user, auth
) -> None:
    """Include stored selections, hide internal accounts, and preserve order."""
    credentials = []
    for kind in ("env", "provider_secret", "mcp_oauth"):
        service = CredentialService.objects.create(
            name=kind, slug=f"read-{uuid.uuid4().hex}", credential_type=kind
        )
        credentials.append(
            Credential.objects.create(
                user=user,
                service=service,
                name=kind,
                # Reading metadata must work even without a decryptable value.
                encrypted_value="opaque-value-that-cannot-be-decrypted",
            )
        )
    workspace.credentials.set(credentials)
    plugins = [
        Plugin.objects.create(name=name, slug=f"read-{uuid.uuid4().hex}", enabled=False)
        for name in ("Z first selected", "A last selected")
    ]
    for plugin in plugins:
        WorkspacePluginActivation.objects.create(
            workspace=workspace, plugin=plugin, enabled_by=user
        )

    data = _read_both(workspace, auth)

    assert data["credential_ids"] == [str(credentials[2].id), str(credentials[0].id)]
    assert data["plugin_ids"] == [str(plugin.id) for plugin in reversed(plugins)]
    assert "opaque-value" not in str(data)
    assert data["credentials_present"] is False


@pytest.mark.parametrize(
    ("status", "timeout"),
    [("running", 30), ("stopped", 30), ("running", None), ("running", 0)],
)
def test_workspace_presence_auto_stop_and_optional_metadata(
    workspace, organization, auth, status: str, timeout: int | None
) -> None:
    """Auto-stop is advertised only for a running workspace with a timeout."""
    organization.workspace_auto_stop_timeout_minutes = timeout
    organization.save(update_fields=["workspace_auto_stop_timeout_minutes"])
    workspace.status = status
    workspace.last_activity_at = OBSERVED_AT
    workspace.disk_used_bytes = 1
    workspace.disk_free_bytes = 2
    workspace.disk_total_bytes = 3
    workspace.disk_usage_observed_at = OBSERVED_AT
    workspace.repos = ["https://example.test/repository.git"]
    workspace.delete_last_error = "last removal failed"
    workspace.delete_attempt_count = 2
    workspace.save()

    data = _read_both(workspace, auth)

    assert data["runner_online"] is True
    assert data["auto_stop_timeout_minutes"] == timeout
    expected = OBSERVED_AT + timedelta(minutes=timeout or 0)
    assert data["auto_stop_at"] == (
        expected.isoformat().replace("+00:00", "Z")
        if status == "running" and timeout
        else None
    )
    assert data["disk_usage"] == {
        "used_bytes": 1,
        "free_bytes": 2,
        "total_bytes": 3,
        "observed_at": OBSERVED_AT.isoformat().replace("+00:00", "Z"),
    }
    assert data["delete_last_error"] == "last removal failed"
    assert data["delete_attempt_count"] == 2
    assert data["repos"] == workspace.repos
    assert data["base_image"] is data["pending_base_image"] is None
    assert data["base_image_name"] is None
    assert data["credential_ids"] == data["plugin_ids"] == []


def test_workspace_base_and_pending_image_versions(workspace, user, runner, auth):
    """Current/pending versions and update availability retain their meaning."""
    _, (base, latest) = make_line(runner, user, ("ready", "ready"))
    workspace.base_image_instance = base
    workspace.pending_base_image_instance = latest
    workspace.save()

    data = _read_both(workspace, auth)

    assert data["base_image_name"] == base.name
    assert data["base_image"]["id"] == str(base.id)
    assert data["base_image"]["latest_id"] == str(latest.id)
    assert data["base_image"]["update_available"] is True
    assert data["pending_base_image"]["id"] == str(latest.id)
    assert data["pending_base_image"]["update_available"] is False


def test_workspace_base_image_name_prefers_recipe(workspace, auth):
    """A legacy recipe image keeps its recipe's user-facing name."""
    definition = ImageDefinition.objects.create(name="Recipe name")
    image = ImageInstance.objects.create(
        is_legacy=True,
        runner=workspace.runner,
        origin_type="definition_build",
        origin_definition=definition,
        name="Immutable build name",
        status="ready",
    )
    workspace.base_image_instance = image
    workspace.save()

    data = _read_both(workspace, auth)

    assert data["base_image_name"] == "Recipe name"
    assert data["base_image"]["line_kind"] is None


def test_workspace_read_keeps_owner_and_permission_boundaries(
    workspace, organization, auth
) -> None:
    """Foreign workspaces stay hidden and a missing API permission stays 403."""
    other = get_user_model().objects.create_user(email="read-other@example.test")
    Membership.objects.create(
        user=other, organization=organization, role=MembershipRole.MEMBER
    )
    foreign = headers(other, organization, [APIKeyPermission.WORKSPACES_READ])
    client = Client()
    assert (
        client.get(f"/api/v1/workspaces/{workspace.id}/", **foreign).status_code == 404
    )
    assert client.get("/api/v1/workspaces/", **foreign).json() == []
    denied = headers(workspace.created_by, organization, [])
    assert (
        client.get(f"/api/v1/workspaces/{workspace.id}/", **denied).status_code == 403
    )
