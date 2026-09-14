"""
Tests for plugin model constraints and validation.
"""

from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction

from apps.credentials.models import CredentialService
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.plugins.models import (
    OrgPluginActivation,
    Plugin,
    PluginCredentialRequirement,
    PluginMcpServer,
    PluginSkill,
    WorkspacePluginActivation,
)
from apps.plugins.services import (
    PLAYWRIGHT_MCP_ARGS,
    PluginService,
    normalize_mcp_payload,
)
from apps.runners.models import Runner, Workspace


def _org(name: str) -> Organization:
    return Organization.objects.create(
        name=name, slug=f"{name.lower()}-{uuid.uuid4().hex[:8]}"
    )


@pytest.mark.django_db
def test_global_slug_unique_and_org_slug_scoped():
    Plugin.objects.create(name="G", slug="dup")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Plugin.objects.create(name="G2", slug="dup")

    org_a, org_b = _org("A"), _org("B")
    Plugin.objects.create(name="P", slug="same", organization=org_a)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Plugin.objects.create(name="P2", slug="same", organization=org_a)
    # Same slug in another org is fine; global slug independent of org slugs.
    Plugin.objects.create(name="P3", slug="same", organization=org_b)
    Plugin.objects.create(name="G3", slug="same")


@pytest.mark.django_db
def test_component_unique_constraints():
    plugin = Plugin.objects.create(name="P", slug=f"p-{uuid.uuid4().hex[:8]}")
    PluginSkill.objects.create(plugin=plugin, name="S", slug="s", body="b", position=0)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            PluginSkill.objects.create(
                plugin=plugin, name="S2", slug="s", body="b", position=1
            )
    PluginMcpServer.objects.create(
        plugin=plugin, name="M", slug="m", transport="stdio", command="npx"
    )
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            PluginMcpServer.objects.create(
                plugin=plugin, name="M2", slug="m", transport="stdio", command="npx"
            )


@pytest.mark.django_db
def test_activation_unique_constraints():
    user_model = get_user_model()
    user = user_model.objects.create_user(
        email=f"u-{uuid.uuid4().hex[:8]}@t.local", password="secret"
    )
    org = _org("Act")
    plugin = Plugin.objects.create(name="P", slug=f"p-{uuid.uuid4().hex[:8]}")
    OrgPluginActivation.objects.create(organization=org, plugin=plugin, enabled_by=user)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            OrgPluginActivation.objects.create(organization=org, plugin=plugin)

    runner = Runner.objects.create(
        name="r", api_token_hash=uuid.uuid4().hex, organization=org
    )
    workspace = Workspace.objects.create(runner=runner, name="w", created_by=user)
    WorkspacePluginActivation.objects.create(
        workspace=workspace, plugin=plugin, enabled_by=user
    )
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            WorkspacePluginActivation.objects.create(workspace=workspace, plugin=plugin)


@pytest.mark.django_db
def test_requirement_unique_constraints():
    plugin = Plugin.objects.create(name="P", slug=f"p-{uuid.uuid4().hex[:8]}")
    svc_a = CredentialService.objects.create(
        name="A",
        slug=f"a-{uuid.uuid4().hex[:8]}",
        credential_type="env",
        env_var_name="A_TOKEN",
    )
    svc_b = CredentialService.objects.create(
        name="B",
        slug=f"b-{uuid.uuid4().hex[:8]}",
        credential_type="env",
        env_var_name="B_TOKEN",
    )
    PluginCredentialRequirement.objects.create(
        plugin=plugin, key="api_key", credential_service=svc_a
    )
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            PluginCredentialRequirement.objects.create(
                plugin=plugin, key="api_key", credential_service=svc_b
            )
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            PluginCredentialRequirement.objects.create(
                plugin=plugin, key="other", credential_service=svc_a
            )


@pytest.mark.django_db
def test_credential_service_org_slug_constraints():
    org_a, org_b = _org("SvcA"), _org("SvcB")
    CredentialService.objects.create(
        name="G", slug="shared-svc", credential_type="env", env_var_name="X"
    )
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            CredentialService.objects.create(
                name="G2", slug="shared-svc", credential_type="env", env_var_name="Y"
            )
    CredentialService.objects.create(
        name="O",
        slug="shared-svc",
        credential_type="env",
        env_var_name="Z",
        organization=org_a,
    )
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            CredentialService.objects.create(
                name="O2",
                slug="shared-svc",
                credential_type="env",
                env_var_name="W",
                organization=org_a,
            )
    CredentialService.objects.create(
        name="O3",
        slug="shared-svc",
        credential_type="env",
        env_var_name="V",
        organization=org_b,
    )


@pytest.mark.django_db
def test_mcp_validation_stdio_requires_command_and_no_url():
    with pytest.raises(ValueError, match="command is required"):
        normalize_mcp_payload({"name": "M", "transport": "stdio"})
    with pytest.raises(ValueError, match="must be empty for stdio"):
        normalize_mcp_payload(
            {"name": "M", "transport": "stdio", "command": "npx", "url": "http://x"}
        )
    with pytest.raises(ValueError, match="single executable"):
        normalize_mcp_payload(
            {"name": "M", "transport": "stdio", "command": "npx foo; rm -rf /"}
        )


@pytest.mark.django_db
def test_mcp_validation_http_requires_url_and_no_command():
    with pytest.raises(ValueError, match="http\\(s\\) URL"):
        normalize_mcp_payload({"name": "M", "transport": "streamable_http"})
    with pytest.raises(ValueError, match="must be empty for http/sse"):
        normalize_mcp_payload(
            {
                "name": "M",
                "transport": "sse",
                "url": "https://example.com/mcp",
                "command": "npx",
            }
        )
    ok = normalize_mcp_payload(
        {"name": "M", "transport": "sse", "url": "https://example.com/mcp"}
    )
    assert ok["url"] == "https://example.com/mcp"


@pytest.mark.django_db
def test_playwright_seed_shape():
    # The migration under test already ran when the test DB was created.
    plugin = Plugin.objects.filter(slug="playwright", organization__isnull=True).first()
    assert plugin is not None
    assert plugin.enabled and plugin.published
    server = PluginMcpServer.objects.filter(plugin=plugin, slug="playwright").first()
    assert server is not None
    assert server.transport == "stdio"
    assert server.command == "npx"
    for required_arg in PLAYWRIGHT_MCP_ARGS:
        assert required_arg in list(server.args or [])
    assert PluginSkill.objects.filter(plugin=plugin).exists()
    # Explicit opt-in: no org activations created by the seed.
    assert not OrgPluginActivation.objects.filter(plugin=plugin).exists()
    assert PluginService().plugins.get_global_by_slug("playwright") is not None


@pytest.mark.django_db
def test_member_orgs_do_not_auto_enable_playwright():
    from apps.organizations.services import OrganizationService

    user_model = get_user_model()
    user = user_model.objects.create_user(
        email=f"seed-{uuid.uuid4().hex[:8]}@t.local", password="secret"
    )
    org = OrganizationService().create_organization(name="SeedOrg", user=user)
    Membership.objects.filter(user=user, organization=org).update(
        role=MembershipRole.ADMIN
    )
    playwright = Plugin.objects.filter(
        slug="playwright", organization__isnull=True
    ).first()
    assert playwright is not None
    assert not OrgPluginActivation.objects.filter(
        organization=org, plugin=playwright
    ).exists()
