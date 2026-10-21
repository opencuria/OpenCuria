"""REST contracts for harness engine availability and model catalogs."""

from __future__ import annotations

import json
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.models import APIKey, APIKeyPermission
from apps.organizations.models import Membership, Organization
from common.utils import generate_api_token, hash_token


@pytest.fixture
def engine_api_setup(db):
    """Create a member with separate catalog and account-management API keys."""
    organization = Organization.objects.create(
        name="Engine API", slug=f"engine-api-{uuid.uuid4().hex[:10]}"
    )
    owner = get_user_model().objects.create_user(
        email=f"engine-api-{uuid.uuid4().hex[:8]}@example.com", password="secret"
    )
    Membership.objects.create(user=owner, organization=organization)

    def client(permission: APIKeyPermission) -> Client:
        token = generate_api_token()
        APIKey.objects.create(
            user=owner,
            name="engine test",
            key_hash=hash_token(token),
            key_prefix=token[:12],
            permissions=[permission.value],
        )
        return Client(HTTP_X_API_KEY=token, HTTP_X_ORGANIZATION_ID=str(organization.id))

    return client(APIKeyPermission.HARNESS_READ), client(
        APIKeyPermission.HARNESS_PROVIDERS
    )


@pytest.mark.django_db(transaction=True)
def test_engine_availability_follows_provider_account_selection(
    engine_api_setup,
) -> None:
    read, accounts = engine_api_setup
    endpoint = "/api/v1/harness/engines/"
    engines = read.get(endpoint)
    assert engines.status_code == 200, engines.content
    assert engines.json() == [
        {
            "id": "native",
            "name": "OpenCuria",
            "modes": ["build", "plan"],
            "connected": True,
            "default_model": None,
            "default_effort": None,
        },
        {
            "id": "claude",
            "name": "Claude Agent",
            "modes": ["build", "plan"],
            "connected": False,
            "default_model": "sonnet",
            "default_effort": "high",
        },
        {
            "id": "codex",
            "name": "Codex",
            "modes": ["build", "plan"],
            "connected": False,
            "default_model": "chatgpt/gpt-6.1-sol",
            "default_effort": "low",
        },
    ]
    token = "safe-claude-api-token"
    saved = accounts.post(
        "/api/v1/provider-config/accounts/claude-agent/",
        data=json.dumps({"auth_type": "api_token", "token": token}),
        content_type="application/json",
    )
    assert saved.status_code == 201, saved.content
    assert saved.json()["connected"] is True
    assert token not in saved.content.decode()
    listing = read.get(endpoint)
    assert listing.json()[1]["connected"] is True
    assert token not in listing.content.decode()

    cleared = accounts.put(
        "/api/v1/provider-config/selection/claude-agent/",
        data=json.dumps({"account_id": None}),
        content_type="application/json",
    )
    assert cleared.status_code == 204, cleared.content
    assert read.get(endpoint).json()[1]["connected"] is False
    deleted = accounts.delete(
        f"/api/v1/provider-config/accounts/claude-agent/{saved.json()['id']}/"
    )
    assert deleted.status_code == 204, deleted.content

    models = read.get("/api/v1/harness/engines/claude/models/")
    assert models.status_code == 200
    assert {item["id"] for item in models.json()} == {"sonnet", "opus", "haiku"}
    assert all(item["provider"] == "claude" for item in models.json())


@pytest.mark.django_db
@pytest.mark.parametrize(
    "path", ["", "native/models/", "claude/models/", "codex/models/"]
)
def test_engine_catalog_permissions_and_organization_scope(
    engine_api_setup, path
) -> None:
    read, providers = engine_api_setup
    endpoint = f"/api/v1/harness/engines/{path}"
    assert providers.get(endpoint).status_code == 403
    assert Client().get(endpoint).status_code == 401
    assert read.get(endpoint, HTTP_X_ORGANIZATION_ID="").status_code == 401
    assert read.get(endpoint, HTTP_X_ORGANIZATION_ID="not-a-uuid").status_code == 401
    assert (
        read.get(endpoint, HTTP_X_ORGANIZATION_ID=str(uuid.uuid4())).status_code == 404
    )
