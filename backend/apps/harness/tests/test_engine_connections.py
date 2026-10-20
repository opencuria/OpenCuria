"""Claude runtime authentication from scoped provider accounts."""

from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model

from apps.harness.engines.connections import EngineConnectionService
from apps.harness.provider_accounts import ProviderAccountService
from apps.organizations.repositories import MembershipRepository
from common.exceptions import NotFoundError


@pytest.fixture
def connection_owner(organization):
    """Create an active organization member for authentication tests."""
    user = get_user_model().objects.create_user(
        email=f"claude-{uuid.uuid4().hex[:10]}@example.com", password="secret"
    )
    MembershipRepository.create(user=user, organization=organization)
    return user


@pytest.mark.django_db
@pytest.mark.parametrize(
    "auth_type,env_name",
    [
        ("api_token", "ANTHROPIC_API_KEY"),
        ("subscription_token", "CLAUDE_CODE_OAUTH_TOKEN"),
    ],
)
def test_resolve_selected_and_explicit_account_auth(
    organization, connection_owner, auth_type, env_name
) -> None:
    accounts = ProviderAccountService()
    account = accounts.save_account(
        organization_id=organization.id,
        user=connection_owner,
        provider="claude-agent",
        payload={"auth_type": auth_type, "token": "claude-secret", "label": "Work"},
    )
    service = EngineConnectionService()
    selected = service.resolve(organization.id, connection_owner.id)
    assert selected == service.resolve(organization.id, connection_owner.id, account.id)
    assert selected.auth_type == auth_type
    assert selected.token == "claude-secret"
    assert selected.label == "Work"
    assert selected.secret_env == {env_name: "claude-secret"}
    assert selected.connection_id == account.id
    assert "claude-secret" not in repr(selected)
    assert service.is_connected(organization.id, connection_owner.id)

    accounts.select_account(organization.id, connection_owner.id, "claude-agent", None)
    assert not service.is_connected(organization.id, connection_owner.id)
    with pytest.raises(NotFoundError):
        service.resolve(organization.id, connection_owner.id)
    # An admitted turn keeps its explicit account even after selection changes.
    assert service.resolve(organization.id, connection_owner.id, account.id) == selected


@pytest.mark.django_db
def test_resolution_rejects_foreign_deleted_and_non_claude_accounts(
    organization, connection_owner
) -> None:
    accounts = ProviderAccountService()
    account = accounts.save_account(
        organization_id=organization.id,
        user=connection_owner,
        provider="claude-agent",
        payload={"token": "owner-token"},
    )
    other = get_user_model().objects.create_user(
        email=f"other-{uuid.uuid4().hex[:10]}@example.com", password="secret"
    )
    MembershipRepository.create(user=other, organization=organization)
    service = EngineConnectionService()
    assert not service.is_connected(organization.id, other.id)
    for org_id, user_id, account_id in (
        (organization.id, other.id, account.id),
        (uuid.uuid4(), connection_owner.id, account.id),
        (organization.id, other.id, None),
    ):
        with pytest.raises(NotFoundError):
            service.resolve(org_id, user_id, account_id)

    native = accounts.save_account(
        organization_id=organization.id,
        user=connection_owner,
        provider="openrouter",
        payload={"api_key": "native-secret"},
    )
    with pytest.raises(NotFoundError):
        service.resolve(organization.id, connection_owner.id, native.id)
    accounts.delete_account(organization.id, connection_owner.id, account.id)
    assert not service.is_connected(organization.id, connection_owner.id)
    with pytest.raises(NotFoundError):
        service.resolve(organization.id, connection_owner.id, account.id)


@pytest.mark.django_db
def test_resolution_accepts_selected_organization_shared_account(
    organization, connection_owner, provider_account_factory
) -> None:
    account = provider_account_factory("claude-agent", token="shared-claude-token")
    ProviderAccountService().select_account(
        organization.id, connection_owner.id, "claude-agent", account.id
    )
    service = EngineConnectionService()
    assert service.is_connected(organization.id, connection_owner.id)
    resolved = service.resolve(organization.id, connection_owner.id)
    assert resolved.connection_id == account.id
    assert resolved.token == "shared-claude-token"
