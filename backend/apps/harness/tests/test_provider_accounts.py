"""Tests for scoped provider accounts and account selection."""

from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model

from apps.credentials.services import CredentialSvc
from apps.harness.models import ProviderConnection, ProviderSelection
from apps.harness.provider_accounts import ProviderAccountService
from apps.organizations.models import Membership, MembershipRole
from apps.organizations.repositories import MembershipRepository
from common.exceptions import AuthenticationError, NotFoundError


@pytest.fixture
def account_members(organization):
    """Create an admin, two members, and provider-account service."""
    users = get_user_model()
    admin = users.objects.create_user(
        email=f"account-admin-{uuid.uuid4().hex[:8]}@example.com", password="secret"
    )
    owner = users.objects.create_user(
        email=f"account-owner-{uuid.uuid4().hex[:8]}@example.com", password="secret"
    )
    other = users.objects.create_user(
        email=f"account-other-{uuid.uuid4().hex[:8]}@example.com", password="secret"
    )
    Membership.objects.create(
        user=admin, organization=organization, role=MembershipRole.ADMIN
    )
    Membership.objects.create(
        user=owner, organization=organization, role=MembershipRole.MEMBER
    )
    Membership.objects.create(
        user=other, organization=organization, role=MembershipRole.MEMBER
    )
    return organization, admin, owner, other, ProviderAccountService()


@pytest.mark.django_db
def test_personal_accounts_are_user_and_org_scoped_and_secretless_in_view(
    account_members,
):
    org, _admin, owner, other, service = account_members
    account = service.save_account(
        organization_id=org.id,
        user=owner,
        provider="openrouter",
        payload={"label": "Personal OpenRouter", "api_key": "sk-do-not-return"},
    )

    rows = service.list_accounts(org.id, owner.id, provider="openrouter")
    assert rows[0]["id"] == str(account.id)
    assert rows[0]["scope"] == "personal"
    assert rows[0]["connected"] is True
    assert rows[0]["api_key_hint"] == "••••turn"
    assert "sk-do-not-return" not in repr(rows)
    assert service.list_accounts(org.id, other.id, provider="openrouter") == []
    assert CredentialSvc().list_credentials(owner, org.id) == []

    with pytest.raises(NotFoundError):
        service.get_account(org.id, other.id, account.id)
    with pytest.raises(NotFoundError):
        service.get_account(org.id, other.id, account.id, manage=True)


@pytest.mark.django_db
def test_short_secrets_are_not_revealed_by_account_hint(account_members):
    """Masked metadata must not disclose keys shorter than the suffix width."""
    org, _admin, owner, _other, service = account_members
    service.save_account(
        organization_id=org.id,
        user=owner,
        provider="openrouter",
        payload={"label": "Short key", "api_key": "xYz"},
    )

    rows = service.list_accounts(org.id, owner.id, provider="openrouter")

    assert rows[0]["api_key_hint"] == "••••"
    assert "xYz" not in repr(rows)


def test_shared_account_requires_admin_and_is_visible_to_members(account_members):
    org, admin, member, _other, service = account_members
    with pytest.raises(AuthenticationError):
        service.save_account(
            organization_id=org.id,
            user=member,
            provider="openrouter",
            payload={"shared": True, "label": "Org account", "api_key": "sk-test"},
        )

    account = service.save_account(
        organization_id=org.id,
        user=admin,
        provider="amazon-bedrock",
        payload={
            "shared": True,
            "label": "Production Bedrock",
            "access_key_id": "AKIA",
            "secret_access_key": "bedrock-secret",
            "region": "us-east-1",
        },
    )
    visible = service.list_accounts(org.id, member.id, provider="amazon-bedrock")
    assert len(visible) == 1
    assert visible[0]["id"] == str(account.id)
    assert visible[0]["scope"] == "organization"
    assert visible[0]["can_manage"] is False
    assert "bedrock-secret" not in repr(visible)
    with pytest.raises(AuthenticationError):
        service.delete_account(org.id, member.id, account.id)


@pytest.mark.django_db
def test_provider_selection_initializes_once_and_explicit_none_does_not_fallback(
    account_members,
):
    org, admin, member, _other, service = account_members
    first = service.save_account(
        organization_id=org.id,
        user=admin,
        provider="openai-compatible",
        payload={
            "shared": True,
            "label": "First",
            "base_url": "https://example.test/v1",
        },
    )
    assert (
        service.selected_accounts(org.id, member.id)["openai-compatible"].id == first.id
    )
    selection = ProviderSelection.objects.get(
        organization_id=org.id, user=member, provider="openai-compatible"
    )

    second = service.save_account(
        organization_id=org.id,
        user=admin,
        provider="openai-compatible",
        payload={
            "shared": True,
            "label": "Second",
            "base_url": "https://example.test/v1",
        },
    )
    assert (
        service.selected_accounts(org.id, member.id)["openai-compatible"].id == first.id
    )
    assert selection.account_id == first.id

    service.select_account(org.id, member.id, "openai-compatible", None)
    assert service.selected_accounts(org.id, member.id) == {}
    assert ProviderSelection.objects.get(id=selection.id).account_id is None

    with pytest.raises(NotFoundError):
        service.select_account(org.id, member.id, "openrouter", second.id)


@pytest.mark.django_db
def test_unshare_transfers_private_owner_and_keeps_admin_only_access(account_members):
    org, admin, member, _other, service = account_members
    shared = service.save_account(
        organization_id=org.id,
        user=admin,
        provider="openrouter",
        payload={"shared": True, "label": "Shared key", "api_key": "shared-secret"},
    )
    service.select_account(org.id, member.id, "openrouter", shared.id)

    private = service.save_account(
        organization_id=org.id,
        user=admin,
        provider="openrouter",
        payload={"shared": False},
        account_id=shared.id,
    )
    assert private.id == shared.id
    assert private.user_id == admin.id
    assert private.credential.user_id == admin.id
    assert private.credential.organization_id is None
    assert service.list_accounts(org.id, member.id, "openrouter") == []
    assert service.selected_accounts(org.id, member.id) == {}


@pytest.mark.django_db
def test_delete_soft_tombstones_account_and_clears_selection(account_members):
    org, admin, member, _other, service = account_members
    account = service.save_account(
        organization_id=org.id,
        user=admin,
        provider="claude-agent",
        payload={
            "shared": True,
            "label": "Claude API",
            "auth_type": "api_token",
            "token": "claude-secret",
        },
    )
    service.select_account(org.id, member.id, "claude-agent", account.id)
    credential_id = account.credential_id

    service.delete_account(org.id, admin.id, account.id)
    account.refresh_from_db()
    assert account.deleted_at is not None
    assert account.credential_id is None
    assert not account.__class__.objects.filter(
        id=account.id, deleted_at__isnull=True
    ).exists()
    assert (
        ProviderSelection.objects.get(
            organization_id=org.id, user=member, provider="claude-agent"
        ).account_id
        is None
    )
    assert not account.credential_id
    assert CredentialSvc().list_credentials(admin, org.id) == []

    with pytest.raises(NotFoundError):
        service.get_account(org.id, admin.id, account.id)
    assert credential_id is not None


@pytest.mark.django_db
def test_each_member_selects_only_visible_personal_accounts(account_members):
    org, _admin, owner, other, service = account_members
    first = service.save_account(
        organization_id=org.id,
        user=owner,
        provider="openai-compatible",
        payload={"label": "Personal A", "base_url": "https://a.example/v1"},
    )
    second = service.save_account(
        organization_id=org.id,
        user=owner,
        provider="openai-compatible",
        payload={"label": "Personal B", "base_url": "https://b.example/v1"},
    )
    assert (
        service.selected_accounts(org.id, owner.id)["openai-compatible"].id == first.id
    )
    service.select_account(org.id, owner.id, "openai-compatible", second.id)
    assert (
        service.selected_accounts(org.id, owner.id)["openai-compatible"].id == second.id
    )
    assert service.selected_accounts(org.id, other.id) == {}


@pytest.mark.django_db
def test_personal_account_owner_delete_revokes_access(account_members):
    """Deleting a user cascades personal accounts and internal credentials."""
    org, _admin, owner, _other, service = account_members
    account = service.save_account(
        organization_id=org.id,
        user=owner,
        provider="openrouter",
        payload={"label": "Personal", "api_key": "delete-me"},
    )
    credential_id = account.credential_id
    owner.delete()
    assert not account.__class__.objects.filter(id=account.id).exists()
    from apps.credentials.models import Credential

    assert not Credential.objects.filter(id=credential_id).exists()


@pytest.mark.django_db
def test_secret_binding_rejects_wrong_account_scope_and_generic_credential_mutation(
    account_members,
):
    """Internal secrets are never generically readable, writable, or injectable."""
    org, _admin, owner, _other, service = account_members
    account = service.save_account(
        organization_id=org.id,
        user=owner,
        provider="openrouter",
        payload={"label": "Bound", "api_key": "bound-secret"},
    )
    secret = account.credential
    from common.exceptions import NotFoundError

    with pytest.raises(NotFoundError):
        CredentialSvc().get_public_key(secret.id, org_id=org.id, user=owner)
    with pytest.raises(NotFoundError):
        CredentialSvc().update_credential(
            credential_id=secret.id,
            org_id=org.id,
            user=owner,
            is_admin=False,
            name="attempted generic mutation",
        )
    with pytest.raises(NotFoundError):
        CredentialSvc().resolve_credentials([secret.id], org_id=org.id, user=owner)
    forged = ProviderConnection.objects.create(
        organization=org,
        user=owner,
        provider="chatgpt",
        label="Forged binding",
        credential=secret,
        config={},
    )
    with pytest.raises(NotFoundError):
        service.get_credentials(forged)


@pytest.mark.django_db(transaction=True)
def test_first_selection_initialization_cannot_overwrite_explicit_none(account_members):
    """The absent-row initializer uses get_or_create and preserves explicit null."""
    org, admin, member, _other, service = account_members
    account = service.save_account(
        organization_id=org.id,
        user=admin,
        provider="openrouter",
        payload={"shared": True, "label": "Shared", "api_key": "key"},
    )
    selection = ProviderSelection.objects.create(
        organization=org, user=member, provider="openrouter", account=None
    )
    winner = service.repository.initialize_selection(
        org.id, member.id, "openrouter", account.id
    )
    selection.refresh_from_db()
    assert winner.id == selection.id
    assert selection.account_id is None
    assert service.selected_accounts(org.id, member.id) == {}


@pytest.mark.django_db(transaction=True)
def test_scope_unshare_revoke_notify_is_post_commit_and_revokes_before_secret_move(
    account_members, monkeypatch
):
    """Unsharing revokes under the fence and reliably dispatches the local wakeup."""
    org, admin, member, _other, service = account_members
    account = service.save_account(
        organization_id=org.id,
        user=admin,
        provider="openrouter",
        payload={"shared": True, "label": "Shared", "api_key": "key"},
    )
    calls = []
    from apps.harness.provider_turn_repository import ProviderTurnRepository

    monkeypatch.setattr(
        ProviderTurnRepository,
        "revoke_account",
        lambda account_id, *, allowed_user_id=None: calls.append(
            ("revoke", account_id, allowed_user_id)
        ),
    )
    monkeypatch.setattr(
        service,
        "_notify_revoked",
        lambda account_id: calls.append(("notify", account_id)),
    )
    service.save_account(
        organization_id=org.id,
        user=admin,
        provider="openrouter",
        account_id=account.id,
        payload={"shared": False},
    )
    assert calls == [
        ("revoke", account.id, admin.id),
        ("notify", account.id),
    ]
    assert service.list_accounts(org.id, member.id, "openrouter") == []


@pytest.mark.django_db(transaction=True)
def test_account_save_locks_current_membership(account_members, monkeypatch):
    """Mutations re-read and lock the active membership inside the transaction."""
    org, _admin, owner, _other, service = account_members
    real = MembershipRepository.get_for_user_and_org
    lock_flags = []

    def spy(user_id, organization_id, *, lock=False):
        lock_flags.append(lock)
        return real(user_id, organization_id, lock=lock)

    monkeypatch.setattr(MembershipRepository, "get_for_user_and_org", spy)
    service.save_account(
        organization_id=org.id,
        user=owner,
        provider="openrouter",
        payload={"label": "Member account", "api_key": "secret"},
    )
    assert lock_flags and all(lock_flags)


@pytest.mark.django_db
def test_catalog_tracks_selected_account_revision_and_never_caches_private_visibility(
    account_members,
):
    """Same provider/model namespace uses only the selected account's catalog."""
    from apps.harness.services import ProviderConfigService

    org, _admin, owner, other, service = account_members
    a = service.save_account(
        organization_id=org.id,
        user=owner,
        provider="openai-compatible",
        payload={"label": "A", "base_url": "https://a.example/v1", "models": ["a"]},
    )
    b = service.save_account(
        organization_id=org.id,
        user=owner,
        provider="openai-compatible",
        payload={"label": "B", "base_url": "https://b.example/v1", "models": ["b"]},
    )
    config = ProviderConfigService()
    assert [m.id for m in config.list_models(org.id, user_id=owner.id)] == [
        "openai-compatible/a"
    ]
    service.select_account(org.id, owner.id, "openai-compatible", b.id)
    assert [m.id for m in config.list_models(org.id, user_id=owner.id)] == [
        "openai-compatible/b"
    ]
    service.save_account(
        organization_id=org.id,
        user=owner,
        provider="openai-compatible",
        account_id=b.id,
        payload={"models": ["b2"]},
    )
    assert [m.id for m in config.list_models(org.id, user_id=owner.id)] == [
        "openai-compatible/b2"
    ]
    from common.exceptions import NotFoundError

    with pytest.raises(NotFoundError):
        config.list_models(
            org.id, user_id=other.id, account_ids={"openai-compatible": str(a.id)}
        )
    service.select_account(org.id, owner.id, "openai-compatible", None)
    with pytest.raises(NotFoundError):
        config.list_models(org.id, user_id=owner.id)


@pytest.mark.django_db
def test_bedrock_auth_switch_removes_stale_bearer_credentials(account_members):
    """Switching auth modes must not silently keep sending the old bearer."""
    org, _admin, owner, _other, service = account_members
    account = service.save_account(
        organization_id=org.id,
        user=owner,
        provider="amazon-bedrock",
        payload={"label": "AWS", "auth_method": "bearer", "bearer_token": "old-bearer"},
    )
    account = service.save_account(
        organization_id=org.id,
        user=owner,
        provider="amazon-bedrock",
        account_id=account.id,
        payload={
            "auth_method": "access_keys",
            "access_key_id": "aws-id",
            "secret_access_key": "aws-secret",
        },
    )
    assert "bearer_token" not in service.get_credentials(account)
    assert service.get_credentials(account)["access_key_id"] == "aws-id"


@pytest.mark.django_db
@pytest.mark.parametrize("revocation", ["delete", "unshare"])
def test_selection_distinguishes_unset_explicit_none_and_revocation(
    account_members, revocation
):
    """Revocation stays durable and distinct from a member's empty choice."""
    org, admin, member, _other, service = account_members
    assert not ProviderSelection.objects.filter(
        organization=org, user=member, provider="openrouter"
    ).exists()
    account = service.save_account(
        organization_id=org.id,
        user=admin,
        provider="openrouter",
        payload={"shared": True, "label": "Shared", "api_key": "shared-key"},
    )
    service.select_account(org.id, member.id, "openrouter", None)
    selection = ProviderSelection.objects.get(
        organization=org, user=member, provider="openrouter"
    )
    assert selection.account_id is None
    assert selection.is_revoked is False
    service.select_account(org.id, member.id, "openrouter", account.id)
    if revocation == "delete":
        service.delete_account(org.id, admin.id, account.id)
    else:
        service.save_account(
            organization_id=org.id,
            user=admin,
            provider="openrouter",
            account_id=account.id,
            payload={"shared": False},
        )
    selection.refresh_from_db()
    assert selection.account_id is None
    assert selection.is_revoked is True

    alternative = service.save_account(
        organization_id=org.id,
        user=member,
        provider="openrouter",
        payload={"label": "Alternative", "api_key": "alternative-key"},
    )
    assert service.selected_accounts(org.id, member.id) == {}
    selection.refresh_from_db()
    assert selection.is_revoked is True
    service.select_account(org.id, member.id, "openrouter", None)
    selection.refresh_from_db()
    assert selection.account_id is None
    assert selection.is_revoked is False
    service.select_account(org.id, member.id, "openrouter", alternative.id)
    selection.refresh_from_db()
    assert selection.account_id == alternative.id
    assert selection.is_revoked is False
