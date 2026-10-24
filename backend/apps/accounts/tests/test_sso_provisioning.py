"""Characterize local account provisioning after verified OIDC claims."""

from __future__ import annotations

from typing import Any

import pytest

from apps.accounts.models import User
from apps.accounts.sso import KeycloakSsoService, SsoProviderConfig
from common.exceptions import AuthenticationError

pytestmark = pytest.mark.django_db


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> KeycloakSsoService:
    service = KeycloakSsoService(
        SsoProviderConfig(
            enabled=True,
            provider="keycloak",
            issuer="https://sso.example.com/realms/test",
            authorization_endpoint="https://sso.example.com/auth",
            token_endpoint="https://sso.example.com/token",
            jwks_uri="https://sso.example.com/keys",
            client_id="test-client",
            scope="openid email profile",
        )
    )
    monkeypatch.setattr(service, "_exchange_code_for_id_token", lambda **kw: "id-token")
    return service


def _login(
    service: KeycloakSsoService,
    monkeypatch: pytest.MonkeyPatch,
    claims: dict[str, Any],
) -> User:
    monkeypatch.setattr(service, "_validate_id_token", lambda token: claims)
    return service.exchange_code_for_user(
        code="authorization-code", redirect_uri="https://app.example.com/sso/callback"
    )


@pytest.mark.parametrize("with_names", [False, True])
def test_sso_creates_local_account(
    service: KeycloakSsoService, monkeypatch: pytest.MonkeyPatch, with_names: bool
) -> None:
    claims = {"email": "sso@example.com"}
    if with_names:
        claims.update(given_name="Sso", family_name="User")

    user = _login(service, monkeypatch, claims)

    assert User.objects.count() == 1
    user.refresh_from_db()
    assert user.email == user.username == claims["email"]
    assert user.first_name == claims.get("given_name", "")
    assert user.last_name == claims.get("family_name", "")
    assert user.is_active is True


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        ({"given_name": "Updated", "family_name": "Name"}, ("Updated", "Name")),
        ({"given_name": "Updated"}, ("Updated", "Family")),
        ({"given_name": "", "family_name": ""}, ("Original", "Family")),
        ({}, ("Original", "Family")),
        ({"given_name": "Original", "family_name": "Family"}, ("Original", "Family")),
    ],
)
def test_sso_updates_only_nonempty_names_on_existing_account(
    service: KeycloakSsoService,
    monkeypatch: pytest.MonkeyPatch,
    names: dict[str, str],
    expected: tuple[str, str],
) -> None:
    user = User.objects.create_user(
        email="existing@example.com",
        username="local-username",
        password="local-password",
        first_name="Original",
        last_name="Family",
        is_staff=True,
    )
    password_hash = user.password

    authenticated = _login(service, monkeypatch, {"email": user.email, **names})

    assert authenticated.id == user.id
    assert User.objects.count() == 1
    user.refresh_from_db()
    assert (user.first_name, user.last_name) == expected
    assert user.username == "local-username"
    assert user.password == password_hash
    assert user.is_staff is True


def test_sso_missing_email_does_not_create_account(
    service: KeycloakSsoService, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(AuthenticationError) as error:
        _login(service, monkeypatch, {"given_name": "Unknown"})

    assert error.value.message == "SSO token did not include an email claim"
    assert User.objects.count() == 0


def test_sso_rejects_disabled_account_after_profile_sync(
    service: KeycloakSsoService, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = User.objects.create_user(email="disabled@example.com", is_active=False)

    with pytest.raises(AuthenticationError) as error:
        _login(service, monkeypatch, {"email": user.email, "given_name": "Updated"})

    assert error.value.message == "Account is disabled"
    user.refresh_from_db()
    assert user.first_name == "Updated"
    assert user.is_active is False
    assert User.objects.count() == 1
