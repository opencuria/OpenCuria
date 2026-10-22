"""New organizations start with every global credential service activated."""

from __future__ import annotations

import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.models import APIKey, APIKeyPermission
from apps.credentials.enums import CredentialType
from apps.credentials.models import CredentialService, OrgCredentialServiceActivation
from apps.organizations.models import Organization
from common.utils import generate_api_token, hash_token


@pytest.mark.django_db
def test_created_organization_activates_all_global_services() -> None:
    """Global services (incl. provider secrets) are seeded; foreign ones not."""
    user = get_user_model().objects.create_user(
        email="seed@org.test", password="secret"
    )
    token = generate_api_token()
    APIKey.objects.create(
        user=user,
        name="test-key",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=[APIKeyPermission.ORGANIZATIONS_WRITE.value],
    )
    global_env = CredentialService.objects.create(
        name="Seed Env",
        slug="seed-env",
        credential_type=CredentialType.ENV,
        env_var_name="SEED_ENV",
    )
    global_secret = CredentialService.objects.create(
        name="Seed Provider",
        slug="seed-provider",
        credential_type=CredentialType.PROVIDER_SECRET,
    )
    other_org = Organization.objects.create(name="Other", slug="other-seed")
    foreign = CredentialService.objects.create(
        organization=other_org,
        name="Foreign",
        slug="foreign",
        credential_type=CredentialType.ENV,
        env_var_name="FOREIGN",
    )

    response = Client().post(
        "/api/v1/organizations/",
        data=json.dumps({"name": "Seeded Org"}),
        content_type="application/json",
        HTTP_X_API_KEY=token,
    )

    assert response.status_code == 201, response.content[:500]
    activated = set(
        OrgCredentialServiceActivation.objects.filter(
            organization_id=response.json()["id"]
        ).values_list("credential_service_id", flat=True)
    )
    global_ids = set(
        CredentialService.objects.filter(organization__isnull=True).values_list(
            "id", flat=True
        )
    )
    assert {global_env.id, global_secret.id} <= activated
    assert activated == global_ids
    assert foreign.id not in activated
