"""ORM access for scoped provider accounts and their selections."""

from __future__ import annotations

import uuid
from typing import Any

from django.db import IntegrityError
from django.db.models import Q
from django.utils import timezone

from .models import ProviderConnection, ProviderSelection


class ProviderAccountRepository:
    """Persistence operations used only by ``ProviderAccountService``."""

    @staticmethod
    def get_by_id(
        account_id: uuid.UUID, *, lock: bool = False
    ) -> ProviderConnection | None:
        """Fetch a live account by durable ID for internal refresh persistence."""
        query = ProviderConnection.objects.filter(
            id=account_id, deleted_at__isnull=True
        ).select_related("credential", "credential__service")
        if lock:
            query = query.select_for_update(of=("self",))
        return query.first()

    @staticmethod
    def list_visible(
        organization_id: uuid.UUID,
        user_id: int,
        provider: str | None = None,
    ) -> list[ProviderConnection]:
        """List organization accounts and the requesting user's own accounts."""
        query = ProviderConnection.objects.filter(
            organization_id=organization_id,
            deleted_at__isnull=True,
        ).filter(Q(user_id__isnull=True) | Q(user_id=user_id))
        if provider is not None:
            query = query.filter(provider=provider)
        return list(query.select_related("credential", "credential__service"))

    @staticmethod
    def get_visible(
        organization_id: uuid.UUID,
        user_id: int,
        account_id: uuid.UUID,
        *,
        lock: bool = False,
    ) -> ProviderConnection | None:
        """Fetch an account visible to one organization member."""
        from django.db.models import Q

        query = (
            ProviderConnection.objects.filter(
                organization_id=organization_id,
                deleted_at__isnull=True,
            )
            .filter(Q(user_id__isnull=True) | Q(user_id=user_id))
            .select_related("credential", "credential__service")
            .filter(id=account_id)
        )
        if lock:
            query = query.select_for_update(of=("self",))
        return query.first()

    @staticmethod
    def create(
        *,
        organization_id: uuid.UUID,
        user_id: int | None,
        provider: str,
        label: str,
        credential_id: uuid.UUID | None,
        config: dict[str, Any],
    ) -> ProviderConnection:
        """Create a provider account."""
        return ProviderConnection.objects.create(
            organization_id=organization_id,
            user_id=user_id,
            provider=provider,
            label=label,
            credential_id=credential_id,
            config=config,
        )

    @staticmethod
    def update(
        account: ProviderConnection,
        *,
        label: str,
        credential_id: uuid.UUID | None,
        config: dict[str, Any],
        user_id: int | None,
    ) -> ProviderConnection:
        """Persist mutable account metadata and ownership scope."""
        account.label = label
        account.credential_id = credential_id
        account.config = config
        account.user_id = user_id
        account.save(
            update_fields=["label", "credential", "config", "user", "updated_at"]
        )
        account.refresh_from_db()
        return account

    @staticmethod
    def clear_selections(account_id: uuid.UUID) -> None:
        """Explicitly revoke current selections without deleting their rows."""
        ProviderSelection.objects.filter(account_id=account_id).update(
            account=None, is_revoked=True, updated_at=timezone.now()
        )

    @staticmethod
    def clear_selections_for_others(account_id: uuid.UUID, user_id: int) -> None:
        """Clear selections for members who lose access when account is unshared."""
        ProviderSelection.objects.filter(account_id=account_id).exclude(
            user_id=user_id
        ).update(account=None, is_revoked=True, updated_at=timezone.now())

    @staticmethod
    def selection(
        organization_id: uuid.UUID,
        user_id: int,
        provider: str,
        *,
        lock: bool = False,
    ) -> ProviderSelection | None:
        """Get a selection row; absent is distinct from explicit None."""
        query = ProviderSelection.objects.filter(
            organization_id=organization_id,
            user_id=user_id,
            provider=provider,
        )
        if lock:
            query = query.select_for_update(of=("self",))
        return query.first()

    @staticmethod
    def set_selection(
        organization_id: uuid.UUID,
        user_id: int,
        provider: str,
        account_id: uuid.UUID | None,
    ) -> None:
        """Create or update a selection, including an explicit empty choice."""
        try:
            ProviderSelection.objects.update_or_create(
                organization_id=organization_id,
                user_id=user_id,
                provider=provider,
                defaults={"account_id": account_id, "is_revoked": False},
            )
        except IntegrityError:
            # Concurrent first access can race on the unique key. Retry as update.
            ProviderSelection.objects.filter(
                organization_id=organization_id,
                user_id=user_id,
                provider=provider,
            ).update(
                account_id=account_id, is_revoked=False, updated_at=timezone.now()
            )

    @staticmethod
    def initialize_selection(
        organization_id: uuid.UUID,
        user_id: int,
        provider: str,
        account_id: uuid.UUID,
    ) -> ProviderSelection:
        """Initialize an absent selection only; explicit None is preserved."""
        row, _ = ProviderSelection.objects.get_or_create(
            organization_id=organization_id,
            user_id=user_id,
            provider=provider,
            defaults={"account_id": account_id},
        )
        return row

    @staticmethod
    def list_live_for_user(user_id: int) -> list[ProviderConnection]:
        """List personal live accounts for account-owner deletion revocation."""
        return list(
            ProviderConnection.objects.filter(
                user_id=user_id, deleted_at__isnull=True
            ).only("id")
        )

    @staticmethod
    def list_live_for_membership_deletion(
        organization_id: uuid.UUID,
    ) -> list[ProviderConnection]:
        """List live shared accounts whose access is lost by membership delete."""
        return list(
            ProviderConnection.objects.filter(
                organization_id=organization_id,
                user__isnull=True,
                deleted_at__isnull=True,
            ).only("id")
        )

    @staticmethod
    def soft_delete(account: ProviderConnection) -> None:
        """Mark the account deleted and retain its historical identity."""
        account.deleted_at = timezone.now()
        account.credential = None
        account.save(update_fields=["deleted_at", "credential", "updated_at"])
