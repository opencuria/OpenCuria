"""Catalogue contracts and bounded SQL work for complete plugin definitions."""

from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.models import APIKey, APIKeyPermission
from apps.credentials.models import CredentialService
from apps.organizations.models import Membership, Organization
from apps.plugins.models import (
    OrgPluginActivation,
    Plugin,
    PluginCredentialRequirement,
    PluginMcpServer,
    PluginSkill,
)
from apps.plugins.repositories import OrgPluginActivationRepository
from apps.plugins.services import PluginService
from common.exceptions import NotFoundError
from common.utils import generate_api_token, hash_token


@pytest.fixture
def catalogue(db):
    """Isolate catalogue size from seeded global plugins."""
    Plugin.objects.all().delete()
    org = Organization.objects.create(name="Catalogue", slug="catalogue")
    foreign = Organization.objects.create(name="Foreign", slug="foreign")
    user = get_user_model().objects.create_user(email="catalogue@example.com")
    Membership.objects.create(user=user, organization=org)
    services = [
        CredentialService.objects.create(
            name=f"Service {index}",
            slug=f"catalogue-{index}",
            credential_type="env",
            env_var_name=f"CATALOGUE_{index}",
        )
        for index in range(2)
    ]
    return org, foreign, user, services


def _plugin(
    org: Organization | None, index: int, services: list[CredentialService]
) -> Plugin:
    """Create nested rows out of display order to exercise repository ordering."""
    plugin = Plugin.objects.create(
        organization=org, name=f"Plugin {index:03}", slug=f"catalogue-{index}"
    )
    for order, name in enumerate(("Zulu", "Alpha")):
        PluginSkill.objects.create(
            plugin=plugin,
            name=name,
            slug=name.lower(),
            body=f"Guide {name}",
            position=1 - order,
        )
        PluginMcpServer.objects.create(
            plugin=plugin,
            name=name,
            slug=name.lower(),
            command="synthetic-mcp",
            args=["--example"],
            env={"TOKEN": "{{credential.example}}"},
            resources={"desktop": {"activation": "first_tool"}},
            startup_timeout_seconds=45,
            request_timeout_seconds=90,
        )
        PluginCredentialRequirement.objects.create(
            plugin=plugin,
            key=name.lower(),
            credential_service=services[order],
            required=order == 0,
            description=f"Requirement {name}",
        )
    return plugin


def test_catalogue_preserves_visibility_components_and_activation(catalogue) -> None:
    """List/detail expose the same ordered definitions with org-scoped state."""
    org, foreign, _, services = catalogue
    global_plugin = _plugin(None, 2, services)
    owned = _plugin(org, 1, services)
    hidden = _plugin(foreign, 0, services)
    empty = Plugin.objects.create(
        organization=org, name="Empty", slug="empty", enabled=False, published=False
    )
    OrgPluginActivation.objects.create(organization=org, plugin=global_plugin)
    OrgPluginActivation.objects.create(organization=foreign, plugin=owned)
    service = PluginService()

    rows = service.list_visible(org_id=org.id)

    assert [row["id"] for row in rows] == [empty.id, owned.id, global_plugin.id]
    assert rows[0]["skills"] == rows[0]["mcp_servers"] == []
    assert rows[0]["credential_requirements"] == []
    assert rows[0]["enabled"] is rows[0]["published"] is False
    assert rows[1]["org_enabled"] is False
    assert rows[2]["org_enabled"] is True
    assert rows[1]["is_global"] is False
    assert rows[2]["is_global"] is True
    for row in rows[1:]:
        assert [skill["name"] for skill in row["skills"]] == ["Alpha", "Zulu"]
        assert [skill["body"] for skill in row["skills"]] == [
            "Guide Alpha",
            "Guide Zulu",
        ]
        assert [server["name"] for server in row["mcp_servers"]] == ["Alpha", "Zulu"]
        assert row["mcp_servers"][0]["resources"] == {
            "desktop": {"activation": "first_tool"}
        }
        assert row["mcp_servers"][0]["env"] == {"TOKEN": "{{credential.example}}"}
        assert [req["key"] for req in row["credential_requirements"]] == [
            "alpha",
            "zulu",
        ]
        assert [req["service_name"] for req in row["credential_requirements"]] == [
            "Service 1",
            "Service 0",
        ]
        assert [req["required"] for req in row["credential_requirements"]] == [
            False,
            True,
        ]
        assert row == service.get_visible(row["id"], org_id=org.id)
    with pytest.raises(NotFoundError):
        service.get_visible(hidden.id, org_id=org.id)
    with pytest.raises(NotFoundError):
        service.get_visible(uuid.uuid4(), org_id=org.id)


@pytest.mark.parametrize("count", [1, 12, 50])
def test_catalogue_query_count_is_bounded(
    catalogue, count: int, django_assert_num_queries
) -> None:
    """Relation and activation reads stay constant as the catalogue grows."""
    org, _, _, services = catalogue
    for index in range(count):
        _plugin(org if index % 2 else None, index, services)

    with django_assert_num_queries(5):
        rows = PluginService().list_visible(org_id=org.id)

    assert len(rows) == count
    assert all(len(row["credential_requirements"]) == 2 for row in rows)


def test_empty_catalogue_skips_related_queries(
    catalogue, django_assert_num_queries
) -> None:
    """An empty visible set needs only the initial plugin query."""
    org, foreign, _, services = catalogue
    _plugin(foreign, 0, services)
    with django_assert_num_queries(1):
        assert PluginService().list_visible(org_id=org.id) == []


def test_catalogue_rest_query_count_is_bounded(
    catalogue, django_assert_num_queries
) -> None:
    """Measure the authenticated adapter through complete JSON serialization."""
    org, _, user, services = catalogue
    for index in range(12):
        _plugin(org, index, services)
    token = generate_api_token()
    APIKey.objects.create(
        user=user,
        name="Catalogue read",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=[APIKeyPermission.PLUGINS_READ.value],
    )
    client = Client(HTTP_X_API_KEY=token, HTTP_X_ORGANIZATION_ID=str(org.id))
    with django_assert_num_queries(9):
        response = client.get("/api/v1/plugins/")
        assert response.status_code == 200
        assert len(response.json()) == 12


def test_catalogue_refreshes_changed_components_and_activations(catalogue) -> None:
    """Repeated calls on the same service never retain stale catalogue state."""
    org, _, _, services = catalogue
    plugin = _plugin(org, 0, services)
    service = PluginService()
    before = service.get_visible(plugin.id, org_id=org.id)
    assert before["org_enabled"] is False

    PluginSkill.objects.filter(plugin=plugin).update(body="New instructions")
    PluginMcpServer.objects.filter(plugin=plugin).delete()
    PluginCredentialRequirement.objects.filter(plugin=plugin).delete()
    OrgPluginActivationRepository.set_enabled(
        org_id=org.id, plugin=plugin, enabled_by=None, active=True
    )
    after = service.list_visible(org_id=org.id)[0]

    assert after["org_enabled"] is True
    assert all(skill["body"] == "New instructions" for skill in after["skills"])
    assert after["mcp_servers"] == after["credential_requirements"] == []
    assert before["mcp_servers"] and before["credential_requirements"]
