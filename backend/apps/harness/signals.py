"""Revocation hooks for deleted provider-account owners and memberships."""

from __future__ import annotations

import uuid

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models.signals import pre_delete
from django.dispatch import receiver

from apps.organizations.models import Membership

from .provider_account_events import notify_account_revoked
from .provider_account_repository import ProviderAccountRepository
from .provider_turn_repository import ProviderTurnRepository


@receiver(pre_delete, sender=get_user_model())
def revoke_user_provider_accounts(sender, instance, using, **kwargs) -> None:
    """Fence account-owner deletion and revoke dependent turns."""
    # Fence before enumerating accounts and even when the current set is empty:
    # otherwise an account/turn could be admitted after a stale empty read and
    # then be cascade-deleted without revoking its in-flight snapshot.
    ProviderTurnRepository.lock_mutex()
    account_ids = [
        row.id for row in ProviderAccountRepository.list_live_for_user(instance.pk)
    ]
    for account_id in account_ids:
        ProviderTurnRepository.revoke_account(account_id)
        transaction.on_commit(lambda value=account_id: _notify(value), using=using)


@receiver(pre_delete, sender=Membership)
def revoke_shared_accounts_on_membership_delete(
    sender, instance, using, **kwargs
) -> None:
    """Cancel the member's org answers when membership access is removed."""
    # A personal account remains owned after membership deletion, but a run
    # using it is still scoped to this organization and must stop. Revoke all
    # active turns in the organization, not only turns referencing shared rows.
    # This also fences admission against the membership removal transaction.
    account_ids = ProviderTurnRepository.revoke_member_turns(
        instance.organization_id, instance.user_id
    )
    for account_id in account_ids:
        transaction.on_commit(lambda value=account_id: _notify(value), using=using)


def _notify(account_id: uuid.UUID) -> None:
    """Wake matching local monitors after the deletion transaction commits."""
    notify_account_revoked(account_id)
