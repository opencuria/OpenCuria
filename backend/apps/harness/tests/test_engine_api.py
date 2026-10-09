"""REST contracts for harness engines and personal Claude connections."""

from __future__ import annotations

import json
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.models import APIKey, APIKeyPermission
from apps.credentials.models import Credential, CredentialService
from apps.credentials.repositories import OrgCredentialServiceActivationRepository
from apps.harness.engines.connections import EngineConnectionService
from apps.organizations.models import Membership, MembershipRole, Organization
from common.utils import generate_api_token, hash_token


@pytest.fixture
def engine_api_setup(db):
    """Create an organization, two members, API keys and active Claude services."""
    organization = Organization.objects.create(
        name=f"Claude API {uuid.uuid4().hex[:8]}",
        slug=f"claude-api-{uuid.uuid4().hex[:10]}",
    )
    owner = get_user_model().objects.create_user(
        email=f"claude-api-{uuid.uuid4().hex[:8]}@example.com",
        password="secret",
    )
    other = get_user_model().objects.create_user(
        email=f"claude-other-{uuid.uuid4().hex[:8]}@example.com",
        password="secret",
    )
    Membership.objects.create(
        user=owner, organization=organization, role=MembershipRole.MEMBER
    )
    Membership.objects.create(
        user=other, organization=organization, role=MembershipRole.MEMBER
    )
    service_rows = [
        CredentialService.objects.get_or_create(
            slug="claude-agent-api-token",
            organization=None,
            defaults={
                "name": "Claude Agent API Token",
                "credential_type": "env",
                "env_var_name": "ANTHROPIC_API_KEY",
                "label": "Claude API Token",
            },
        )[0],
        CredentialService.objects.get_or_create(
            slug="claude-agent-subscription-token",
            organization=None,
            defaults={
                "name": "Claude Subscription Token",
                "credential_type": "env",
                "env_var_name": "CLAUDE_CODE_OAUTH_TOKEN",
                "label": "Claude Subscription Token",
            },
        )[0],
    ]
    OrgCredentialServiceActivationRepository.ensure_activated(
        organization.id, [service.id for service in service_rows]
    )

    def make_client(user, permission: APIKeyPermission) -> Client:
        token = generate_api_token()
        APIKey.objects.create(
            user=user,
            name="engine test",
            key_hash=hash_token(token),
            key_prefix=token[:12],
            permissions=[permission.value],
        )
        return Client(
            HTTP_X_API_KEY=token,
            HTTP_X_ORGANIZATION_ID=str(organization.id),
        )

    return {
        "organization": organization,
        "owner": owner,
        "other": other,
        "read_client": make_client(owner, APIKeyPermission.HARNESS_READ),
        "providers_client": make_client(owner, APIKeyPermission.HARNESS_PROVIDERS),
        "read_only_client": make_client(other, APIKeyPermission.HARNESS_READ),
        "other_providers_client": make_client(
            other, APIKeyPermission.HARNESS_PROVIDERS
        ),
    }


@pytest.mark.django_db(transaction=True)
def test_engine_api_lists_catalog_and_safe_connection(engine_api_setup) -> None:
    data = engine_api_setup
    read = data["read_client"]

    engines = read.get("/api/v1/harness/engines/")
    assert engines.status_code == 200, engines.content
    assert engines.json() == [
        {
            "id": "native",
            "name": "OpenCuria",
            "modes": ["build", "plan"],
            "connected": True,
        },
        {
            "id": "claude",
            "name": "Claude Agent",
            "modes": ["build", "plan"],
            "connected": False,
        },
    ]
    assert (
        data["providers_client"]
        .get("/api/v1/harness/engines/claude/connection/")
        .json()
        is None
    )

    models = read.get("/api/v1/harness/engines/claude/models/")
    assert models.status_code == 200
    assert {item["id"] for item in models.json()} == {"sonnet", "opus", "haiku"}
    assert all(item["provider"] == "claude" for item in models.json())

    token = "safe-claude-api-token"
    saved = data["providers_client"].put(
        "/api/v1/harness/engines/claude/connection/",
        data=json.dumps(
            {"auth_type": "api_token", "token": token, "label": "My Claude"}
        ),
        content_type="application/json",
    )
    assert saved.status_code == 200, saved.content
    payload = saved.json()
    assert payload["auth_type"] == "api_token"
    assert payload["label"] == "My Claude"
    assert payload["connected"] is True
    assert token not in saved.content.decode()

    listing = read.get("/api/v1/harness/engines/")
    assert listing.json()[1]["connected"] is True
    connection = data["providers_client"].get(
        "/api/v1/harness/engines/claude/connection/"
    )
    assert connection.json() == {
        "id": payload["id"],
        "auth_type": "api_token",
        "label": "My Claude",
        "connected": True,
    }
    assert token not in connection.content.decode()

    credential = Credential.objects.get(
        user=data["owner"], service__slug="claude-agent-api-token"
    )
    assert (
        EngineConnectionService()
        .resolve(data["organization"].id, data["owner"].id)
        .token
        == token
    )
    deleted = data["providers_client"].delete(
        "/api/v1/harness/engines/claude/connection/"
    )
    assert deleted.status_code == 204, deleted.content
    assert not Credential.objects.filter(id=credential.id).exists()


@pytest.mark.django_db(transaction=True)
def test_engine_api_permissions_and_ownership_are_scoped(engine_api_setup) -> None:
    data = engine_api_setup
    read = data["read_client"]
    forbidden = data["read_only_client"].put(
        "/api/v1/harness/engines/claude/connection/",
        data=json.dumps({"auth_type": "api_token", "token": "nope"}),
        content_type="application/json",
    )
    assert forbidden.status_code == 403

    saved = data["providers_client"].put(
        "/api/v1/harness/engines/claude/connection/",
        data=json.dumps({"auth_type": "subscription_token", "token": "owner-only"}),
        content_type="application/json",
    )
    assert saved.status_code == 200
    assert (
        data["other_providers_client"]
        .get("/api/v1/harness/engines/claude/connection/")
        .json()
        is None
    )
    assert (
        data["read_only_client"]
        .get("/api/v1/harness/engines/claude/connection/")
        .status_code
        == 403
    )
    assert (
        data["providers_client"]
        .get(
            "/api/v1/harness/engines/claude/connection/",
            HTTP_X_ORGANIZATION_ID=str(uuid.uuid4()),
        )
        .status_code
        == 404
    )

    bad_mode = data["providers_client"].put(
        "/api/v1/harness/engines/claude/connection/",
        data=json.dumps({"auth_type": "openrouter", "token": "secret"}),
        content_type="application/json",
    )
    assert bad_mode.status_code == 400
    bad_model_endpoint = read.get(
        "/api/v1/harness/engines/claude/models/",
        HTTP_X_ORGANIZATION_ID="not-a-uuid",
    )
    assert bad_model_endpoint.status_code == 401
