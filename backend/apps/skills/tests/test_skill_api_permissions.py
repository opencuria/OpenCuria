"""Skill API permission regression tests."""

from __future__ import annotations

import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.models import APIKey
from apps.organizations.models import Membership, MembershipRole, Organization
from common.utils import generate_api_token, hash_token


@pytest.fixture
def client() -> Client:
    return Client()


def _auth_headers(token: str, org_id: str) -> dict[str, str]:
    return {
        "HTTP_X_API_KEY": token,
        "HTTP_X_ORGANIZATION_ID": org_id,
    }


def _create_api_key(*, user, permissions: list[str]) -> str:
    token = generate_api_token()
    APIKey.objects.create(
        user=user,
        name="test-key",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=permissions,
    )
    return token


@pytest.mark.django_db
def test_api_key_without_skills_write_cannot_create_skill(client: Client):
    user = get_user_model().objects.create_user(
        email="skills@test.local",
        password="secret",
    )
    org = Organization.objects.create(name="Skills Org", slug="skills-org")
    Membership.objects.create(user=user, organization=org, role=MembershipRole.ADMIN)
    token = _create_api_key(user=user, permissions=[])

    response = client.post(
        "/api/v1/skills/",
        data=json.dumps(
            {
                "name": "Blocked Skill",
                "body": "Do not allow this",
                "organization_skill": False,
            }
        ),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"
