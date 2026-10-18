"""Tests for user-scoped Claude engine connections."""

from __future__ import annotations

import json
import uuid

import pytest
from django.contrib.auth import get_user_model

from apps.credentials.models import Credential, CredentialService
from apps.credentials.repositories import OrgCredentialServiceActivationRepository
from apps.harness.engines.connections import EngineConnectionService
from apps.organizations.models import MembershipRole
from apps.organizations.repositories import MembershipRepository
from common.exceptions import NotFoundError
from common.utils import decrypt_value


@pytest.fixture
def connection_owner(organization):
    """Create an active organization member for connection tests."""
    user = get_user_model().objects.create_user(
        email=f"claude-{uuid.uuid4().hex[:10]}@example.com",
        password="secret",
    )
    MembershipRepository.create(
        user=user, organization=organization, role=MembershipRole.MEMBER
    )
    for service in CredentialService.objects.filter(
        slug__in=(
            "claude-agent-api-token",
            "claude-agent-subscription-token",
        ),
        organization__isnull=True,
    ):
        OrgCredentialServiceActivationRepository.ensure_activated(
            organization.id, [service.id]
        )
    return user


@pytest.mark.django_db
def test_connection_uses_credential_store_and_safe_list_view(
    organization, connection_owner
) -> None:
    service = EngineConnectionService()
    saved = service.save_connection(
        organization_id=organization.id,
        user=connection_owner,
        auth_type="api_token",
        token="secret-api-token",
        label="Work Claude",
    )

    credential = Credential.objects.get(id=saved.credential_id)
    assert credential.user_id == connection_owner.id
    assert credential.organization_id is None
    assert credential.service.slug == "provider-account-claude-agent"
    assert credential.service.credential_type == "provider_secret"
    assert "secret-api-token" not in credential.encrypted_value
    assert decrypt_value(credential.encrypted_value) == '{"token": "secret-api-token"}'

    public = service.list_connections(organization.id, connection_owner.id)
    assert public == [
        {
            "id": str(saved.id),
            "auth_type": "api_token",
            "label": "Work Claude",
            "connected": True,
            "created_at": saved.created_at,
            "updated_at": saved.updated_at,
        }
    ]
    assert "secret-api-token" not in json.dumps(public, default=str)
    assert "credential_id" not in public[0]
    resolved = service.resolve(organization.id, connection_owner.id, saved.id)
    assert resolved.auth_type == "api_token"
    assert resolved.token == "secret-api-token"
    assert resolved.secret_env == {"ANTHROPIC_API_KEY": "secret-api-token"}
    assert resolved.connection_id == saved.id
    assert "secret-api-token" not in repr(resolved)


@pytest.mark.django_db
def test_blank_token_preserves_mode_and_secret_but_mode_change_requires_token(
    organization, connection_owner
) -> None:
    service = EngineConnectionService()
    saved = service.save_connection(
        organization_id=organization.id,
        user=connection_owner,
        auth_type="subscription_token",
        token="subscription-secret",
    )
    updated = service.save_connection(
        organization_id=organization.id,
        user=connection_owner,
        auth_type="subscription_token",
        token="  ",
        label="Renamed",
    )
    assert updated.id == saved.id
    assert updated.label == "Renamed"
    assert service.resolve(organization.id, connection_owner.id).token == (
        "subscription-secret"
    )

    with pytest.raises(ValueError, match="token is required when changing auth_type"):
        service.save_connection(
            organization_id=organization.id,
            user=connection_owner,
            auth_type="api_token",
            token="",
        )

    changed = service.save_connection(
        organization_id=organization.id,
        user=connection_owner,
        auth_type="api_token",
        token="new-api-secret",
    )
    assert changed.id == saved.id
    assert changed.auth_type == "api_token"
    assert service.resolve(organization.id, connection_owner.id).token == (
        "new-api-secret"
    )


@pytest.mark.django_db
def test_connection_resolution_is_owner_and_organization_scoped(
    organization, connection_owner
) -> None:
    service = EngineConnectionService()
    connection = service.save_connection(
        organization_id=organization.id,
        user=connection_owner,
        auth_type="api_token",
        token="owner-token",
    )
    other = get_user_model().objects.create_user(
        email=f"other-{uuid.uuid4().hex[:10]}@example.com",
        password="secret",
    )
    MembershipRepository.create(user=other, organization=organization)

    with pytest.raises(NotFoundError):
        service.resolve(organization.id, other.id, connection.id)
    with pytest.raises(NotFoundError):
        service.resolve(uuid.uuid4(), connection_owner.id, connection.id)
    assert service.list_connections(organization.id, other.id) == []


@pytest.mark.django_db
def test_connection_resolution_accepts_selected_organization_shared_account(
    organization, connection_owner
) -> None:
    """Shared Claude accounts are scoped by membership, not private owner id."""
    from apps.harness.provider_accounts import ProviderAccountService
    from apps.organizations.models import Membership, MembershipRole

    admin = get_user_model().objects.create_user(
        email=f"admin-{uuid.uuid4().hex[:10]}@example.com", password="secret"
    )
    Membership.objects.create(
        user=admin, organization=organization, role=MembershipRole.ADMIN
    )
    accounts = ProviderAccountService()
    shared = accounts.save_account(
        organization_id=organization.id,
        user=admin,
        provider="claude-agent",
        payload={
            "shared": True,
            "label": "Shared Claude",
            "auth_type": "api_token",
            "token": "shared-claude-token",
        },
    )
    accounts.select_account(
        organization.id, connection_owner.id, "claude-agent", shared.id
    )

    resolved = EngineConnectionService().resolve(organization.id, connection_owner.id)
    assert resolved.connection_id == shared.id
    assert resolved.token == "shared-claude-token"


@pytest.mark.django_db
def test_connection_delete_removes_personal_credential(
    organization, connection_owner
) -> None:
    service = EngineConnectionService()
    connection = service.save_connection(
        organization_id=organization.id,
        user=connection_owner,
        auth_type="api_token",
        token="delete-me",
    )
    credential_id = connection.credential_id
    service.delete_connection(organization.id, connection_owner.id, connection.id)
    assert not Credential.objects.filter(id=credential_id).exists()
    assert service.list_connections(organization.id, connection_owner.id) == []


@pytest.mark.django_db
@pytest.mark.parametrize("select_none", [False, True])
def test_compatibility_save_preserves_selection_when_updating_inactive_account(
    organization, connection_owner, select_none
) -> None:
    """Saving an exact account is not an implicit active-account selection."""
    from apps.harness.provider_accounts import ProviderAccountService

    accounts = ProviderAccountService()
    first = accounts.save_account(
        organization_id=organization.id,
        user=connection_owner,
        provider="claude-agent",
        payload={"label": "First", "token": "first-secret"},
    )
    second = accounts.save_account(
        organization_id=organization.id,
        user=connection_owner,
        provider="claude-agent",
        payload={"label": "Second", "token": "second-secret"},
    )
    if select_none:
        accounts.select_account(
            organization.id, connection_owner.id, "claude-agent", None
        )

    updated = EngineConnectionService().save_connection(
        organization_id=organization.id,
        user=connection_owner,
        connection_id=second.id,
        auth_type="api_token",
        token="updated-secret",
        label="Updated second",
    )

    assert updated.id == second.id
    assert accounts.get_credentials(updated)["token"] == "updated-secret"
    selected = accounts.selected_accounts(organization.id, connection_owner.id)
    assert {key: row.id for key, row in selected.items()} == (
        {} if select_none else {"claude-agent": first.id}
    )


@pytest.mark.django_db
def test_compatibility_create_preserves_explicit_none(
    organization, connection_owner
) -> None:
    """Connecting after opting out must not silently reactivate a provider."""
    from apps.harness.provider_accounts import ProviderAccountService

    accounts = ProviderAccountService()
    accounts.select_account(organization.id, connection_owner.id, "claude-agent", None)
    saved = EngineConnectionService().save_connection(
        organization_id=organization.id,
        user=connection_owner,
        auth_type="api_token",
        token="new-secret",
    )
    assert saved.credential_id is not None
    assert accounts.selected_accounts(organization.id, connection_owner.id) == {}


@pytest.mark.django_db
def test_compatibility_save_rejects_explicit_shared_account(
    organization, connection_owner
) -> None:
    """An exact shared ID must not silently create a different private account."""
    from apps.harness.provider_accounts import ProviderAccountService

    membership = MembershipRepository.get_for_user_and_org(
        connection_owner.id, organization.id
    )
    membership.role = MembershipRole.ADMIN
    membership.save(update_fields=["role"])
    accounts = ProviderAccountService()
    shared = accounts.save_account(
        organization_id=organization.id,
        user=connection_owner,
        provider="claude-agent",
        payload={"label": "Shared", "shared": True, "token": "shared-secret"},
    )

    with pytest.raises(NotFoundError):
        EngineConnectionService().save_connection(
            organization_id=organization.id,
            user=connection_owner,
            connection_id=shared.id,
            auth_type="api_token",
            token="must-not-create-a-private-copy",
        )

    assert accounts.get_credentials(shared)["token"] == "shared-secret"
    assert [
        row["id"]
        for row in accounts.list_accounts(organization.id, connection_owner.id)
    ] == [str(shared.id)]
