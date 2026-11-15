"""Central ChatGPT OAuth refresh for native adapters and external engines.

Only this service should exchange rotating refresh tokens for persisted provider
accounts. Database leases coordinate processes without keeping a transaction open
across HTTP. Every read/save rechecks the caller's live account authority.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from asgiref.sync import sync_to_async

from ..provider_accounts import ProviderAccountService
from ..refresh_repository import ProviderRefreshRepository
from .base import ProviderAuthError, ProviderTimeoutError
from .chatgpt_oauth import OAuthTokens, refresh_tokens

# Preflight leaves ample room for an engine's nine-second refresh RPC deadline.
EXPIRY_MARGIN_SECONDS = 60.0
REFRESH_TIMEOUT_SECONDS = 5.5
LEASE_SECONDS = 7.0
WAIT_TIMEOUT_SECONDS = 8.0
POLL_SECONDS = 0.05
RECONNECT_MESSAGE = (
    "ChatGPT authorization expired. Reconnect ChatGPT in organization settings."
)


@dataclass(frozen=True)
class _Claim:
    """In-process secret snapshot, never written to lease/status records."""

    owner: uuid.UUID
    generation: int
    credentials: dict[str, Any] = field(repr=False)
    binding: tuple[Any, ...] = field(repr=False)


class ChatGPTCredentialService:
    """Coordinate refreshes for an exact account and initiating authority."""

    def __init__(
        self,
        organization_id: uuid.UUID,
        user_id: int,
        account_id: uuid.UUID | str,
    ) -> None:
        self.organization_id = organization_id
        self.user_id = user_id
        self.account_id = ProviderAccountService._parse_account_id(account_id)
        self.accounts = ProviderAccountService()
        self.repository = ProviderRefreshRepository()

    def _read(self) -> tuple[dict[str, Any], tuple[Any, ...]]:
        """Read current vault credentials only after validating live ACL/binding."""
        account = self.accounts.get_account(
            self.organization_id, self.user_id, self.account_id
        )
        if account.provider != "chatgpt" or account.credential_id is None:
            raise ProviderAuthError(RECONNECT_MESSAGE, provider="chatgpt")
        credentials = self.accounts.get_credentials(account)
        # Compare encrypted bytes, not token hashes persisted in a side channel.
        # This also detects reconnects that restore the very same plaintext.
        binding = (
            account.organization_id,
            account.user_id,
            account.credential_id,
            account.credential.encrypted_value,
            account.updated_at,
        )
        return credentials, binding

    @staticmethod
    def _fresh(credentials: dict[str, Any]) -> bool:
        """Treat missing/malformed expiry as expired, with a proactive margin."""
        try:
            expiry = float(credentials.get("expires") or 0)
        except (TypeError, ValueError, OverflowError):
            return False
        return (
            bool(credentials.get("access"))
            and expiry > (time.time() + EXPIRY_MARGIN_SECONDS) * 1000
        )

    def _prepare(
        self, rejected_access_token: str | None, force: bool
    ) -> dict[str, Any] | _Claim | None:
        """Reread and double-check tokens atomically before claiming a lease."""
        with self.repository.locked():
            credentials, binding = self._read()
            access = credentials.get("access")
            # A 401 rejects one token, not a newer worker's replacement. Reuse
            # that replacement when fresh instead of rotating it a second time.
            rejected = rejected_access_token is not None and access == (
                rejected_access_token
            )
            forced = force and rejected_access_token is None
            if self._fresh(credentials) and not rejected and not forced:
                return credentials
            if not credentials.get("refresh"):
                raise ProviderAuthError(RECONNECT_MESSAGE, provider="chatgpt")
            ownership = self.repository.claim(self.account_id, LEASE_SECONDS)
            if ownership is None:
                return None
            return _Claim(*ownership, credentials, binding)

    def _save(self, claim: _Claim, tokens: OAuthTokens) -> dict[str, Any]:
        """Fence stale owners, revalidate authority, then encrypt via the vault."""
        with self.repository.locked():
            current, binding = self._read()
            if binding != claim.binding or not self.repository.owns(
                self.account_id, claim.owner, claim.generation
            ):
                raise ProviderAuthError(RECONNECT_MESSAGE, provider="chatgpt")
            updated = dict(current)
            updated.update(
                access=tokens.access, refresh=tokens.refresh, expires=tokens.expires
            )
            if tokens.account_id:
                updated["account_id"] = tokens.account_id
            if tokens.residency:
                updated["residency"] = tokens.residency
            self.accounts.update_credentials(self.account_id, updated)
            self.repository.release(self.account_id, claim.owner, claim.generation)
            return updated

    async def get_fresh_credentials(
        self, *, rejected_access_token: str | None = None, force: bool = False
    ) -> dict[str, Any]:
        """Return live tokens or exchange once under a durable account lease.

        For a rejected token pass its exact access value (also when ``force`` is
        true). A newer token is reused. ``force`` without a rejected token takes
        a baseline snapshot so concurrent forced callers also converge.
        """
        deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
        if force and rejected_access_token is None:
            baseline = await sync_to_async(self._baseline)()
            rejected_access_token = str(baseline.get("access") or "")
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderTimeoutError(
                    "ChatGPT credential refresh is busy; retry the request.",
                    provider="chatgpt",
                )
            result = await sync_to_async(self._prepare)(rejected_access_token, force)
            if isinstance(result, dict):
                return result
            if result is None:
                await asyncio.sleep(min(POLL_SECONDS, remaining))
                continue
            try:
                # Both HTTP timeout and total coroutine timeout are bounded.
                # Never propagate an OAuth response body or request exception:
                # the upstream helper may include access/refresh tokens in it.
                try:
                    remaining = deadline - time.monotonic()
                    async with httpx.AsyncClient(
                        timeout=min(REFRESH_TIMEOUT_SECONDS, max(0.01, remaining))
                    ) as client:
                        tokens = await asyncio.wait_for(
                            refresh_tokens(
                                result.credentials["refresh"], client=client
                            ),
                            timeout=min(REFRESH_TIMEOUT_SECONDS, max(0.01, remaining)),
                        )
                    if not tokens.access or not tokens.refresh:
                        raise ValueError("Missing OAuth tokens")
                except Exception:
                    raise ProviderAuthError(
                        RECONNECT_MESSAGE, provider="chatgpt"
                    ) from None
                return await sync_to_async(self._save)(result, tokens)
            finally:
                await sync_to_async(self.repository.release)(
                    self.account_id, result.owner, result.generation
                )

    def _baseline(self) -> dict[str, Any]:
        """Read the rejected-token baseline under the same mutation fence."""
        with self.repository.locked():
            credentials, _ = self._read()
            return credentials


async def get_fresh_chatgpt_credentials(
    organization_id: uuid.UUID,
    user_id: int,
    account_id: uuid.UUID | str,
    *,
    rejected_access_token: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Get/refresh one exact account; never consult active account selection."""
    return await ChatGPTCredentialService(
        organization_id, user_id, account_id
    ).get_fresh_credentials(rejected_access_token=rejected_access_token, force=force)
