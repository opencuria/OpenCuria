"""Per-user provider-account binding for external Claude engine turns."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

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
    """Resolve Claude runtime authentication from scoped provider accounts."""

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

    def is_connected(self, organization_id: uuid.UUID, user_id: object) -> bool:
        """Return whether the caller's selected Claude account has credentials."""
        organization_id, user_id = self._ids(organization_id, user_id)
        self._assert_member(organization_id, user_id)
        account = self._active_account(organization_id, user_id)
        return account is not None and account.credential_id is not None

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
            account = self.accounts.get_account(organization_id, user_id, connection_id)
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
