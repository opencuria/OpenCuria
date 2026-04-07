"""Organization API permission regression tests."""

from __future__ import annotations

import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.models import APIKey, APIKeyPermission
from apps.organizations.models import Membership, MembershipRole, Organization
from common.utils import generate_api_token, hash_token


@pytest.fixture
def client() -> Client:
    return Client()


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
def test_api_key_without_org_write_cannot_create_organization(client: Client):
    user = get_user_model().objects.create_user(
        email="org-perms@test.local",
        password="secret",
    )
    token = _create_api_key(user=user, permissions=[])

    response = client.post(
        "/api/v1/organizations/",
        data=json.dumps({"name": "Blocked Org"}),
        content_type="application/json",
        HTTP_X_API_KEY=token,
    )

    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"


@pytest.mark.django_db
def test_api_key_without_org_write_cannot_update_workspace_policy(client: Client):
    user = get_user_model().objects.create_user(
        email="org-policy-no-write@test.local",
        password="secret",
    )
    org = Organization.objects.create(name="Policy Org", slug="policy-org")
    Membership.objects.create(user=user, organization=org, role=MembershipRole.ADMIN)
    token = _create_api_key(user=user, permissions=[APIKeyPermission.ORGANIZATIONS_READ.value])

    response = client.patch(
        f"/api/v1/organizations/{org.id}/workspace-policy/",
        data=json.dumps({"workspace_auto_stop_timeout_minutes": 60}),
        content_type="application/json",
        HTTP_X_API_KEY=token,
        HTTP_X_ORGANIZATION_ID=str(org.id),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"


@pytest.mark.django_db
def test_admin_can_update_workspace_policy(client: Client):
    user = get_user_model().objects.create_user(
        email="org-policy-admin@test.local",
        password="secret",
    )
    org = Organization.objects.create(name="Policy Org 2", slug="policy-org-2")
    Membership.objects.create(user=user, organization=org, role=MembershipRole.ADMIN)
    token = _create_api_key(
        user=user,
        permissions=[
            APIKeyPermission.ORGANIZATIONS_READ.value,
            APIKeyPermission.ORGANIZATIONS_WRITE.value,
        ],
    )

    response = client.patch(
        f"/api/v1/organizations/{org.id}/workspace-policy/",
        data=json.dumps({"workspace_auto_stop_timeout_minutes": 45}),
        content_type="application/json",
        HTTP_X_API_KEY=token,
        HTTP_X_ORGANIZATION_ID=str(org.id),
    )

    assert response.status_code == 200
    org.refresh_from_db()
    assert org.workspace_auto_stop_timeout_minutes == 45
    assert response.json()["workspace_auto_stop_timeout_minutes"] == 45
