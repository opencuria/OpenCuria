"""
Tests for credential service API endpoints.
"""

from __future__ import annotations

import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.models import APIKey, APIKeyPermission
from apps.credentials.models import CredentialService, OrgCredentialServiceActivation
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
def test_admin_can_create_org_credential_service(client: Client):
    user_model = get_user_model()
    admin = user_model.objects.create_user(email="admin@org.test", password="secret")
    admin.is_staff = True
    admin.save(update_fields=["is_staff"])
    org = Organization.objects.create(name="Acme", slug="acme")
    Membership.objects.create(user=admin, organization=org, role=MembershipRole.ADMIN)
    token = _create_api_key(
        user=admin,
        permissions=[
            APIKeyPermission.ORG_CREDENTIAL_SERVICES_WRITE.value,
        ],
    )

    response = client.post(
        "/api/v1/org-credential-services/",
        data=json.dumps(
            {
                "name": "GitHub Token",
                "credential_type": "env",
                "env_var_name": "GITHUB_TOKEN",
                "description": "Token for GitHub API",
                "label": "Personal Access Token",
            }
        ),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["name"] == "GitHub Token"
    assert payload["slug"] == "github-token"
    assert payload["credential_type"] == "env"
    assert payload["env_var_name"] == "GITHUB_TOKEN"
    assert payload["is_active"] is True
    service = CredentialService.objects.get(id=payload["id"])
    assert OrgCredentialServiceActivation.objects.filter(
        organization=org,
        credential_service=service,
    ).exists()


@pytest.mark.django_db
def test_member_cannot_create_org_credential_service(client: Client):
    user_model = get_user_model()
    member = user_model.objects.create_user(email="member@org.test", password="secret")
    org = Organization.objects.create(name="Beta", slug="beta")
    Membership.objects.create(user=member, organization=org, role=MembershipRole.MEMBER)
    token = _create_api_key(
        user=member,
        permissions=[
            APIKeyPermission.ORG_CREDENTIAL_SERVICES_WRITE.value,
        ],
    )

    response = client.post(
        "/api/v1/org-credential-services/",
        data=json.dumps(
            {
                "name": "OpenAI API Key",
                "credential_type": "env",
                "env_var_name": "OPENAI_API_KEY",
            }
        ),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


@pytest.mark.django_db
def test_non_staff_admin_cannot_create_org_credential_service(client: Client):
    user_model = get_user_model()
    admin = user_model.objects.create_user(email="admin-nonstaff@org.test", password="secret")
    org = Organization.objects.create(name="Gamma", slug="gamma")
    Membership.objects.create(user=admin, organization=org, role=MembershipRole.ADMIN)
    token = _create_api_key(
        user=admin,
        permissions=[APIKeyPermission.ORG_CREDENTIAL_SERVICES_WRITE.value],
    )

    response = client.post(
        "/api/v1/org-credential-services/",
        data=json.dumps(
            {
                "name": "Azure Token",
                "credential_type": "env",
                "env_var_name": "AZURE_TOKEN",
            }
        ),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Only staff users can create credential services"


@pytest.mark.django_db
def test_non_staff_admin_cannot_toggle_org_credential_service_activation(client: Client):
    user_model = get_user_model()
    admin = user_model.objects.create_user(email="toggle-admin@org.test", password="secret")
    org = Organization.objects.create(name="Delta", slug="delta")
    Membership.objects.create(user=admin, organization=org, role=MembershipRole.ADMIN)
    token = _create_api_key(
        user=admin,
        permissions=[APIKeyPermission.ORG_CREDENTIAL_SERVICES_WRITE.value],
    )
    service = CredentialService.objects.create(
        name="GitHub Token",
        slug="github-token-test",
        credential_type="env",
        env_var_name="GITHUB_TOKEN",
    )

    response = client.post(
        f"/api/v1/org-credential-services/{service.id}/activation/",
        data=json.dumps({"active": True}),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Only staff users can modify credential service activation"
