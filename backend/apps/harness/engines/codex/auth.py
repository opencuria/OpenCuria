"""Server-only Codex credentials resolved through the existing account ACL.

No credential material belongs in engine_state, messages or task snapshots.
Execution uses the shared database-leased ChatGPT credential service before
login and for app-server refresh requests. ``persist_credentials`` only updates
the captured account; it is not a refresh concurrency mechanism.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from asgiref.sync import sync_to_async

from common.exceptions import AuthenticationError, NotFoundError

from ...provider_accounts import ProviderAccountService
from .catalog import codex_provider


@dataclass(frozen=True)
class CodexAuthConfiguration:
    """Private resolved auth; safe identity fields plus repr-hidden secrets."""

    provider_id: str
    connection_id: uuid.UUID
    credentials: dict[str, Any] = field(repr=False)
    base_url: str = ""

    @property
    def secret_env(self) -> dict[str, str]:
        """OpenRouter uses an API key; ChatGPT uses ephemeral OAuth login data."""
        if self.provider_id == "openrouter":
            return {"OPENCURIA_CODEX_API_KEY": str(self.credentials["api_key"])}
        return {}

    @property
    def login_data(self) -> dict[str, str]:
        """Return material for account/login/start chatgptAuthTokens (server only)."""
        if self.provider_id != "chatgpt":
            return {}
        return {
            "type": "chatgptAuthTokens",
            "accessToken": str(self.credentials["access"]),
            "chatgptAccountId": str(self.credentials["account_id"]),
        }

    async def persist_credentials(self, credentials: dict[str, Any]) -> None:
        """Persist a refresh against the captured account, with live binding checks."""
        await sync_to_async(ProviderAccountService().update_credentials)(
            self.connection_id, credentials
        )


def resolve_codex_auth(
    organization_id: uuid.UUID,
    user_id: int,
    account_ids: dict[str, str] | None,
    model: str,
) -> CodexAuthConfiguration:
    """Resolve current selection or an explicit immutable per-run account snapshot.

    An empty snapshot is fail-closed; it must never fall back to current selection.
    Execution refreshes ChatGPT via the shared credential service before login;
    this resolver does not exchange tokens or change the selected account.
    """
    if user_id is None:
        raise AuthenticationError("A user is required for Codex authorization")
    provider = codex_provider(model)
    service = ProviderAccountService()
    if account_ids is None:
        account = service.selected_accounts(organization_id, user_id).get(provider)
    else:
        account_id = account_ids.get(provider)
        if not account_id:
            raise AuthenticationError(f"No {provider} account selected for Codex")
        account = service.get_account(organization_id, user_id, account_id)
    if account is None or account.provider != provider:
        raise NotFoundError("ProviderAccount", provider)
    credentials = service.get_credentials(account)
    required = (
        ("access", "refresh", "account_id") if provider == "chatgpt" else ("api_key",)
    )
    if any(not credentials.get(key) for key in required):
        raise AuthenticationError(f"Reconnect your {provider} account to use Codex")
    return CodexAuthConfiguration(
        provider_id=provider,
        connection_id=account.id,
        credentials=dict(credentials),
        base_url=str((account.config or {}).get("base_url") or ""),
    )
