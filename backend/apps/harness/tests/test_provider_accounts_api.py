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

ACCOUNTS = "/api/v1/provider-config/accounts/"


def _client(user, org, permissions=None):
    token = generate_api_token()
    APIKey.objects.create(
        user=user,
        name=f"accounts-{uuid.uuid4().hex[:8]}",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=permissions or [APIKeyPermission.HARNESS_PROVIDERS.value],
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


def _post(client, provider, values):
    return client.post(
        f"{ACCOUNTS}{provider}/",
        data=json.dumps(values),
        content_type="application/json",
    )


def _put(client, provider, account_id, values):
    return client.put(
        f"{ACCOUNTS}{provider}/{account_id}/",
        data=json.dumps(values),
        content_type="application/json",
    )


@pytest.mark.django_db(transaction=True)
def test_account_create_visibility_shared_write_acl_and_secret_masking(accounts_setup):
    org, admin, member, _outsider = accounts_setup
    member_client = _client(member, org)
    admin_client = _client(admin, org)

    personal = _post(
        member_client,
        "openrouter",
        {"label": "Personal", "api_key": "sk-private-1234"},
    )
    assert personal.status_code == 201, personal.content[:500]
    personal_body = personal.json()
    assert personal_body["is_active"] is True
    assert personal_body["scope"] == "personal"
    assert personal_body["api_key_hint"] == "••••1234"
    assert "sk-private-1234" not in personal.content.decode()
    personal_id = personal_body["id"]
    assert admin_client.get(ACCOUNTS).json() == []

    shared = _post(
        admin_client,
        "openrouter",
        {"label": "Org", "shared": True, "api_key": "sk-org-5678"},
    )
    assert shared.status_code == 201, shared.content[:500]
    assert shared.json()["scope"] == "organization"
    assert "sk-org-5678" not in shared.content.decode()
    assert {row["id"] for row in member_client.get(ACCOUNTS).json()} == {
        personal_id,
        shared.json()["id"],
    }

    denied_write = _put(
        member_client,
        "openrouter",
        shared.json()["id"],
        {"api_key": "sk-member-must-not-change"},
    )
    assert denied_write.status_code == 403
    shared_account = ProviderAccountService().get_account(
        org.id, member.id, shared.json()["id"]
    )
    assert (
        ProviderAccountService().get_credentials(shared_account)["api_key"]
        == "sk-org-5678"
    )

    selected = member_client.put(
        "/api/v1/provider-config/selection/openrouter/",
        data=json.dumps({"account_id": shared.json()["id"]}),
        content_type="application/json",
    )
    assert selected.status_code == 204
    none = member_client.put(
        "/api/v1/provider-config/selection/openrouter/",
        data=json.dumps({"account_id": None}),
        content_type="application/json",
    )
    assert none.status_code == 204
    assert not any(row["is_active"] for row in member_client.get(ACCOUNTS).json())


@pytest.mark.django_db(transaction=True)
def test_account_update_preserves_omitted_secrets_and_normalizes_models(accounts_setup):
    org, _admin, member, _outsider = accounts_setup
    client = _client(member, org)
    created = _post(
        client,
        "openai-compatible",
        {
            "label": "Local endpoint",
            "base_url": "https://local.example/v1/",
            "api_key": "sk-initial-secret",
            "models": [" a ", "a", "b"],
        },
    )
    assert created.status_code == 201, created.content[:500]
    account_id = created.json()["id"]
    assert created.json()["base_url"] == "https://local.example/v1"
    assert created.json()["models"] == ["a", "b"]
    assert created.json()["api_key_hint"] == "••••cret"
    assert "sk-initial-secret" not in created.content.decode()

    omitted_models = _put(
        client,
        "openai-compatible",
        account_id,
        {"base_url": "https://local.example/v1", "label": "Renamed"},
    )
    assert omitted_models.status_code == 200, omitted_models.content[:500]
    assert omitted_models.json()["models"] == ["a", "b"]
    assert omitted_models.json()["api_key_hint"] == "••••cret"

    cleared_models = _put(
        client,
        "openai-compatible",
        account_id,
        {"base_url": "https://local.example/v1", "models": [], "api_key": ""},
    )
    assert cleared_models.status_code == 200
    assert cleared_models.json()["models"] == []
    assert cleared_models.json()["api_key_hint"] == "••••cret"
    credentials = ProviderAccountService().get_credentials(
        ProviderAccountService().get_account(org.id, member.id, account_id)
    )
    assert credentials["api_key"] == "sk-initial-secret"

    invalid_models = _put(
        client,
        "openai-compatible",
        account_id,
        {"models": ["valid", "  "]},
    )
    assert invalid_models.status_code == 400


@pytest.mark.django_db(transaction=True)
def test_account_mutation_rejects_foreign_account_and_provider_mismatch(accounts_setup):
    org, admin, member, _outsider = accounts_setup
    member_client = _client(member, org)
    admin_client = _client(admin, org)
    personal = _post(
        member_client,
        "openrouter",
        {"label": "Personal", "api_key": "private-key"},
    )
    assert personal.status_code == 201
    account_id = personal.json()["id"]

    assert (
        _put(admin_client, "openrouter", account_id, {"label": "not mine"}).status_code
        == 404
    )
    assert (
        _put(
            member_client, "chatgpt", account_id, {"label": "wrong provider"}
        ).status_code
        == 404
    )
    deleted = member_client.delete(f"{ACCOUNTS}openrouter/{account_id}/")
    assert deleted.status_code == 204
    assert member_client.get(ACCOUNTS).json() == []


@pytest.mark.django_db(transaction=True)
def test_account_validation_and_bedrock_auth_credentials(accounts_setup):
    org, _admin, member, _outsider = accounts_setup
    client = _client(member, org)

    for base_url in ("https://", "http:///x", "ftp://host.example/v1"):
        bad = _post(
            client,
            "openai-compatible",
            {"label": "Bad endpoint", "base_url": base_url},
        )
        assert bad.status_code == 400

    bad_bedrock = _post(
        client,
        "amazon-bedrock",
        {"label": "AWS", "auth_method": "invalid"},
    )
    assert bad_bedrock.status_code == 400
    missing_bedrock_credentials = _post(
        client,
        "amazon-bedrock",
        {"label": "AWS", "auth_method": "access_keys"},
    )
    assert missing_bedrock_credentials.status_code == 400

    access_keys = _post(
        client,
        "amazon-bedrock",
        {
            "label": "AWS keys",
            "auth_method": "access_keys",
            "region": "eu-central-1",
            "access_key_id": "AKIA123",
            "secret_access_key": "aws-secret-value",
        },
    )
    assert access_keys.status_code == 201, access_keys.content[:500]
    assert access_keys.json()["auth_method"] == "access_keys"
    assert access_keys.json()["region"] == "eu-central-1"
    assert "aws-secret-value" not in access_keys.content.decode()
    access_account = ProviderAccountService().get_account(
        org.id, member.id, access_keys.json()["id"]
    )
    assert (
        ProviderAccountService().get_credentials(access_account)["secret_access_key"]
        == "aws-secret-value"
    )

    bearer = _post(
        client,
        "amazon-bedrock",
        {
            "label": "AWS bearer",
            "auth_method": "bearer",
            "bearer_token": "bearer-secret",
        },
    )
    assert bearer.status_code == 201, bearer.content[:500]
    assert bearer.json()["auth_method"] == "bearer"
    assert bearer.json()["api_key_hint"] == "••••cret"
    bearer_account = ProviderAccountService().get_account(
        org.id, member.id, bearer.json()["id"]
    )
    assert (
        ProviderAccountService().get_credentials(bearer_account)["bearer_token"]
        == "bearer-secret"
    )


@pytest.mark.django_db(transaction=True)
def test_removed_provider_connection_routes_are_not_found(accounts_setup):
    org, _admin, member, _outsider = accounts_setup
    client = _client(member, org)
    assert client.get("/api/v1/provider-config/providers/").status_code == 404
    url = "/api/v1/provider-config/providers/openrouter/"
    assert (
        client.put(url, data="{}", content_type="application/json").status_code == 404
    )
    assert client.delete(url).status_code == 404
    claude_url = "/api/v1/harness/engines/claude/connection/"
    assert client.get(claude_url).status_code == 404
    assert (
        client.put(claude_url, data="{}", content_type="application/json").status_code
        == 404
    )
    assert client.delete(claude_url).status_code == 404


@pytest.mark.django_db
@pytest.mark.parametrize(
    "method,path", [("get", ACCOUNTS), ("post", f"{ACCOUNTS}openrouter/")]
)
def test_account_api_requires_provider_permission(accounts_setup, method, path):
    org, _admin, member, outsider = accounts_setup
    denied = _client(member, org, [APIKeyPermission.HARNESS_READ.value])
    kwargs = (
        {"data": "{}", "content_type": "application/json"} if method == "post" else {}
    )
    assert getattr(denied, method)(path, **kwargs).status_code == 403
    assert getattr(_client(outsider, org), method)(path, **kwargs).status_code == 404
