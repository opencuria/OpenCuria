"""Revocation signal coverage for provider-account access changes."""

from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model

from apps.harness.models import HarnessMessage, ProviderTurn
from apps.harness.provider_accounts import ProviderAccountService
from apps.harness.repositories import HarnessSessionRepository
from apps.organizations.models import Membership, MembershipRole


@pytest.mark.django_db(transaction=True)
def test_membership_delete_revokes_only_that_members_shared_account_turn(
    harness_workspace, monkeypatch
):
    """Deleting a membership cancels only its dependent run and notifies post-commit."""
    from apps.harness import signals

    organization = harness_workspace.runner.organization
    member = harness_workspace.created_by
    admin = get_user_model().objects.create_user(
        email=f"revocation-admin-{uuid.uuid4().hex[:8]}@example.test", password="secret"
    )
    outsider = get_user_model().objects.create_user(
        email=f"revocation-outsider-{uuid.uuid4().hex[:8]}@example.test",
        password="secret",
    )
    Membership.objects.create(
        user=admin, organization=organization, role=MembershipRole.ADMIN
    )
    Membership.objects.create(
        user=outsider, organization=organization, role=MembershipRole.MEMBER
    )
    membership = Membership.objects.get(user=member, organization=organization)
    account = ProviderAccountService().save_account(
        organization_id=organization.id,
        user=admin,
        provider="openrouter",
        payload={"shared": True, "label": "Shared", "api_key": "key"},
    )
    session = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=organization.id,
        created_by_id=member.id,
    )
    member_turn = ProviderTurn.objects.create(
        assistant_message=HarnessMessage.objects.create(
            session=session, role="assistant", position=0
        ),
        organization_id=organization.id,
        initiated_by=member,
        account_ids={"openrouter": str(account.id)},
        holder_tokens=[str(uuid.uuid4())],
        holds=1,
    )
    outsider_turn = ProviderTurn.objects.create(
        assistant_message=HarnessMessage.objects.create(
            session=session, role="assistant", position=1
        ),
        organization_id=organization.id,
        initiated_by=outsider,
        account_ids={"openrouter": str(account.id)},
        holder_tokens=[str(uuid.uuid4())],
        holds=1,
    )
    notifications = []
    monkeypatch.setattr(signals, "_notify", notifications.append)

    membership.delete()

    member_turn.refresh_from_db()
    outsider_turn.refresh_from_db()
    assert member_turn.cancel_requested is True
    assert member_turn.revoked_account_id == account.id
    assert outsider_turn.cancel_requested is False
    assert notifications == [account.id]


@pytest.mark.django_db(transaction=True)
def test_membership_delete_revokes_turns_using_personal_accounts(
    harness_workspace, monkeypatch
):
    """A member losing org access cannot keep a run on their personal account."""
    from apps.harness import signals

    organization = harness_workspace.runner.organization
    member = harness_workspace.created_by
    account = ProviderAccountService().save_account(
        organization_id=organization.id,
        user=member,
        provider="openrouter",
        payload={"label": "Personal", "api_key": "key"},
    )
    session = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=organization.id,
        created_by_id=member.id,
    )
    turn = ProviderTurn.objects.create(
        assistant_message=HarnessMessage.objects.create(
            session=session, role="assistant", position=0
        ),
        organization_id=organization.id,
        initiated_by=member,
        account_ids={"openrouter": str(account.id)},
        holder_tokens=[str(uuid.uuid4())],
        holds=1,
    )
    notifications = []
    monkeypatch.setattr(signals, "_notify", notifications.append)

    Membership.objects.get(user=member, organization=organization).delete()

    turn.refresh_from_db()
    assert turn.cancel_requested is True
    assert turn.revoked_account_id == account.id
    assert notifications == [account.id]


@pytest.mark.django_db(transaction=True)
def test_admission_rejects_user_without_active_membership(harness_workspace):
    """A stale request cannot enter the turn after org membership is deleted."""
    from apps.harness.provider_turns import ProviderTurnService

    member = harness_workspace.created_by
    organization = harness_workspace.runner.organization
    session = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=organization.id,
        created_by_id=member.id,
    )
    assistant = HarnessMessage.objects.create(
        session=session, role="assistant", position=0
    )
    Membership.objects.filter(user=member, organization=organization).delete()

    with pytest.raises(ValueError, match="active organization membership"):
        ProviderTurnService().admit(
            assistant_message_id=assistant.id,
            organization_id=organization.id,
            user_id=member.id,
            account_ids={},
        )


def test_user_delete_uses_fence_and_revokes_before_account_cascade(
    organization, monkeypatch
):
    """Owner deletion acquires the common fence before cascading provider rows."""
    from apps.harness import signals
    from apps.harness.provider_turn_repository import ProviderTurnRepository

    user = get_user_model().objects.create_user(
        email=f"owner-revocation-{uuid.uuid4().hex[:8]}@example.test", password="secret"
    )
    Membership.objects.create(
        user=user, organization=organization, role=MembershipRole.MEMBER
    )
    account = ProviderAccountService().save_account(
        organization_id=organization.id,
        user=user,
        provider="openrouter",
        payload={"label": "Personal", "api_key": "key"},
    )
    calls = []
    real_lock = ProviderTurnRepository.lock_mutex

    def lock_spy():
        calls.append("fence")
        real_lock()

    monkeypatch.setattr(ProviderTurnRepository, "lock_mutex", lock_spy)
    monkeypatch.setattr(
        ProviderTurnRepository,
        "revoke_account",
        lambda account_id, *, allowed_user_id=None: calls.append(
            ("revoke", account_id, allowed_user_id)
        ),
    )
    monkeypatch.setattr(
        signals, "_notify", lambda account_id: calls.append(("notify", account_id))
    )
    monkeypatch.setattr(
        signals.transaction,
        "on_commit",
        lambda callback, using=None: callback(),
    )

    user.delete()

    assert not account.__class__.objects.filter(id=account.id).exists()
    assert calls[0] == "fence"
    assert ("revoke", account.id, None) in calls
    assert ("notify", account.id) in calls
