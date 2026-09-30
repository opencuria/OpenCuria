"""End-to-end API connect and public callback redirect contract."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.auth_backends import DjangoJWTBackend
from apps.credentials.enums import CredentialType
from apps.credentials.models import (
    Credential,
    CredentialService,
    McpOAuthAuthorizationState,
)
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.plugins.models import Plugin, PluginCredentialRequirement, PluginMcpServer
from common.utils import decrypt_value


@pytest.mark.django_db
def test_connect_sets_binding_cookie_then_callback_redirects_only_to_frontend_result(
    settings, monkeypatch
):
    frontend = "http://127.0.0.1:8080/?settings=plugins"
    callback_url = "http://127.0.0.1:8000/api/v1/mcp-oauth/callback/"
    settings.DEBUG = True
    settings.MCP_OAUTH_CALLBACK_URL = callback_url
    settings.MCP_OAUTH_FRONTEND_RETURN_URL = frontend

    user = get_user_model().objects.create_user(
        email="oauth-callback@test.local", password="secret"
    )
    organization = Organization.objects.create(
        name="OAuth callback org", slug="oauth-callback-org"
    )
    Membership.objects.create(
        user=user, organization=organization, role=MembershipRole.ADMIN
    )
    plugin = Plugin.objects.create(
        name="OAuth callback plugin",
        slug="oauth-callback-plugin",
        organization=organization,
    )
    service = CredentialService.objects.create(
        name="MCP OAuth",
        slug="oauth-callback-service",
        organization=organization,
        credential_type=CredentialType.MCP_OAUTH,
        plugin_owned=True,
        oauth_plugin_slug=plugin.slug,
        oauth_requirement_key="mcp",
    )
    server = PluginMcpServer.objects.create(
        plugin=plugin,
        name="MCP",
        slug="mcp",
        transport="streamable_http",
        url="https://mcp.example/mcp",
        auth_type="oauth",
        oauth_requirement_key="mcp",
    )
    PluginCredentialRequirement.objects.create(
        plugin=plugin,
        key="mcp",
        credential_service=service,
        required=True,
    )

    monkeypatch.setattr(
        "apps.credentials.mcp_oauth._discover",
        lambda _url: (
            {"_challenge_scope": ""},
            {
                "issuer": "https://auth.example/tenant",
                "authorization_endpoint": "https://auth.example/authorize",
                "token_endpoint": "https://auth.example/token",
            },
            "https://mcp.example/mcp",
        ),
    )

    def registration(_server_url, callback, _protected, metadata):
        from apps.credentials.models import McpOAuthClientRegistration

        return McpOAuthClientRegistration.objects.create(
            server_url=_server_url,
            callback_url=callback,
            issuer=metadata["issuer"],
            client_id="test-client",
            authorization_endpoint=metadata["authorization_endpoint"],
            token_endpoint=metadata["token_endpoint"],
            token_endpoint_auth_method="none",
        )

    monkeypatch.setattr("apps.credentials.mcp_oauth._registration", registration)
    monkeypatch.setattr(
        "apps.credentials.mcp_oauth._validated_https_url",
        lambda url, **_kwargs: (url, ("93.184.216.34",)),
    )
    token_exchange_calls = []

    def exchange_token(*_args, **_kwargs):
        token_exchange_calls.append(True)
        return {
            "access_token": "mock-access-token",
            "refresh_token": "mock-refresh-token",
            "expires_in": 3600,
        }

    monkeypatch.setattr("apps.credentials.mcp_oauth._post_token", exchange_token)

    client = Client()
    token = DjangoJWTBackend().generate_tokens(user).access_token
    response = client.post(
        f"/api/v1/mcp-oauth/{plugin.id}/mcp-servers/{server.id}/oauth/connect/",
        data=json.dumps({"service_id": str(service.id)}),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
        HTTP_X_ORGANIZATION_ID=str(organization.id),
        HTTP_HOST="127.0.0.1:8000",
    )
    assert response.status_code == 200, response.content
    oauth_cookies = [name for name in response.cookies if name.startswith("mcp_oauth_")]
    assert len(oauth_cookies) == 1
    cookie_name = oauth_cookies[0]
    cookie = response.cookies[cookie_name]
    assert cookie["httponly"] is True
    assert cookie["samesite"] == "Lax"
    assert cookie["path"] == "/api/v1/mcp-oauth/callback/"

    authorization_params = parse_qs(
        urlparse(response.json()["authorization_url"]).query
    )
    raw_state = authorization_params["state"][0]
    state_row = McpOAuthAuthorizationState.objects.get()
    assert raw_state not in state_row.state_hash

    callback_response = client.get(
        f"/api/v1/mcp-oauth/callback/?code=provider-secret-code&state={raw_state}",
        HTTP_COOKIE=f"{cookie_name}={cookie.value}",
        HTTP_HOST="127.0.0.1:8000",
    )
    assert callback_response.status_code == 302
    assert len(token_exchange_calls) == 1
    assert callback_response["Location"] == frontend + "&mcp_oauth=connected"
    target = urlparse(callback_response["Location"])
    target_params = parse_qs(target.query)
    assert target_params == {"settings": ["plugins"], "mcp_oauth": ["connected"]}
    assert "provider-secret-code" not in callback_response["Location"]
    assert "code" not in target_params
    assert "state" not in target_params
    assert callback_response["Cache-Control"] == "no-store"
    assert callback_response.cookies[cookie_name]["max-age"] == 0

    credential = Credential.objects.get(user=user, service=service)
    decrypted = decrypt_value(credential.encrypted_value)
    assert "mock-access-token" in decrypted
    assert "mock-access-token" not in callback_response["Location"]


def test_cross_site_or_hostname_browser_flow_mismatch_is_explicit(settings):
    from apps.credentials.mcp_oauth import OAuthError, _validate_browser_flow_origins

    settings.DEBUG = True
    settings.MCP_OAUTH_CALLBACK_URL = "http://localhost:8000/api/v1/mcp-oauth/callback/"
    settings.MCP_OAUTH_FRONTEND_RETURN_URL = "http://127.0.0.1:8080/?settings=plugins"
    with pytest.raises(OAuthError, match="same hostname and scheme"):
        _validate_browser_flow_origins()
