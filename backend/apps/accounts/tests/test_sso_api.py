from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings


@pytest.fixture
def client() -> Client:
    return Client()


@override_settings(SSO_ENABLED=False)
@pytest.mark.django_db
def test_auth_providers_reports_sso_disabled(client: Client):
    response = client.get("/api/v1/auth/providers/")
    assert response.status_code == 200
    payload = response.json()
    assert payload["password_enabled"] is True
    assert payload["sso"]["enabled"] is False


@override_settings(
    SSO_ENABLED=True,
    SSO_PROVIDER="keycloak",
    SSO_KEYCLOAK_BASE_URL="http://localhost:18080",
    SSO_KEYCLOAK_REALM="opencuria",
    SSO_KEYCLOAK_CLIENT_ID="opencuria-web",
    SSO_KEYCLOAK_SCOPE="openid email profile",
)
@pytest.mark.django_db
def test_auth_providers_reports_keycloak_enabled(client: Client):
    response = client.get("/api/v1/auth/providers/")
    assert response.status_code == 200
    payload = response.json()
    assert payload["sso"]["enabled"] is True
    assert payload["sso"]["provider"] == "keycloak"
    assert payload["sso"]["client_id"] == "opencuria-web"
    assert payload["sso"]["authorization_endpoint"].endswith(
        "/realms/opencuria/protocol/openid-connect/auth"
    )


@override_settings(SSO_ENABLED=False)
@pytest.mark.django_db
def test_sso_callback_rejected_when_disabled(client: Client):
    response = client.post(
        "/api/v1/auth/sso/callback/",
        data={"code": "abc", "redirect_uri": "http://localhost:5173/sso/callback"},
        content_type="application/json",
    )
    assert response.status_code == 403
    assert response.json()["code"] == "sso_disabled"


@override_settings(
    SSO_ENABLED=True,
    SSO_PROVIDER="keycloak",
    SSO_KEYCLOAK_BASE_URL="http://localhost:18080",
    SSO_KEYCLOAK_REALM="opencuria",
    SSO_KEYCLOAK_CLIENT_ID="opencuria-web",
)
@pytest.mark.django_db
def test_sso_callback_issues_local_tokens(client: Client, monkeypatch):
    user = get_user_model().objects.create_user(
        email="sso-user@example.com",
        password=None,
        first_name="Sso",
        last_name="User",
    )

    def _fake_exchange(self, *, code: str, redirect_uri: str):  # noqa: ARG001
        return user

    monkeypatch.setattr(
        "apps.accounts.sso.KeycloakSsoService.exchange_code_for_user",
        _fake_exchange,
    )

    response = client.post(
        "/api/v1/auth/sso/callback/",
        data={"code": "abc", "redirect_uri": "http://localhost:5173/sso/callback"},
        content_type="application/json",
    )
    assert response.status_code == 200
    payload = response.json()
    assert "access_token" in payload
    assert "refresh_token" in payload
