"""MCP/REST parity for harness engine availability and model catalogs."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.models import APIKey, APIKeyPermission
from apps.mcp_app.server import (
    _TOOL_HANDLERS,
    _TOOL_PERMISSIONS,
    _TOOLS,
    _call_get_provider_config,
    _call_list_claude_engine_models,
    _call_list_engine_models,
    _call_list_harness_engines,
    _call_save_provider_account,
)
from apps.organizations.models import Membership, Organization
from common.utils import generate_api_token, hash_token


def _payload(result):
    return json.loads(result[0].text)


@pytest.fixture
def engine_mcp_setup(db):
    organization = Organization.objects.create(
        name="MCP engines", slug=f"mcp-engines-{uuid.uuid4().hex[:10]}"
    )
    user = get_user_model().objects.create_user(
        email=f"engine-mcp-{uuid.uuid4().hex[:8]}@example.com", password="secret"
    )
    Membership.objects.create(user=user, organization=organization)
    return organization, SimpleNamespace(user=user)


def test_engine_catalog_tools_require_read_permission() -> None:
    names = {"list_harness_engines", "list_claude_engine_models", "list_engine_models"}
    assert names <= {tool.name for tool in _TOOLS}
    assert names <= _TOOL_HANDLERS.keys()
    assert all(
        _TOOL_PERMISSIONS[name] == APIKeyPermission.HARNESS_READ for name in names
    )


@pytest.mark.django_db(transaction=True)
def test_engine_and_provider_config_payloads_match_rest(engine_mcp_setup) -> None:
    """The account API drives the same engine status in both adapters."""
    org, api_key = engine_mcp_setup
    token = generate_api_token()
    APIKey.objects.create(
        user=api_key.user,
        name="parity",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=[APIKeyPermission.HARNESS_READ.value],
    )
    rest = Client(HTTP_X_API_KEY=token, HTTP_X_ORGANIZATION_ID=str(org.id))

    def assert_parity() -> None:
        for endpoint, handler in (
            ("harness/engines/", _call_list_harness_engines),
            ("harness/engines/claude/models/", _call_list_claude_engine_models),
            ("provider-config/", _call_get_provider_config),
        ):
            response = rest.get(f"/api/v1/{endpoint}")
            assert response.status_code == 200, response.content
            assert response.json() == _payload(handler(api_key, org.id, {}))

    assert_parity()
    assert not _payload(_call_list_harness_engines(api_key, org.id, {}))[1]["connected"]
    saved = _payload(
        _call_save_provider_account(
            api_key,
            org.id,
            {
                "provider": "claude-agent",
                "auth_type": "api_token",
                "token": "parity-token",
            },
        )
    )
    assert saved["connected"] is True
    assert "parity-token" not in repr(saved)
    assert _payload(_call_list_harness_engines(api_key, org.id, {}))[1]["connected"]
    assert_parity()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("handler", "args"),
    [
        (_call_list_harness_engines, {}),
        (_call_list_claude_engine_models, {}),
        (_call_list_engine_models, {"engine_id": "native"}),
        (_call_list_engine_models, {"engine_id": "claude"}),
        (_call_list_engine_models, {"engine_id": "codex"}),
    ],
)
def test_engine_catalog_rejects_foreign_organization(
    engine_mcp_setup, handler, args
) -> None:
    _org, api_key = engine_mcp_setup
    response = handler(api_key, uuid.uuid4(), args)
    assert response[0].text.startswith("Error:")
    assert "Organization" in response[0].text
