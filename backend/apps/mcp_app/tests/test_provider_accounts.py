"""MCP tools for named provider accounts and ChatGPT OAuth."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from django.contrib.auth import get_user_model

from apps.accounts.models import APIKeyPermission
from apps.harness.providers.chatgpt_oauth import DeviceFlowStart, OAuthTokens
from apps.mcp_app.server import (
    _TOOL_HANDLERS,
    _TOOL_PERMISSIONS,
    _TOOLS,
    _call_chatgpt_oauth_cancel,
    _call_chatgpt_oauth_start,
    _call_chatgpt_oauth_status,
    _call_delete_provider_account,
    _call_list_provider_accounts,
    _call_save_provider_account,
    _call_select_provider_account,
)
from apps.organizations.models import Membership, MembershipRole, Organization


@pytest.fixture
def provider_mcp_setup(db):
    """An org member and its MCP API-key identity; no workspace is needed."""
    organization = Organization.objects.create(
        name="MCP providers", slug=f"mcp-providers-{uuid.uuid4().hex[:8]}"
    )
    user = get_user_model().objects.create_user(
        email=f"mcp-provider-{uuid.uuid4().hex[:8]}@example.com", password="secret"
    )
    Membership.objects.create(
        user=user, organization=organization, role=MembershipRole.MEMBER
    )
    return {"org": organization, "api_key": SimpleNamespace(user=user)}


def _parse_text(result) -> dict | list:
    assert len(result) == 1
    return json.loads(result[0].text)


def test_mcp_provider_account_tools_are_registered_and_old_tools_removed():
    tool_names = {tool.name for tool in _TOOLS}
    retained = {
        "list_provider_accounts",
        "save_provider_account",
        "delete_provider_account",
        "select_provider_account",
        "chatgpt_oauth_start",
        "chatgpt_oauth_status",
        "chatgpt_oauth_cancel",
    }
    for name in retained:
        assert name in tool_names
        assert name in _TOOL_HANDLERS
        assert _TOOL_PERMISSIONS[name] == APIKeyPermission.HARNESS_PROVIDERS

    removed = {
        "list_provider_connections",
        "save_provider_connection",
        "delete_provider_connection",
        "get_claude_connection",
        "save_claude_connection",
        "delete_claude_connection",
    }
    assert tool_names.isdisjoint(removed)
    assert not (_TOOL_HANDLERS.keys() & removed)
    assert not (_TOOL_PERMISSIONS.keys() & removed)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "provider,credentials",
    [
        ("openrouter", {"api_key": "sk-mcp-private-1234"}),
        (
            "claude-agent",
            {"auth_type": "api_token", "token": "sk-mcp-private-1234"},
        ),
    ],
)
def test_mcp_provider_accounts_save_list_select_and_delete(
    provider_mcp_setup, provider, credentials
):
    """Account tools expose safe metadata and preserve selection semantics."""
    api_key = provider_mcp_setup["api_key"]
    org_id = provider_mcp_setup["org"].id
    saved = _parse_text(
        _call_save_provider_account(
            api_key,
            org_id,
            {
                "provider": provider,
                "label": "Personal MCP",
                **credentials,
            },
        )
    )
    assert saved["provider"] == provider
    assert saved["scope"] == "personal"
    assert saved["api_key_hint"] == "••••1234"
    assert "sk-mcp-private-1234" not in repr(saved)
    assert [
        row["id"]
        for row in _parse_text(_call_list_provider_accounts(api_key, org_id, {}))
    ] == [saved["id"]]

    cleared = _parse_text(
        _call_select_provider_account(
            api_key, org_id, {"provider": provider, "account_id": None}
        )
    )
    assert cleared["selected"] is True
    deleted = _parse_text(
        _call_delete_provider_account(api_key, org_id, {"account_id": saved["id"]})
    )
    assert deleted == {"deleted": True, "account_id": saved["id"]}
    assert _parse_text(_call_list_provider_accounts(api_key, org_id, {})) == []


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_mcp_chatgpt_oauth_flow(provider_mcp_setup):
    """chatgpt_oauth_* tools retain flow IDs through polling and cancellation."""
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
    flow_id = start_body["flow_id"]

    with patch(
        "apps.harness.providers.chatgpt_oauth.poll_device_flow_once",
        new_callable=AsyncMock,
    ) as mock_poll:
        from apps.harness.providers.chatgpt_oauth import DeviceFlowPollResult

        mock_poll.return_value = DeviceFlowPollResult(status="pending")
        pending = await _call_chatgpt_oauth_status(
            api_key, org_id, {"flow_id": flow_id}
        )
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
        connected = await _call_chatgpt_oauth_status(
            api_key, org_id, {"flow_id": flow_id}
        )
    assert _parse_text(connected) == {"status": "connected", "account_id": "acc-mcp"}

    from apps.harness.provider_accounts import ProviderAccountService

    accounts = ProviderAccountService().list_accounts(
        org_id, api_key.user.id, "chatgpt"
    )
    assert len(accounts) == 1
    assert accounts[0]["account_id"] == "acc-mcp"
    assert "mcp-access" not in repr(accounts)
    assert (
        _parse_text(await _call_chatgpt_oauth_status(api_key, org_id, {}))["status"]
        == "no_flow"
    )

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
        cancel_flow_id = _parse_text(
            await _call_chatgpt_oauth_start(api_key, org_id, {})
        )["flow_id"]
    cancelled = await _call_chatgpt_oauth_cancel(
        api_key, org_id, {"flow_id": cancel_flow_id}
    )
    assert _parse_text(cancelled) == {"cancelled": True}
    assert (
        _parse_text(
            await _call_chatgpt_oauth_status(
                api_key, org_id, {"flow_id": cancel_flow_id}
            )
        )["status"]
        == "cancelled"
    )


@pytest.mark.django_db
def test_mcp_defaults_roundtrip_uses_defaults_only_contract(provider_mcp_setup):
    from apps.mcp_app.server import (
        _call_delete_provider_config,
        _call_get_provider_config,
        _call_save_provider_config,
    )

    api_key, org_id = provider_mcp_setup["api_key"], provider_mcp_setup["org"].id
    saved = _parse_text(
        _call_save_provider_config(
            api_key, org_id, {"small_model": "openrouter/small", "small_effort": "low"}
        )
    )
    assert saved == {"small_model": "openrouter/small", "small_effort": "low"}
    assert _parse_text(_call_get_provider_config(api_key, org_id, {})) == saved
    assert _parse_text(_call_list_provider_accounts(api_key, org_id, {})) == []
    assert _parse_text(_call_delete_provider_config(api_key, org_id, {}))["deleted"]
    assert not any(_parse_text(_call_get_provider_config(api_key, org_id, {})).values())
