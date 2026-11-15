"""Characterize the membership-aware profile exposed at the auth URL."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.accounts.auth_backends import DjangoJWTBackend
from apps.accounts.models import APIKey, User
from apps.organizations.models import Membership, MembershipRole, Organization
from common.utils import generate_api_token, hash_token

PROFILE_URL = "/api/v1/auth/me/"

pytestmark = pytest.mark.django_db


@pytest.fixture
def user() -> User:
    return User.objects.create_user(
        email="member-profile@example.com", first_name="Member", last_name="Profile"
    )


@pytest.fixture(params=["jwt", "api-key-bearer", "api-key-header"])
def auth_headers(request: pytest.FixtureRequest, user: User) -> dict[str, str]:
    if request.param == "jwt":
        token = DjangoJWTBackend().generate_tokens(user).access_token
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    token = f"kai_{generate_api_token()}"
    APIKey.objects.create(
        user=user,
        name="Profile reader",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=[],
    )
    if request.param == "api-key-bearer":
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}
    return {"HTTP_X_API_KEY": token}


def test_profile_returns_only_own_memberships_in_existing_order(
    client: Client, user: User, auth_headers: dict[str, str]
) -> None:
    admin_org = Organization.objects.create(name="Admin org", slug="admin-org")
    member_org = Organization.objects.create(name="Member org", slug="member-org")
    Membership.objects.create(
        user=user, organization=admin_org, role=MembershipRole.ADMIN
    )
    Membership.objects.create(
        user=user, organization=member_org, role=MembershipRole.MEMBER
    )
    other_user = User.objects.create_user(email="other-profile@example.com")
    hidden_org = Organization.objects.create(name="Hidden org", slug="hidden-org")
    Membership.objects.create(user=other_user, organization=hidden_org)

    response = client.get(PROFILE_URL, **auth_headers)

    assert response.status_code == 200
    profile = response.json()
    organizations = profile.pop("organizations")
    assert profile == {
        "id": user.id,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
    }
    assert len(organizations) == 2
    for actual, org, role in zip(
        organizations,
        [member_org, admin_org],
        [MembershipRole.MEMBER, MembershipRole.ADMIN],
    ):
        assert parse_datetime(actual.pop("created_at")) == org.created_at.replace(
            microsecond=org.created_at.microsecond // 1000 * 1000
        )
        assert actual == {
            "id": str(org.id),
            "name": org.name,
            "slug": org.slug,
            "role": role,
        }


def test_profile_without_memberships(
    client: Client, user: User, auth_headers: dict[str, str]
) -> None:
    response = client.get(PROFILE_URL, **auth_headers)

    assert response.status_code == 200
    assert response.json() == {
        "id": user.id,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "organizations": [],
    }


@pytest.mark.parametrize(
    "case", ["missing", "invalid-jwt", "revoked-key", "expired-key"]
)
def test_profile_rejects_invalid_authentication(
    client: Client, user: User, case: str
) -> None:
    headers = {}
    if case == "invalid-jwt":
        headers = {"HTTP_AUTHORIZATION": "Bearer invalid-jwt"}
    elif case in {"revoked-key", "expired-key"}:
        token = f"kai_{generate_api_token()}"
        APIKey.objects.create(
            user=user,
            name="Unusable profile key",
            key_hash=hash_token(token),
            key_prefix=token[:12],
            is_active=case != "revoked-key",
            expires_at=(
                timezone.now() - timedelta(minutes=1) if case == "expired-key" else None
            ),
        )
        headers = {"HTTP_X_API_KEY": token}

    response = client.get(PROFILE_URL, **headers)

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_profile_preserves_url_name_and_openapi_contract(client: Client) -> None:
    assert reverse("api:me") == PROFILE_URL
    schema = client.get("/api/v1/openapi.json").json()
    assert schema["paths"][PROFILE_URL] == {
        "get": {
            "operationId": "apps_accounts_api_me",
            "summary": "Get current user",
            "parameters": [],
            "responses": {
                "200": {
                    "description": "OK",
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/UserWithOrgsOut"}
                        }
                    },
                }
            },
            "description": (
                "Return the authenticated user's profile with organization memberships."
            ),
            "tags": ["auth"],
            "security": [{"JWTAuth": []}, {"APIKeyBearer": []}, {"APIKeyInHeader": []}],
        }
    }
