"""Service-scoped OAuth API contract tests."""

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
    McpOAuthClientRegistration,
    OrgCredentialServiceActivation,
)
from apps.credentials.services import CredentialOAuthSvc
from apps.organizations.models import Membership, MembershipRole, Organization
from common.exceptions import AuthenticationError, ConflictError
from common.utils import decrypt_value, encrypt_value


@pytest.mark.django_db
def test_callback_commit_rechecks_registration_membership_service_and_activation(
    settings, monkeypatch
):
    settings.DEBUG = True
    settings.MCP_OAUTH_CALLBACK_URL = "http://127.0.0.1:8000/api/v1/mcp-oauth/callback/"
    settings.MCP_OAUTH_FRONTEND_RETURN_URL = (
        "http://127.0.0.1:8080/?settings=credentials"
    )
    user = get_user_model().objects.create_user(
        email="oauth-revalidation@test.local", password="x"
    )
    org = Organization.objects.create(name="Recheck org", slug="oauth-recheck-org")
    Membership.objects.create(user=user, organization=org, role=MembershipRole.ADMIN)
    endpoint = "https://mcp.recheck.test/mcp"
    service = CredentialService.objects.create(
        name="Recheck MCP",
        slug="recheck-mcp",
        organization=org,
        credential_type=CredentialType.MCP_OAUTH,
        oauth_server_url=endpoint,
    )
    activation = OrgCredentialServiceActivation.objects.create(
        organization=org, credential_service=service
    )
    registration = McpOAuthClientRegistration.objects.create(
        server_url=endpoint,
        callback_url=settings.MCP_OAUTH_CALLBACK_URL,
        issuer="https://auth.recheck.test",
        client_id="recheck-client",
        authorization_endpoint="https://auth.recheck.test/authorize",
        token_endpoint="https://auth.recheck.test/token",
    )
    monkeypatch.setattr(
        "apps.credentials.mcp_oauth._discover",
        lambda _url: (
            {"_challenge_scope": ""},
            {"issuer": registration.issuer},
            endpoint,
        ),
    )
    monkeypatch.setattr(
        "apps.credentials.mcp_oauth._registration", lambda *_args: registration
    )
    monkeypatch.setattr(
        "apps.credentials.mcp_oauth._validated_https_url",
        lambda url, **_kwargs: (url, ("93.184.216.34",)),
    )
    token_calls = []
    monkeypatch.setattr(
        "apps.credentials.mcp_oauth._post_token",
        lambda *_args, **_kwargs: (
            token_calls.append(True)
            or {
                "access_token": "rechecked-access",
                "refresh_token": "rechecked-refresh",
            }
        ),
    )
    client = Client()
    access_token = DjangoJWTBackend().generate_tokens(user).access_token
    headers = {
        "HTTP_AUTHORIZATION": f"Bearer {access_token}",
        "HTTP_X_ORGANIZATION_ID": str(org.id),
        "HTTP_HOST": "127.0.0.1:8000",
    }

    def begin():
        response = client.post(
            f"/api/v1/credential-services/{service.id}/oauth/connect/",
            data=json.dumps({"name": "Recheck"}),
            content_type="application/json",
            **headers,
        )
        assert response.status_code == 200
        state = parse_qs(urlparse(response.json()["authorization_url"]).query)["state"][
            0
        ]
        cookie = next(key for key in response.cookies if key.startswith("mcp_oauth_"))
        return state, cookie, response.cookies[cookie].value

    state, cookie, binding = begin()
    registration.client_id = "changed-client"
    registration.save(update_fields=["client_id"])
    response = Client().get(
        f"/api/v1/mcp-oauth/callback/?state={state}&code=provider-code",
        HTTP_COOKIE=f"{cookie}={binding}",
        HTTP_HOST="127.0.0.1:8000",
    )
    assert "oauth_result=error" in response["Location"]
    assert token_calls == []
    registration.client_id = "recheck-client"
    registration.save(update_fields=["client_id"])

    state, cookie, binding = begin()
    Membership.objects.filter(user=user, organization=org).delete()
    with pytest.raises(Exception, match="no longer permitted"):
        CredentialOAuthSvc().complete_authorization(
            raw_state=state, binding=binding, code="provider-code"
        )
    assert token_calls == []
    Membership.objects.create(user=user, organization=org, role=MembershipRole.ADMIN)

    state, cookie, binding = begin()
    service.oauth_server_url = "https://changed.recheck.test/mcp"
    service.save(update_fields=["oauth_server_url"])
    with pytest.raises(AuthenticationError):
        CredentialOAuthSvc().complete_authorization(
            raw_state=state, binding=binding, code="provider-code"
        )
    assert token_calls == []
    service.oauth_server_url = endpoint
    service.save(update_fields=["oauth_server_url"])

    state, cookie, binding = begin()
    activation.delete()
    with pytest.raises(ConflictError):
        CredentialOAuthSvc().complete_authorization(
            raw_state=state, binding=binding, code="provider-code"
        )
    assert token_calls == []
    assert not Credential.objects.filter(service=service).exists()


@pytest.mark.django_db
def test_connect_without_plugin_creates_independent_named_accounts_and_reconnects(
    settings, monkeypatch
):
    settings.DEBUG = True
    settings.MCP_OAUTH_CALLBACK_URL = "http://127.0.0.1:8000/api/v1/mcp-oauth/callback/"
    settings.MCP_OAUTH_FRONTEND_RETURN_URL = (
        "http://127.0.0.1:8080/?settings=credentials"
    )
    user = get_user_model().objects.create_user(
        email="service-oauth@test.local", password="x"
    )
    org = Organization.objects.create(name="OAuth org", slug="oauth-service-org")
    Membership.objects.create(user=user, organization=org, role=MembershipRole.ADMIN)
    endpoint = "https://mcp.example.test/mcp"
    service = CredentialService.objects.create(
        name="Example MCP",
        slug="example-mcp",
        organization=org,
        credential_type=CredentialType.MCP_OAUTH,
        oauth_server_url=endpoint,
    )
    OrgCredentialServiceActivation.objects.create(
        organization=org, credential_service=service
    )
    registration = McpOAuthClientRegistration.objects.create(
        server_url=endpoint,
        callback_url=settings.MCP_OAUTH_CALLBACK_URL,
        issuer="https://auth.example.test",
        client_id="client",
        authorization_endpoint="https://auth.example.test/authorize",
        token_endpoint="https://auth.example.test/token",
        token_endpoint_auth_method="client_secret_post",
        encrypted_client_secret=encrypt_value("test-secret"),
    )
    monkeypatch.setattr(
        "apps.credentials.mcp_oauth._discover",
        lambda url: (
            {"_challenge_scope": ""},
            {"issuer": registration.issuer},
            endpoint,
        ),
    )
    monkeypatch.setattr(
        "apps.credentials.mcp_oauth._registration", lambda *args: registration
    )
    monkeypatch.setattr(
        "apps.credentials.mcp_oauth._validated_https_url",
        lambda url, **kwargs: (url, ("93.184.216.34",)),
    )
    token_calls = []

    def exchange_token(*args, **kwargs):
        token_calls.append(True)
        return {
            "access_token": "secret-access",
            "refresh_token": "secret-refresh",
            "expires_in": 3600,
        }

    monkeypatch.setattr("apps.credentials.mcp_oauth._post_token", exchange_token)

    client = Client()
    token = DjangoJWTBackend().generate_tokens(user).access_token
    headers = {
        "HTTP_AUTHORIZATION": f"Bearer {token}",
        "HTTP_X_ORGANIZATION_ID": str(org.id),
        "HTTP_HOST": "127.0.0.1:8000",
    }

    def begin(name: str):
        response = client.post(
            f"/api/v1/credential-services/{service.id}/oauth/connect/",
            data=json.dumps({"name": name}),
            content_type="application/json",
            **headers,
        )
        assert response.status_code == 200, response.content
        query = parse_qs(urlparse(response.json()["authorization_url"]).query)
        state = query["state"][0]
        cookie_name = next(
            name for name in response.cookies if name.startswith("mcp_oauth_")
        )
        return state, cookie_name, response.cookies[cookie_name].value

    first_state, first_cookie, first_binding = begin("Work account")
    second_state, second_cookie, second_binding = begin("Personal account")
    assert McpOAuthAuthorizationState.objects.count() == 2
    assert not Credential.objects.filter(service=service).exists()

    def callback(state: str, cookie_name: str, binding: str):
        response = client.get(
            f"/api/v1/mcp-oauth/callback/?state={state}&code=provider-code",
            HTTP_COOKIE=f"{cookie_name}={binding}",
            HTTP_HOST="127.0.0.1:8000",
        )
        assert response.status_code == 302
        assert "oauth_result=connected" in response["Location"]
        assert "provider-code" not in response["Location"]
        return response

    callback(first_state, first_cookie, first_binding)
    callback(second_state, second_cookie, second_binding)
    credentials = list(Credential.objects.filter(service=service).order_by("name"))
    assert [credential.name for credential in credentials] == [
        "Personal account",
        "Work account",
    ]
    assert credentials[0].id != credentials[1].id
    assert all(
        "server_id" not in json.loads(decrypt_value(c.encrypted_value))
        for c in credentials
    )
    listed = client.get("/api/v1/credentials/", **headers)
    assert listed.status_code == 200
    assert {item["id"] for item in listed.json()} == {str(c.id) for c in credentials}
    assert all(item["oauth_connected"] for item in listed.json())
    assert "secret-access" not in listed.content.decode()

    credential = credentials[0]
    reconnect_response = client.post(
        f"/api/v1/credentials/{credential.id}/oauth/reconnect/",
        data=json.dumps({"name": "Renamed account"}),
        content_type="application/json",
        **headers,
    )
    assert reconnect_response.status_code == 200, reconnect_response.content
    query = parse_qs(urlparse(reconnect_response.json()["authorization_url"]).query)
    cookie_name = next(
        name for name in reconnect_response.cookies if name.startswith("mcp_oauth_")
    )
    callback(
        query["state"][0], cookie_name, reconnect_response.cookies[cookie_name].value
    )
    credential.refresh_from_db()
    assert credential.name == "Renamed account"
    assert Credential.objects.filter(service=service).count() == 2

    # Invalid browser bindings, including missing cookies, fail for reconnect
    # without exchanging tokens or changing the explicit target row.
    def reject_reconnect(flow_name: str, cookie_value: str | None):
        response = client.post(
            f"/api/v1/credentials/{credential.id}/oauth/reconnect/",
            data=json.dumps({"name": flow_name}),
            content_type="application/json",
            **headers,
        )
        assert response.status_code == 200
        flow_state = parse_qs(urlparse(response.json()["authorization_url"]).query)[
            "state"
        ][0]
        flow_cookie = next(
            name for name in response.cookies if name.startswith("mcp_oauth_")
        )
        callback_client = Client()
        callback_headers = {"HTTP_HOST": "127.0.0.1:8000"}
        if cookie_value is not None:
            callback_headers["HTTP_COOKIE"] = f"{flow_cookie}={cookie_value}"
        callback = callback_client.get(
            f"/api/v1/mcp-oauth/callback/?state={flow_state}&code=provider-code",
            **callback_headers,
        )
        assert "oauth_result=error" in callback["Location"]
        credential.refresh_from_db()
        assert credential.name == "Renamed account"
        assert Credential.objects.filter(service=service).count() == 2
        assert len(token_calls) == 3

    reject_reconnect("Wrong reconnect binding", "attacker-controlled")
    reject_reconnect("Missing reconnect binding", None)

    # Commit-time ownership changes invalidate an explicit reconnect target.
    ownership_flow = client.post(
        f"/api/v1/credentials/{credential.id}/oauth/reconnect/",
        data=json.dumps({}),
        content_type="application/json",
        **headers,
    )
    ownership_state = parse_qs(
        urlparse(ownership_flow.json()["authorization_url"]).query
    )["state"][0]
    ownership_cookie = next(
        key for key in ownership_flow.cookies if key.startswith("mcp_oauth_")
    )
    owner_before = credential.user
    credential.user = get_user_model().objects.create_user(
        email="oauth-intruder@test.local", password="x"
    )
    credential.save(update_fields=["user"])
    changed_owner_callback = Client().get(
        f"/api/v1/mcp-oauth/callback/?state={ownership_state}&code=provider-code",
        HTTP_COOKIE=f"{ownership_cookie}={ownership_flow.cookies[ownership_cookie].value}",
        HTTP_HOST="127.0.0.1:8000",
    )
    assert "oauth_result=error" in changed_owner_callback["Location"]
    assert len(token_calls) == 3
    credential.user = owner_before
    credential.save(update_fields=["user"])

    # Invalid browser bindings, including missing cookies, never consume tokens.
    def reject_callback(flow_name: str, cookie_value: str | None):
        response = client.post(
            f"/api/v1/credential-services/{service.id}/oauth/connect/",
            data=json.dumps({"name": flow_name}),
            content_type="application/json",
            **headers,
        )
        assert response.status_code == 200
        flow_state = parse_qs(urlparse(response.json()["authorization_url"]).query)[
            "state"
        ][0]
        flow_cookie = next(
            name for name in response.cookies if name.startswith("mcp_oauth_")
        )
        callback_client = Client()
        callback_headers = {"HTTP_HOST": "127.0.0.1:8000"}
        if cookie_value is not None:
            callback_headers["HTTP_COOKIE"] = f"{flow_cookie}={cookie_value}"
        callback = callback_client.get(
            f"/api/v1/mcp-oauth/callback/?state={flow_state}&code=provider-code",
            **callback_headers,
        )
        assert "oauth_result=error" in callback["Location"]
        assert not Credential.objects.filter(service=service, name=flow_name).exists()
        assert len(token_calls) == 3

    reject_callback("Wrong binding", "attacker-controlled")
    reject_callback("Missing binding", None)

    # A deleted explicit reconnect target must fail closed at callback commit.
    deleted_flow = client.post(
        f"/api/v1/credentials/{credential.id}/oauth/reconnect/",
        data=json.dumps({}),
        content_type="application/json",
        **headers,
    )
    deleted_query = parse_qs(urlparse(deleted_flow.json()["authorization_url"]).query)
    deleted_cookie = next(
        key for key in deleted_flow.cookies if key.startswith("mcp_oauth_")
    )
    deleted_binding = deleted_flow.cookies[deleted_cookie].value
    credential.delete()
    deleted_callback = Client().get(
        f"/api/v1/mcp-oauth/callback/?state={deleted_query['state'][0]}&code=provider-code",
        HTTP_COOKIE=f"{deleted_cookie}={deleted_binding}",
        HTTP_HOST="127.0.0.1:8000",
    )
    assert "oauth_result=error" in deleted_callback["Location"]
    assert len(token_calls) == 3

    # Disabling blocks only fresh authorization. Reconnect remains available.
    OrgCredentialServiceActivation.objects.filter(
        organization=org, credential_service=service
    ).delete()
    inactive = client.post(
        f"/api/v1/credential-services/{service.id}/oauth/connect/",
        data=json.dumps({"name": "Third account"}),
        content_type="application/json",
        **headers,
    )
    assert inactive.status_code == 409
    reconnect = client.post(
        f"/api/v1/credentials/{credentials[1].id}/oauth/reconnect/",
        data=json.dumps({}),
        content_type="application/json",
        **headers,
    )
    assert reconnect.status_code == 200
    assert Credential.objects.filter(service=service).count() == 1
