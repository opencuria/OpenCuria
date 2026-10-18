"""REST contract and authorization tests for named provider accounts."""

from __future__ import annotations

import json
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.models import APIKey, APIKeyPermission
from apps.harness.provider_accounts import ProviderAccountService
from apps.organizations.models import Membership, MembershipRole
from common.utils import generate_api_token, hash_token


def _client(user, org):
    token = generate_api_token()
    APIKey.objects.create(
        user=user,
        name=f"accounts-{uuid.uuid4().hex[:8]}",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=[APIKeyPermission.HARNESS_PROVIDERS.value],
    )
    return Client(HTTP_X_API_KEY=token, HTTP_X_ORGANIZATION_ID=str(org.id))


@pytest.fixture
def accounts_setup(organization):
    users = get_user_model()
    admin = users.objects.create_user(
        email=f"pa-{uuid.uuid4().hex[:8]}@example.com", password="x"
    )
    member = users.objects.create_user(
        email=f"pm-{uuid.uuid4().hex[:8]}@example.com", password="x"
    )
    outsider = users.objects.create_user(
        email=f"po-{uuid.uuid4().hex[:8]}@example.com", password="x"
    )
    Membership.objects.create(
        user=admin, organization=organization, role=MembershipRole.ADMIN
    )
    Membership.objects.create(
        user=member, organization=organization, role=MembershipRole.MEMBER
    )
    return organization, admin, member, outsider


@pytest.mark.django_db(transaction=True)
def test_account_api_visibility_admin_and_provider_mismatch(accounts_setup):
    org, admin, member, _outsider = accounts_setup
    member_client = _client(member, org)
    admin_client = _client(admin, org)
    create = member_client.post(
        "/api/v1/provider-config/accounts/openrouter/",
        data=json.dumps({"label": "Personal", "api_key": "sk-private-1234"}),
        content_type="application/json",
    )
    assert create.status_code == 201, create.content[:500]
    personal_id = create.json()["id"]
    assert create.json()["is_active"] is True
    assert create.json()["api_key_hint"] == "••••1234"
    assert "sk-private-1234" not in create.content.decode()
    assert admin_client.get("/api/v1/provider-config/accounts/").json() == []

    shared = admin_client.post(
        "/api/v1/provider-config/accounts/openrouter/",
        data=json.dumps({"label": "Org", "shared": True, "api_key": "sk-org-5678"}),
        content_type="application/json",
    )
    assert shared.status_code == 201
    assert shared.json()["scope"] == "organization"
    assert "sk-org-5678" not in shared.content.decode()
    visible_ids = {
        row["id"]
        for row in member_client.get("/api/v1/provider-config/accounts/").json()
    }
    assert visible_ids == {personal_id, shared.json()["id"]}

    foreign_update = admin_client.put(
        f"/api/v1/provider-config/accounts/openrouter/{personal_id}/",
        data=json.dumps({"api_key": "sk-admin-must-not-see"}),
        content_type="application/json",
    )
    assert foreign_update.status_code == 404
    mismatch = member_client.put(
        f"/api/v1/provider-config/accounts/chatgpt/{personal_id}/",
        data=json.dumps({"label": "mismatch"}),
        content_type="application/json",
    )
    assert mismatch.status_code == 404

    selected_shared = member_client.put(
        "/api/v1/provider-config/selection/openrouter/",
        data=json.dumps({"account_id": shared.json()["id"]}),
        content_type="application/json",
    )
    assert selected_shared.status_code == 204
    explicit_none = member_client.put(
        "/api/v1/provider-config/selection/openrouter/",
        data=json.dumps({"account_id": None}),
        content_type="application/json",
    )
    assert explicit_none.status_code == 204
    assert all(
        not item["is_active"]
        for item in member_client.get("/api/v1/provider-config/accounts/").json()
    )


@pytest.mark.django_db(transaction=True)
def test_legacy_provider_upsert_never_changes_shared_account(accounts_setup):
    org, admin, member, _outsider = accounts_setup
    accounts = ProviderAccountService()
    shared = accounts.save_account(
        organization_id=org.id,
        user=admin,
        provider="openrouter",
        payload={"label": "Shared", "shared": True, "api_key": "sk-shared"},
    )
    client = _client(member, org)
    response = client.put(
        "/api/v1/provider-config/providers/openrouter/",
        data=json.dumps({"api_key": "sk-member-creates"}),
        content_type="application/json",
    )
    # The member's first provider selection is shared, so legacy writes fail
    # closed rather than modifying the org-wide account.
    assert response.status_code == 403
    shared.refresh_from_db()
    assert accounts.get_credentials(shared)["api_key"] == "sk-shared"
    assert accounts.list_accounts(org.id, member.id, "openrouter")[0]["id"] == str(
        shared.id
    )


@pytest.mark.django_db(transaction=True)
def test_list_provider_accounts_omits_disconnected_and_hides_other_personal(
    accounts_setup,
):
    org, admin, member, _outsider = accounts_setup
    personal = ProviderAccountService().save_account(
        organization_id=org.id,
        user=member,
        provider="openai-compatible",
        payload={
            "label": "Optional key endpoint",
            "base_url": "https://local.example/v1",
        },
    )
    disconnected = ProviderAccountService().save_account(
        organization_id=org.id,
        user=admin,
        provider="openrouter",
        payload={"label": "Org account", "shared": True, "api_key": "sk-org"},
    )
    ProviderAccountService().delete_account(org.id, admin.id, disconnected.id)
    rows = _client(member, org).get("/api/v1/provider-config/accounts/").json()
    assert [row["id"] for row in rows] == [str(personal.id)]
    assert rows[0]["connected"] is True
