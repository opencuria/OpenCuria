"""ORM boundary for account-scoped OAuth refresh ownership."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .provider_refresh_models import ProviderRefreshLease
from .provider_turn_repository import ProviderTurnRepository


class ProviderRefreshRepository:
    """Use real SQLite write locks as well as PostgreSQL row locks.

    The existing account-mutation fence is held only for short read/claim/save
    transactions, never during network I/O. Account leases coordinate the much
    longer exchange independently; unrelated accounts can refresh concurrently.
    """

    @staticmethod
    @contextmanager
    def locked() -> Iterator[None]:
        """Serialize with account deletion, reconnect, scope and ACL changes."""
        with transaction.atomic():
            ProviderTurnRepository.lock_mutex()
            yield

    @staticmethod
    def claim(account_id: uuid.UUID, seconds: float) -> tuple[uuid.UUID, int] | None:
        """Claim an idle/stale lease inside ``locked``; return its fencing token."""
        lease, _ = ProviderRefreshLease.objects.get_or_create(account_id=account_id)
        now = timezone.now()
        if lease.owner is not None and lease.expires_at and lease.expires_at > now:
            return None
        lease.owner = uuid.uuid4()
        lease.generation += 1
        lease.expires_at = now + timedelta(seconds=seconds)
        lease.save(update_fields=["owner", "generation", "expires_at"])
        return lease.owner, lease.generation

    @staticmethod
    def owns(account_id: uuid.UUID, owner: uuid.UUID, generation: int) -> bool:
        """Check both token ownership and lease lifetime before saving secrets."""
        return ProviderRefreshLease.objects.filter(
            account_id=account_id,
            owner=owner,
            generation=generation,
            expires_at__gt=timezone.now(),
        ).exists()

    @staticmethod
    def release(account_id: uuid.UUID, owner: uuid.UUID, generation: int) -> None:
        """Release only the caller's lease, never a successor's ownership."""
        ProviderRefreshLease.objects.filter(
            account_id=account_id, owner=owner, generation=generation
        ).update(owner=None, expires_at=None)
