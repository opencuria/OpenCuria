"""User-facing provider-account workflows, independent of HTTP routing.

Account storage, credential policy and selection belong to ProviderAccountService.
This service composes those primitives with connection policy, catalog invalidation
and safe metadata reads. OAuth uses its own workflow to supply verified credentials.
"""

from __future__ import annotations

import uuid
from typing import Any

from apps.accounts.models import User
from common.exceptions import NotFoundError

from .model_catalog_service import clear_models_cache
from .provider_accounts import ProviderAccountService


class ProviderAccountManagementService:
    """List connected accounts and manage exact provider-bound account identities."""

    def __init__(self, accounts: ProviderAccountService | None = None) -> None:
        """Compose the account domain service without depending on an adapter."""
        self.accounts = accounts or ProviderAccountService()

    def list_connected_accounts(
        self, organization_id: uuid.UUID, user_id: int
    ) -> list[dict[str, Any]]:
        """Return safe metadata for visible accounts with a credential binding."""
        return [
            row
            for row in self.accounts.list_accounts(organization_id, user_id)
            if row["connected"]
        ]

    def create_account(
        self,
        *,
        organization_id: uuid.UUID,
        user: User,
        provider: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Create a directly configured account; ChatGPT requires device OAuth."""
        if provider == "chatgpt":
            raise ValueError("ChatGPT accounts must be connected using OAuth")
        account = self.accounts.save_account(
            organization_id=organization_id,
            user=user,
            provider=provider,
            payload=payload,
        )
        clear_models_cache(str(organization_id))
        return self._metadata(organization_id, int(user.id), provider, account.id)

    def update_account(
        self,
        *,
        organization_id: uuid.UUID,
        user: User,
        provider: str,
        account_id: uuid.UUID,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Update a manageable account only when its provider matches exactly."""
        self._require_provider(organization_id, int(user.id), provider, account_id)
        updated = self.accounts.save_account(
            organization_id=organization_id,
            user=user,
            provider=provider,
            payload=payload,
            account_id=account_id,
        )
        clear_models_cache(str(organization_id))
        return self._metadata(organization_id, int(user.id), provider, updated.id)

    def delete_account(
        self,
        organization_id: uuid.UUID,
        user_id: int,
        provider: str,
        account_id: uuid.UUID,
    ) -> None:
        """Delete the exact manageable account and invalidate its org catalog."""
        self._require_provider(organization_id, user_id, provider, account_id)
        self.accounts.delete_account(organization_id, user_id, account_id)
        clear_models_cache(str(organization_id))

    def _require_provider(
        self,
        organization_id: uuid.UUID,
        user_id: int,
        provider: str,
        account_id: uuid.UUID,
    ) -> None:
        account = self.accounts.get_account(
            organization_id, user_id, account_id, manage=True
        )
        if account.provider != provider:
            raise NotFoundError("ProviderAccount", str(account_id))

    def _metadata(
        self,
        organization_id: uuid.UUID,
        user_id: int,
        provider: str,
        account_id: uuid.UUID,
    ) -> dict[str, Any]:
        return next(
            row
            for row in self.accounts.list_accounts(organization_id, user_id, provider)
            if row["id"] == str(account_id)
        )
