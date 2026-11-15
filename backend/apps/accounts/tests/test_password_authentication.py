"""Password and local JWT contracts, independent of user persistence details."""

from __future__ import annotations

from datetime import timedelta

import jwt
import pytest
from django.conf import settings
from django.contrib.auth.hashers import PBKDF2PasswordHasher
from django.test import Client, override_settings
from django.utils import timezone

from apps.accounts.auth_backends import DjangoJWTBackend
from apps.accounts.models import User
from common.exceptions import AuthenticationError

pytestmark = pytest.mark.django_db


@pytest.fixture
def user() -> User:
    return User.objects.create_user(
        email="password-auth@example.com",
        password="correct-password",
        first_name="Local",
    )


def test_login_refresh_and_authenticated_profile(client: Client, user: User) -> None:
    response = client.post(
        "/api/v1/auth/login/",
        data={"email": user.email, "password": "correct-password"},
        content_type="application/json",
    )
    assert response.status_code == 200
    tokens = response.json()
    assert set(tokens) == {"access_token", "refresh_token"}
    access = jwt.decode(
        tokens["access_token"], settings.SECRET_KEY, algorithms=["HS256"]
    )
    refresh = jwt.decode(
        tokens["refresh_token"], settings.SECRET_KEY, algorithms=["HS256"]
    )
    assert access["sub"] == refresh["sub"] == str(user.id)
    assert access["type"] == "access"
    assert access["email"] == user.email
    assert refresh["type"] == "refresh"
    assert "email" not in refresh
    assert access["exp"] - access["iat"] == 60 * getattr(
        settings, "JWT_ACCESS_TOKEN_LIFETIME_MINUTES", 30
    )
    assert refresh["exp"] - refresh["iat"] == 86400 * getattr(
        settings, "JWT_REFRESH_TOKEN_LIFETIME_DAYS", 7
    )

    response = client.post(
        "/api/v1/auth/refresh/",
        data={"refresh_token": tokens["refresh_token"]},
        content_type="application/json",
    )
    assert response.status_code == 200
    refreshed = response.json()
    assert set(refreshed) == set(tokens)
    backend = DjangoJWTBackend()
    assert backend.validate_access_token(refreshed["access_token"]) == user
    assert backend.refresh_tokens(refreshed["refresh_token"]).access_token

    response = client.get(
        "/api/v1/auth/me/",
        HTTP_AUTHORIZATION=f"Bearer {refreshed['access_token']}",
    )
    assert response.status_code == 200
    assert response.json() == {
        "id": user.id,
        "email": user.email,
        "first_name": "Local",
        "last_name": "",
        "organizations": [],
    }


@pytest.mark.parametrize(
    ("case", "detail"),
    [
        ("unknown-email", "Invalid email or password"),
        ("wrong-password", "Invalid email or password"),
        ("unusable-password", "Invalid email or password"),
        ("disabled", "Account is disabled"),
        ("disabled-wrong-password", "Invalid email or password"),
    ],
)
def test_login_rejections(client: Client, user: User, case: str, detail: str) -> None:
    email, password = user.email, "correct-password"
    if case == "unknown-email":
        email = "unknown@example.com"
    if case in {"wrong-password", "disabled-wrong-password"}:
        password = "wrong-password"
    if case.startswith("disabled"):
        user.is_active = False
        user.save(update_fields=["is_active"])
    if case == "unusable-password":
        user.set_unusable_password()
        user.save(update_fields=["password"])

    response = client.post(
        "/api/v1/auth/login/",
        data={"email": email, "password": password},
        content_type="application/json",
    )
    assert response.status_code == 401
    assert response.json() == {"detail": detail, "code": "authentication_error"}


@override_settings(
    PASSWORD_HASHERS=[
        "django.contrib.auth.hashers.MD5PasswordHasher",
        "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    ]
)
def test_login_persists_django_password_hash_upgrade(user: User) -> None:
    user.password = PBKDF2PasswordHasher().encode(
        "correct-password", salt="legacy-salt", iterations=1
    )
    user.save(update_fields=["password"])
    legacy_hash = user.password

    assert DjangoJWTBackend().authenticate(user.email, "correct-password") == user

    user.refresh_from_db()
    assert user.password != legacy_hash
    assert user.password.startswith("md5$")  # The preferred hasher in tests.
    assert user.check_password("correct-password")


@pytest.mark.parametrize("token_type", ["access", "refresh"])
@pytest.mark.parametrize(
    ("case", "detail"),
    [
        ("expired", "{label} token has expired"),
        ("bad-signature", "Invalid {token_type} token"),
        ("malformed", "Invalid {token_type} token"),
        ("wrong-type", "Invalid token type"),
        ("missing-subject", "Invalid token payload"),
        ("invalid-subject", "User not found"),
        ("deleted-user", "User not found"),
        ("disabled-user", "Account is disabled"),
    ],
)
def test_token_rejections(
    client: Client, user: User, token_type: str, case: str, detail: str
) -> None:
    payload = {
        "sub": str(user.id),
        "type": token_type,
        "exp": timezone.now() + timedelta(minutes=5),
    }
    secret = settings.SECRET_KEY
    if case == "expired":
        payload["exp"] = timezone.now() - timedelta(seconds=1)
    elif case == "bad-signature":
        secret = "different-signing-secret-for-tests"
    elif case == "wrong-type":
        payload["type"] = "refresh" if token_type == "access" else "access"
    elif case == "missing-subject":
        del payload["sub"]
    elif case == "invalid-subject":
        payload["sub"] = "not-an-integer"
    elif case == "deleted-user":
        user.delete()
    elif case == "disabled-user":
        user.is_active = False
        user.save(update_fields=["is_active"])
    token = "not-a-jwt" if case == "malformed" else jwt.encode(payload, secret)
    expected = detail.format(label=token_type.title(), token_type=token_type)

    backend = DjangoJWTBackend()
    validate = (
        backend.validate_access_token
        if token_type == "access"
        else backend.refresh_tokens
    )
    with pytest.raises(AuthenticationError) as error:
        validate(token)
    assert error.value.message == expected
    assert error.value.code == "authentication_error"

    if token_type == "access":
        response = client.get("/api/v1/auth/me/", HTTP_AUTHORIZATION=f"Bearer {token}")
    else:
        response = client.post(
            "/api/v1/auth/refresh/",
            data={"refresh_token": token},
            content_type="application/json",
        )
        assert response.json() == {
            "detail": expected,
            "code": "authentication_error",
        }
    assert response.status_code == 401
