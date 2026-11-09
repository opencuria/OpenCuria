"""Local account provisioning and linking after verified OIDC claims."""

from __future__ import annotations

from typing import Any

import pytest

from apps.accounts.models import SsoIdentity, User
from apps.accounts.sso import KeycloakSsoService, SsoProviderConfig
from common.exceptions import AuthenticationError

pytestmark = pytest.mark.django_db

ISSUER = "https://sso.example.com/realms/test"


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> KeycloakSsoService:
    service = KeycloakSsoService(
        SsoProviderConfig(
            enabled=True,
            provider="keycloak",
            issuer=ISSUER,
            authorization_endpoint="https://sso.example.com/auth",
            token_endpoint="https://sso.example.com/token",
            jwks_uri="https://sso.example.com/keys",
            client_id="test-client",
            scope="openid email profile",
        )
    )
    monkeypatch.setattr(service, "_exchange_code_for_id_token", lambda **kw: "id-token")
    return service


def _claims(email: str = "sso@example.com", sub: str = "subject-1", **extra) -> dict:
    return {"iss": ISSUER, "sub": sub, "email": email, "email_verified": True, **extra}


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
def test_sso_creates_local_account_bound_to_issuer_and_subject(
    service: KeycloakSsoService, monkeypatch: pytest.MonkeyPatch, with_names: bool
) -> None:
    claims = _claims()
    if with_names:
        claims.update(given_name="Sso", family_name="User")

    user = _login(service, monkeypatch, claims)

    assert User.objects.count() == 1
    user.refresh_from_db()
    assert user.email == user.username == claims["email"]
    assert user.first_name == claims.get("given_name", "")
    assert user.last_name == claims.get("family_name", "")
    assert user.is_active is True
    assert not user.has_usable_password()
    identity = SsoIdentity.objects.get()
    assert (identity.user, identity.issuer, identity.subject) == (
        user,
        ISSUER,
        "subject-1",
    )


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        ({"given_name": "Updated", "family_name": "Name"}, ("Updated", "Name")),
        ({"given_name": "Updated"}, ("Updated", "Family")),
        ({"given_name": "", "family_name": ""}, ("Original", "Family")),
        ({}, ("Original", "Family")),
    ],
)
def test_sso_updates_only_nonempty_names_on_bound_account(
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
    )
    SsoIdentity.objects.create(user=user, issuer=ISSUER, subject="subject-1")
    password_hash = user.password

    authenticated = _login(service, monkeypatch, _claims(user.email, **names))

    assert authenticated.id == user.id
    user.refresh_from_db()
    assert (user.first_name, user.last_name) == expected
    assert user.username == "local-username"
    assert user.password == password_hash


def test_sso_resolves_bound_identity_even_after_email_changes(
    service: KeycloakSsoService, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _login(service, monkeypatch, _claims("owner@example.com"))

    again = _login(
        service,
        monkeypatch,
        {"iss": ISSUER, "sub": "subject-1", "email": "renamed@example.com"},
    )

    assert again.id == owner.id
    assert User.objects.count() == 1


@pytest.mark.parametrize("verified", [False, None, "true"])
def test_sso_rejects_unverified_email_for_new_identities(
    service: KeycloakSsoService, monkeypatch: pytest.MonkeyPatch, verified
) -> None:
    victim = User.objects.create_user(email="victim@example.com", password=None)
    claims = _claims(victim.email, sub="attacker")
    if verified is None:
        claims.pop("email_verified")
    else:
        claims["email_verified"] = verified

    with pytest.raises(AuthenticationError) as error:
        _login(service, monkeypatch, claims)

    assert error.value.message == "SSO account email is not verified"
    assert not SsoIdentity.objects.exists()
    assert User.objects.count() == 1


@pytest.mark.parametrize(
    "fields",
    [
        {"password": "local-password"},
        {"password": None, "is_staff": True},
        {"password": None, "is_superuser": True},
    ],
    ids=["password", "staff", "superuser"],
)
def test_sso_never_links_password_or_staff_accounts_by_email(
    service: KeycloakSsoService, monkeypatch: pytest.MonkeyPatch, fields: dict
) -> None:
    existing = User.objects.create_user(email="admin@example.com", **fields)

    with pytest.raises(AuthenticationError) as error:
        _login(service, monkeypatch, _claims(existing.email, sub="other-subject"))

    assert "already exists" in error.value.message
    assert not SsoIdentity.objects.exists()
    assert User.objects.count() == 1


def test_sso_links_existing_sso_only_account_once_by_verified_email(
    service: KeycloakSsoService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Accounts provisioned by SSO before identities existed have no password."""
    legacy = User.objects.create(email="legacy@example.com", username="legacy")
    assert legacy.password == ""

    linked = _login(service, monkeypatch, _claims(legacy.email, sub="legacy-sub"))

    assert linked.id == legacy.id
    assert SsoIdentity.objects.get().subject == "legacy-sub"

    # Once bound, another subject cannot claim the account through its email.
    with pytest.raises(AuthenticationError) as error:
        _login(service, monkeypatch, _claims(legacy.email, sub="second-sub"))
    assert "already exists" in error.value.message
    assert SsoIdentity.objects.count() == 1


@pytest.mark.parametrize(
    "claims",
    [
        {"iss": ISSUER, "sub": "subject-1", "email_verified": True},
        {"iss": ISSUER, "email": "sso@example.com", "email_verified": True},
        {"iss": ISSUER, "sub": "", "email": "sso@example.com"},
    ],
    ids=["no-email", "no-subject", "empty-subject"],
)
def test_sso_incomplete_claims_do_not_create_account(
    service: KeycloakSsoService, monkeypatch: pytest.MonkeyPatch, claims: dict
) -> None:
    with pytest.raises(AuthenticationError):
        _login(service, monkeypatch, claims)

    assert User.objects.count() == 0
    assert not SsoIdentity.objects.exists()


def test_sso_rejects_disabled_account_without_changing_it(
    service: KeycloakSsoService, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = User.objects.create_user(email="disabled@example.com", is_active=False)
    SsoIdentity.objects.create(user=user, issuer=ISSUER, subject="subject-1")

    with pytest.raises(AuthenticationError) as error:
        _login(service, monkeypatch, _claims(user.email, given_name="Updated"))

    assert error.value.message == "Account is disabled"
    user.refresh_from_db()
    assert user.first_name == ""
    assert user.is_active is False
