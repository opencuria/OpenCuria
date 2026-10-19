"""MCP parity for user-scoped Claude engines and connection auth."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model

from apps.accounts.models import APIKeyPermission
from apps.credentials.models import Credential, CredentialService
from apps.credentials.repositories import OrgCredentialServiceActivationRepository
from apps.harness.engines.connections import EngineConnectionService
from apps.mcp_app.server import (
    _TOOL_HANDLERS,
    _TOOL_PERMISSIONS,
    _TOOLS,
    _call_delete_claude_connection,
    _call_get_claude_connection,
    _call_list_claude_engine_models,
    _call_list_harness_engines,
    _call_save_claude_connection,
)
from apps.organizations.models import Membership, MembershipRole, Organization


def _payload(result):
    assert result
    if result[0].text.startswith("Error:"):
        return result[0].text
    return json.loads(result[0].text)


@pytest.fixture
def engine_mcp_setup(db):
    organization = Organization.objects.create(
        name=f"MCP Claude {uuid.uuid4().hex[:8]}",
        slug=f"mcp-claude-{uuid.uuid4().hex[:10]}",
    )
    user = get_user_model().objects.create_user(
        email=f"claude-mcp-{uuid.uuid4().hex[:8]}@example.com",
        password="secret",
    )
    other = get_user_model().objects.create_user(
        email=f"claude-mcp-other-{uuid.uuid4().hex[:8]}@example.com",
        password="secret",
    )
    Membership.objects.create(
        user=user, organization=organization, role=MembershipRole.MEMBER
    )
    Membership.objects.create(
        user=other, organization=organization, role=MembershipRole.MEMBER
    )
    service_rows = [
        CredentialService.objects.get_or_create(
            slug="claude-agent-api-token",
            organization=None,
            defaults={
                "name": "Claude Agent API Token",
                "credential_type": "env",
                "env_var_name": "ANTHROPIC_API_KEY",
                "label": "Claude API Token",
            },
        )[0],
        CredentialService.objects.get_or_create(
            slug="claude-agent-subscription-token",
            organization=None,
            defaults={
                "name": "Claude Subscription Token",
                "credential_type": "env",
                "env_var_name": "CLAUDE_CODE_OAUTH_TOKEN",
                "label": "Claude Subscription Token",
            },
        )[0],
    ]
    OrgCredentialServiceActivationRepository.ensure_activated(
        organization.id, [service.id for service in service_rows]
    )
    return {
        "organization": organization,
        "api_key": SimpleNamespace(user=user),
        "other_api_key": SimpleNamespace(user=other),
    }


def test_claude_mcp_tools_require_explicit_permissions() -> None:
    names = {tool.name for tool in _TOOLS}
    read_names = {"list_harness_engines", "list_claude_engine_models"}
    settings_names = {
        "get_claude_connection",
        "save_claude_connection",
        "delete_claude_connection",
    }
    assert read_names | settings_names <= names
    assert read_names | settings_names <= _TOOL_HANDLERS.keys()
    for name in read_names:
        assert _TOOL_PERMISSIONS[name] == APIKeyPermission.HARNESS_READ
    for name in settings_names:
        assert _TOOL_PERMISSIONS[name] == APIKeyPermission.HARNESS_PROVIDERS


@pytest.mark.django_db(transaction=True)
def test_claude_mcp_connection_crud_is_personal_and_secret_free(
    engine_mcp_setup,
) -> None:
    data = engine_mcp_setup
    org_id = data["organization"].id
    api_key = data["api_key"]
    token = "mcp-claude-token"

    engines = _payload(_call_list_harness_engines(api_key, org_id, {}))
    assert engines[1]["id"] == "claude"
    assert engines[1]["connected"] is False
    assert _payload(_call_get_claude_connection(api_key, org_id, {})) is None

    models = _payload(_call_list_claude_engine_models(api_key, org_id, {}))
    assert {model["id"] for model in models} == {"sonnet", "opus", "haiku"}

    saved = _payload(
        _call_save_claude_connection(
            api_key,
            org_id,
            {"auth_type": "api_token", "token": token, "label": "MCP Claude"},
        )
    )
    assert saved["connected"] is True
    assert token not in json.dumps(saved)
    credential = Credential.objects.get(
        user=api_key.user, service__slug="provider-account-claude-agent"
    )
    assert credential.organization_id is None
    assert EngineConnectionService().resolve(org_id, api_key.user.id).token == token
    assert (
        _payload(_call_get_claude_connection(api_key, org_id, {}))["id"] == saved["id"]
    )
    assert _payload(_call_list_harness_engines(api_key, org_id, {}))[1]["connected"]
    assert (
        _payload(_call_get_claude_connection(data["other_api_key"], org_id, {})) is None
    )

    deleted = _payload(_call_delete_claude_connection(api_key, org_id, {}))
    assert deleted == {"deleted": True}
    assert not Credential.objects.filter(id=credential.id).exists()


@pytest.mark.django_db(transaction=True)
def test_claude_mcp_rejects_invalid_modes_and_foreign_organization(
    engine_mcp_setup,
) -> None:
    data = engine_mcp_setup
    response = _call_save_claude_connection(
        data["api_key"],
        data["organization"].id,
        {"auth_type": "not-claude", "token": "secret"},
    )
    assert "auth_type" in _payload(response)
    foreign = _call_get_claude_connection(data["api_key"], uuid.uuid4(), {})
    assert "Organization" in _payload(foreign)


@pytest.mark.django_db(transaction=True)
def test_claude_mcp_exact_account_mutations_preserve_active_selection(
    engine_mcp_setup,
) -> None:
    """MCP forwards exact connection IDs just like the REST compatibility API."""
    from apps.harness.provider_accounts import ProviderAccountService

    data = engine_mcp_setup
    org_id = data["organization"].id
    api_key = data["api_key"]
    accounts = ProviderAccountService()
    first = accounts.save_account(
        organization_id=org_id,
        user=api_key.user,
        provider="claude-agent",
        payload={"label": "First", "token": "first-token"},
    )
    second = accounts.save_account(
        organization_id=org_id,
        user=api_key.user,
        provider="claude-agent",
        payload={"label": "Second", "token": "second-token"},
    )
    for name in ("save_claude_connection", "delete_claude_connection"):
        schema = next(tool for tool in _TOOLS if tool.name == name).inputSchema
        assert "connection_id" in schema["properties"]
    updated = _payload(
        _call_save_claude_connection(
            api_key,
            org_id,
            {
                "connection_id": str(second.id),
                "auth_type": "api_token",
                "label": "Updated second",
                "token": "updated-token",
            },
        )
    )
    assert updated["id"] == str(second.id)
    assert accounts.selected_accounts(org_id, api_key.user.id)["claude-agent"].id == (
        first.id
    )
    deleted = _payload(
        _call_delete_claude_connection(
            api_key, org_id, {"connection_id": str(second.id)}
        )
    )
    assert deleted == {"deleted": True}
    assert accounts.selected_accounts(org_id, api_key.user.id)["claude-agent"].id == (
        first.id
    )
    assert "Error:" in _payload(
        _call_save_claude_connection(
            api_key,
            org_id,
            {"connection_id": str(second.id), "auth_type": "api_token", "token": "x"},
        )
    )


@pytest.mark.django_db(transaction=True)
def test_engine_and_provider_config_payloads_match_rest(engine_mcp_setup) -> None:
    """REST and MCP share one engine list and one config DTO, not copies."""
    from django.test import Client

    from apps.accounts.models import APIKey
    from apps.mcp_app.server import _call_get_provider_config
    from common.utils import generate_api_token, hash_token

    data = engine_mcp_setup
    org_id = data["organization"].id
    api_key = data["api_key"]
    token = generate_api_token()
    APIKey.objects.create(
        user=api_key.user,
        name="parity",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=[APIKeyPermission.HARNESS_READ.value],
    )
    rest = Client(HTTP_X_API_KEY=token, HTTP_X_ORGANIZATION_ID=str(org_id))

    def assert_parity() -> None:
        assert rest.get("/api/v1/harness/engines/").json() == _payload(
            _call_list_harness_engines(api_key, org_id, {})
        )
        assert rest.get("/api/v1/provider-config/").json() == _payload(
            _call_get_provider_config(api_key, org_id, {})
        )

    assert_parity()  # unconnected engine, unconfigured (empty) defaults
    _call_save_claude_connection(
        api_key, org_id, {"auth_type": "api_token", "token": "parity-token"}
    )
    assert _payload(_call_list_harness_engines(api_key, org_id, {}))[1]["connected"]
    assert_parity()
