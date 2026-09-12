"""MCP tools for multi-provider connection management."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from django.contrib.auth import get_user_model

from apps.accounts.models import APIKeyPermission
from apps.harness.providers.chatgpt_oauth import DeviceFlowStart, OAuthTokens
from apps.harness.services import ProviderConfigService
from apps.mcp_app.server import (
    _TOOL_HANDLERS,
    _TOOL_PERMISSIONS,
    _TOOLS,
    _call_chatgpt_oauth_cancel,
    _call_chatgpt_oauth_start,
    _call_chatgpt_oauth_status,
    _call_delete_provider_connection,
    _call_list_provider_connections,
    _call_list_provider_models,
    _call_save_provider_connection,
)
from apps.organizations.models import Membership, MembershipRole, Organization


@pytest.fixture
def provider_mcp_setup(db):
    """Org + member for provider MCP handler tests."""
    user_model = get_user_model()
    org = Organization.objects.create(
        name=f"MCP Prov {uuid.uuid4().hex[:6]}",
        slug=f"mcp-prov-{uuid.uuid4().hex[:8]}",
    )
    user = user_model.objects.create_user(
        email=f"mcp-prov-{uuid.uuid4().hex[:6]}@example.com",
        password="secret",
    )
    Membership.objects.create(user=user, organization=org, role=MembershipRole.MEMBER)
    return {
        "org": org,
        "api_key": SimpleNamespace(user=user),
    }


def _parse_text(result) -> dict | list:
    assert len(result) == 1
    return json.loads(result[0].text)


def _parse_error(result) -> str:
    assert len(result) == 1
    return result[0].text


def test_mcp_provider_tools_registered_with_harness_providers_permission():
    """New provider tools map to harness:providers."""
    tool_names = {tool.name for tool in _TOOLS}
    for name in [
        "list_provider_connections",
        "save_provider_connection",
        "delete_provider_connection",
        "chatgpt_oauth_start",
        "chatgpt_oauth_status",
        "chatgpt_oauth_cancel",
    ]:
        assert name in tool_names
        assert name in _TOOL_HANDLERS
        assert _TOOL_PERMISSIONS[name] == APIKeyPermission.HARNESS_PROVIDERS


@pytest.mark.django_db(transaction=True)
def test_mcp_list_provider_connections(provider_mcp_setup):
    """list_provider_connections mirrors GET /provider-config/providers/."""
    result = _call_list_provider_connections(
        provider_mcp_setup["api_key"],
        provider_mcp_setup["org"].id,
        {},
    )
    payload = _parse_text(result)
    providers = {row["provider"]: row for row in payload}
    assert set(providers) == {
        "openrouter",
        "chatgpt",
        "amazon-bedrock",
        "openai-compatible",
    }
    assert all(not row["connected"] for row in payload)


@pytest.mark.django_db(transaction=True)
def test_mcp_save_provider_connection_openrouter(provider_mcp_setup):
    """save_provider_connection upserts OpenRouter credentials."""
    result = _call_save_provider_connection(
        provider_mcp_setup["api_key"],
        provider_mcp_setup["org"].id,
        {
            "provider": "openrouter",
            "api_key": "sk-mcp-openrouter",
            "base_url": "https://openrouter.ai/api/v1",
        },
    )
    payload = _parse_text(result)
    assert payload["connected"] is True
    assert payload["provider"] == "openrouter"
    assert payload["api_key_hint"] == "••••uter"


@pytest.mark.django_db(transaction=True)
def test_mcp_save_provider_connection_validation_errors(provider_mcp_setup):
    """save_provider_connection surfaces validation errors as MCP errors."""
    chatgpt = _call_save_provider_connection(
        provider_mcp_setup["api_key"],
        provider_mcp_setup["org"].id,
        {"provider": "chatgpt"},
    )
    assert "OAuth" in _parse_error(chatgpt)

    unknown = _call_save_provider_connection(
        provider_mcp_setup["api_key"],
        provider_mcp_setup["org"].id,
        {"provider": "unknown-provider", "api_key": "x"},
    )
    assert "Unknown provider" in _parse_error(unknown)

    bedrock = _call_save_provider_connection(
        provider_mcp_setup["api_key"],
        provider_mcp_setup["org"].id,
        {"provider": "amazon-bedrock", "auth_method": "invalid"},
    )
    assert "auth_method" in _parse_error(bedrock)

    hostless = _call_save_provider_connection(
        provider_mcp_setup["api_key"],
        provider_mcp_setup["org"].id,
        {"provider": "openai-compatible", "base_url": "https://"},
    )
    assert "base_url" in _parse_error(hostless)


@pytest.mark.django_db(transaction=True)
def test_mcp_delete_provider_connection(provider_mcp_setup):
    """delete_provider_connection removes a stored connection."""
    _call_save_provider_connection(
        provider_mcp_setup["api_key"],
        provider_mcp_setup["org"].id,
        {
            "provider": "amazon-bedrock",
            "auth_method": "bearer",
            "bearer_token": "bedrock-bearer",
        },
    )
    deleted = _call_delete_provider_connection(
        provider_mcp_setup["api_key"],
        provider_mcp_setup["org"].id,
        {"provider": "amazon-bedrock"},
    )
    payload = _parse_text(deleted)
    assert payload == {"deleted": True, "provider": "amazon-bedrock"}

    missing = _call_delete_provider_connection(
        provider_mcp_setup["api_key"],
        provider_mcp_setup["org"].id,
        {"provider": "amazon-bedrock"},
    )
    assert "not found" in _parse_error(missing).lower()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_mcp_chatgpt_oauth_flow(provider_mcp_setup):
    """chatgpt_oauth_* tools mirror the REST OAuth flow with mocked polling."""
    api_key = provider_mcp_setup["api_key"]
    org_id = provider_mcp_setup["org"].id

    with patch(
        "apps.harness.providers.chatgpt_oauth.start_device_flow",
        new_callable=AsyncMock,
    ) as mock_start:
        mock_start.return_value = DeviceFlowStart(
            device_auth_id="auth-mcp",
            user_code="MCP-1234",
            verification_url="https://auth.openai.com/codex/device",
            interval=5,
        )
        start = await _call_chatgpt_oauth_start(api_key, org_id, {})
    start_body = _parse_text(start)
    assert start_body["user_code"] == "MCP-1234"
    assert start_body["interval"] == 5
    assert start_body["expires_in"] == 600

    with patch(
        "apps.harness.providers.chatgpt_oauth.poll_device_flow_once",
        new_callable=AsyncMock,
    ) as mock_poll:
        from apps.harness.providers.chatgpt_oauth import DeviceFlowPollResult

        mock_poll.return_value = DeviceFlowPollResult(status="pending")
        pending = await _call_chatgpt_oauth_status(api_key, org_id, {})
    assert _parse_text(pending) == {"status": "pending"}

    with patch(
        "apps.harness.providers.chatgpt_oauth.poll_device_flow_once",
        new_callable=AsyncMock,
    ) as mock_poll:
        from apps.harness.providers.chatgpt_oauth import DeviceFlowPollResult

        mock_poll.return_value = DeviceFlowPollResult(
            status="complete",
            tokens=OAuthTokens(
                access="mcp-access",
                refresh="mcp-refresh",
                expires=9999999999,
                account_id="acc-mcp",
                residency=None,
            ),
        )
        connected = await _call_chatgpt_oauth_status(api_key, org_id, {})
    assert _parse_text(connected) == {
        "status": "connected",
        "account_id": "acc-mcp",
    }

    service = ProviderConfigService()
    connection = service.get_connection(org_id, "chatgpt")
    assert connection is not None
    creds = service.get_connection_credentials(connection)
    assert creds["access"] == "mcp-access"

    no_flow = await _call_chatgpt_oauth_status(api_key, org_id, {})
    assert _parse_text(no_flow)["status"] == "no_flow"

    with patch(
        "apps.harness.providers.chatgpt_oauth.start_device_flow",
        new_callable=AsyncMock,
    ) as mock_start:
        mock_start.return_value = DeviceFlowStart(
            device_auth_id="auth-cancel",
            user_code="CNCL-9999",
            verification_url="https://auth.openai.com/codex/device",
            interval=5,
        )
        await _call_chatgpt_oauth_start(api_key, org_id, {})

    cancelled = await _call_chatgpt_oauth_cancel(api_key, org_id, {})
    assert _parse_text(cancelled) == {"cancelled": True}
    assert _parse_text(await _call_chatgpt_oauth_status(api_key, org_id, {}))[
        "status"
    ] == "no_flow"


@pytest.mark.django_db(transaction=True)
def test_mcp_list_provider_models_includes_provider_field(
    provider_mcp_setup, monkeypatch
):
    """list_provider_models includes the provider field on each model row."""
    from apps.harness.providers.models_catalog import ProviderModel

    class _FakeService:
        def list_models(self, _org_id):
            return [
                ProviderModel(
                    id="openrouter/model-a",
                    name="Model A",
                    provider="openrouter",
                    reasoning_efforts=(),
                    default_effort="",
                    supports_tools=True,
                    context_length=128000,
                    max_output_tokens=8192,
                ),
                ProviderModel(
                    id="amazon-bedrock/model-b",
                    name="Model B",
                    provider="amazon-bedrock",
                    reasoning_efforts=("low",),
                    default_effort="low",
                    supports_tools=False,
                    context_length=64000,
                    max_output_tokens=4096,
                ),
            ]

    monkeypatch.setattr(
        "apps.harness.services.ProviderConfigService",
        lambda: _FakeService(),
    )

    result = _call_list_provider_models(
        provider_mcp_setup["api_key"],
        provider_mcp_setup["org"].id,
        {},
    )
    payload = _parse_text(result)
    providers = {row["provider"] for row in payload}
    assert providers == {"openrouter", "amazon-bedrock"}
