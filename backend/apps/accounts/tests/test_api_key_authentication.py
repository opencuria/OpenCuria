"""API key authentication contract for REST requests."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from apps.accounts.models import APIKey, APIKeyPermission, User
from common.utils import generate_api_token, hash_token

pytestmark = pytest.mark.django_db

ORGANIZATIONS_URL = "/api/v1/organizations/"


@pytest.fixture
def user() -> User:
    return User.objects.create_user(email="api-key-auth@example.com")


def _issue_key(user: User, **fields) -> tuple[APIKey, str]:
    token = f"kai_{generate_api_token()}"
    fields.setdefault(
        "permissions",
        [APIKeyPermission.ORGANIZATIONS_READ.value],
    )
    key = APIKey.objects.create(
        user=user,
        name="Integration",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        **fields,
    )
    return key, token


@pytest.mark.parametrize("header", ["HTTP_AUTHORIZATION", "HTTP_X_API_KEY"])
def test_rest_accepts_active_key_and_records_last_use(user: User, header: str):
    key, token = _issue_key(user, expires_at=timezone.now() + timedelta(days=1))
    value = f"Bearer {token}" if header == "HTTP_AUTHORIZATION" else token

    response = Client().get(ORGANIZATIONS_URL, **{header: value})

    assert response.status_code == 200
    key.refresh_from_db()
    assert key.last_used_at is not None


@pytest.mark.parametrize(
    "fields",
    [
        {"is_active": False},
        {"expires_at": timezone.now() - timedelta(minutes=1)},
    ],
    ids=["revoked", "expired"],
)
def test_rest_rejects_unusable_key_without_recording_use(user: User, fields: dict):
    key, token = _issue_key(user, **fields)

    response = Client().get(ORGANIZATIONS_URL, HTTP_X_API_KEY=token)

    assert response.status_code == 401
    key.refresh_from_db()
    assert key.last_used_at is None


def test_rest_rejects_unknown_key():
    response = Client().get(ORGANIZATIONS_URL, HTTP_X_API_KEY="kai_unknown")

    assert response.status_code == 401
