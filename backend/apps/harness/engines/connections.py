"""Per-user provider-account binding for external Claude engine turns."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from apps.organizations.repositories import MembershipRepository
from common.exceptions import NotFoundError

from ..provider_accounts import ProviderAccountService

AUTH_TYPE_TO_ENV = {
    "api_token": "ANTHROPIC_API_KEY",
    "subscription_token": "CLAUDE_CODE_OAUTH_TOKEN",
}


@dataclass(frozen=True)
class AuthConfiguration:
    """Server-only auth material resolved from one provider account."""

    auth_type: str
    token: str = field(repr=False)
    label: str = ""
    connection_id: uuid.UUID | None = None

    @property
    def secret_env(self) -> dict[str, str]:
        """Return the single narrowly-scoped CLI credential variable."""
        env_name = AUTH_TYPE_TO_ENV.get(self.auth_type)
        if env_name is None:
            raise ValueError("Unsupported Claude authentication type")
        return {env_name: self.token}


class EngineConnectionService:
    """Compatibility wrapper exposing active Claude provider accounts."""

    PROVIDER = "claude-agent"

    def __init__(self, accounts: ProviderAccountService | None = None) -> None:
        self.accounts = accounts or ProviderAccountService()

    @staticmethod
    def _ids(organization_id: uuid.UUID, user_id: object) -> tuple[uuid.UUID, int]:
        return getattr(organization_id, "id", organization_id), int(
            getattr(user_id, "id", user_id)
        )

    @staticmethod
    def _normalize_auth_type(auth_type: str) -> str:
        """Validate auth type and return its canonical value."""
        value = (auth_type or "").strip().lower()
        if value not in AUTH_TYPE_TO_ENV:
            raise ValueError("auth_type must be api_token or subscription_token")
        return value

    @staticmethod
    def _assert_member(organization_id: uuid.UUID, user_id: object) -> None:
        """Require active organization membership."""
        membership = MembershipRepository.get_for_user_and_org(user_id, organization_id)
        if membership is None or not membership.user.is_active:
            raise NotFoundError("Organization", str(organization_id))

    def _active_account(self, organization_id: uuid.UUID, user_id: int):
        """Return the selected Claude account, or None when explicitly empty."""
        return self.accounts.selected_accounts(organization_id, user_id).get(
            self.PROVIDER
        )

    def list_connections(
        self, organization_id: uuid.UUID, user_id: object
    ) -> list[dict[str, Any]]:
        """List only the active account's safe metadata, if any."""
        organization_id, user_id = self._ids(organization_id, user_id)
        self._assert_member(organization_id, user_id)
        account = self._active_account(organization_id, user_id)
        if account is None:
            return []
        return [
            {
                "id": str(account.id),
                "auth_type": account.auth_type,
                "label": account.label,
                "connected": account.credential_id is not None,
                "created_at": account.created_at,
                "updated_at": account.updated_at,
            }
        ]

    def connection_view(
        self, organization_id: uuid.UUID, user_id: object
    ) -> dict[str, Any] | None:
        """Return the user's one connection without any credential material."""
        connections = self.list_connections(organization_id, user_id)
        if not connections:
            return None
        connection = connections[0]
        return {
            "id": connection["id"],
            "auth_type": connection["auth_type"],
            "label": connection["label"],
            "connected": connection["connected"],
        }

    def save_connection(
        self,
        *,
        organization_id: uuid.UUID,
        user,
        auth_type: str,
        token: str = "",
        label: str = "",
        connection_id: uuid.UUID | str | None = None,
    ):
        """Create or update the caller's active personal Claude account."""
        organization_id, user_id = self._ids(organization_id, user)
        self._assert_member(organization_id, user_id)
        normalized_type = self._normalize_auth_type(auth_type)
        if not isinstance(token, str):
            raise ValueError("token must be a string")
        if len(token) > 8192:
            raise ValueError("token exceeds the 8192 character limit")
        secret = token.strip()
        if secret and any(char.isspace() for char in secret):
            raise ValueError("token must not contain whitespace")
        if "\x00" in secret:
            raise ValueError("token must not contain control characters")
        cleaned_label = (label or "").strip()[:255] or "Claude"
        existing = self._active_account(organization_id, user_id)
        if connection_id is not None:
            try:
                requested = uuid.UUID(str(connection_id))
            except (TypeError, ValueError, AttributeError):
                raise NotFoundError("ProviderAccount", str(connection_id)) from None
            existing = self.accounts.get_account(
                organization_id, user_id, requested, manage=True
            )
            if existing.provider != self.PROVIDER or existing.user_id != user_id:
                raise NotFoundError("ProviderAccount", str(connection_id))
        if existing is not None and existing.user_id != user_id:
            # Saving a compatibility connection must never mutate a shared
            # account or an account belonging to another member.
            existing = None
        if not secret and existing is None:
            raise ValueError("A token is required to connect Claude")
        payload: dict[str, Any] = {
            "label": cleaned_label,
            "auth_type": normalized_type,
            "token": secret,
            "shared": False,
        }
        if existing is not None and not secret and existing.auth_type != normalized_type:
            raise ValueError("A token is required when changing auth_type")
        account = self.accounts.save_account(
            organization_id=organization_id,
            user=user,
            provider=self.PROVIDER,
            payload=payload,
            account_id=existing.id if existing is not None else None,
        )
        return account

    def delete_connection(
        self,
        organization_id: uuid.UUID,
        user_id: object,
        connection_id: uuid.UUID | str | None = None,
    ) -> None:
        """Delete the selected Claude account through provider-account ACLs."""
        organization_id, user_id = self._ids(organization_id, user_id)
        self._assert_member(organization_id, user_id)
        account = (
            self.accounts.get_account(
                organization_id, user_id, connection_id, manage=True
            )
            if connection_id is not None
            else self._active_account(organization_id, user_id)
        )
        if account is None or account.provider != self.PROVIDER:
            raise NotFoundError("ProviderAccount", str(connection_id or user_id))
        self.accounts.delete_account(organization_id, user_id, account.id)

    def resolve(
        self,
        organization_id: uuid.UUID,
        user_id: object,
        connection_id: uuid.UUID | str | None = None,
    ) -> AuthConfiguration:
        """Resolve one explicitly scoped or currently selected account."""
        organization_id, user_id = self._ids(organization_id, user_id)
        self._assert_member(organization_id, user_id)
        if connection_id is not None:
            account = self.accounts.get_account(
                organization_id, user_id, connection_id
            )
        else:
            account = self._active_account(organization_id, user_id)
        if account is None or account.provider != self.PROVIDER:
            raise NotFoundError("ProviderAccount", str(connection_id or user_id))
        auth_type = self._normalize_auth_type(account.auth_type)
        credentials = self.accounts.get_credentials(account)
        token = str(credentials.get("token") or "")
        if not token:
            raise NotFoundError("ProviderAccount", str(account.id))
        return AuthConfiguration(
            auth_type=auth_type,
            token=token,
            label=account.label,
            connection_id=account.id,
        )
