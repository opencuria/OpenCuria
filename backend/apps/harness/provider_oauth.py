"""ChatGPT device OAuth lifecycle for provider accounts."""

from __future__ import annotations

import uuid
from typing import Any

from asgiref.sync import sync_to_async

from apps.organizations.services import OrganizationService
from common.exceptions import AuthenticationError, NotFoundError

from .provider_accounts import ProviderAccountService
from .provider_oauth_models import ProviderOAuthFlow
from .provider_oauth_repository import ProviderOAuthRepository

CHATGPT_OAUTH_FLOW_EXPIRES_SECONDS = 600


class ProviderOAuthService:
    """Manage user-bound, durable ChatGPT device authorization flows."""

    def __init__(self, accounts: ProviderAccountService | None = None) -> None:
        self.accounts = accounts or ProviderAccountService()
        self.organizations = OrganizationService()

    async def start(
        self,
        *,
        organization_id: uuid.UUID,
        user: Any,
        label: str = "",
        shared: bool | None = None,
        account_id: uuid.UUID | str | None = None,
    ) -> dict[str, Any]:
        """Start an isolated flow after validating the exact target account."""
        organization_id, user_id = self.accounts._ids(organization_id, user)
        membership = await sync_to_async(self.organizations.require_active_membership)(
            user_id, organization_id
        )
        target = None
        if account_id is not None:
            target = await sync_to_async(self.accounts.get_account)(
                organization_id, user_id, account_id, manage=True
            )
            if target.provider != "chatgpt":
                raise NotFoundError("ProviderAccount", str(account_id))
        requested_shared = (
            target.user_id is None
            if shared is None and target is not None
            else bool(shared)
        )
        if requested_shared and membership.role != "admin":
            raise AuthenticationError("Only org admins can share provider accounts")
        if target is not None and requested_shared != (target.user_id is None):
            raise ValueError("shared must match the target account's scope")
        resolved_label = (label or (target.label if target else "ChatGPT")).strip()
        if not resolved_label:
            raise ValueError("label must not be empty")
        if len(resolved_label) > 255:
            raise ValueError("label must be at most 255 characters")

        from .providers.chatgpt_oauth import start_device_flow

        flow_result = await start_device_flow()
        membership = await sync_to_async(self.organizations.require_active_membership)(
            user_id, organization_id
        )
        if requested_shared and membership.role != "admin":
            raise AuthenticationError("Only org admins can share provider accounts")
        if target is not None:
            target = await sync_to_async(self.accounts.get_account)(
                organization_id, user_id, target.id, manage=True
            )
        await sync_to_async(ProviderOAuthRepository.cleanup_expired)()
        flow = await sync_to_async(ProviderOAuthRepository.create)(
            organization_id=organization_id,
            user_id=user_id,
            target_account_id=target.id if target is not None else None,
            label=resolved_label,
            shared=requested_shared,
            device_auth_id=flow_result.device_auth_id,
            user_code=flow_result.user_code,
            interval=flow_result.interval,
        )
        return {
            "flow_id": str(flow.id),
            "user_code": flow_result.user_code,
            "verification_url": flow_result.verification_url,
            "interval": flow_result.interval,
            "expires_in": CHATGPT_OAUTH_FLOW_EXPIRES_SECONDS,
        }

    async def status(
        self,
        *,
        organization_id: uuid.UUID,
        user: Any,
        flow_id: uuid.UUID | str,
    ) -> dict[str, str]:
        """Poll one flow once, never disclosing OAuth tokens to the caller."""
        organization_id, user_id = self.accounts._ids(organization_id, user)
        await sync_to_async(self.organizations.require_active_membership)(
            user_id, organization_id
        )
        flow = await sync_to_async(ProviderOAuthRepository.get_owned)(
            flow_id, organization_id, user_id
        )
        if flow is None:
            return {"status": "no_flow"}
        if flow.status == ProviderOAuthFlow.Status.CONSUMED:
            return {"status": "connected", "account_id": flow.connected_account_id}
        if flow.status == ProviderOAuthFlow.Status.CANCELLED:
            return {"status": "cancelled"}
        if flow.status in (
            ProviderOAuthFlow.Status.EXPIRED,
            ProviderOAuthFlow.Status.DENIED,
        ):
            return {"status": flow.status}

        # Revalidate target access and the sharing role before every poll.
        membership = await sync_to_async(self.organizations.require_active_membership)(
            user_id, organization_id
        )
        if flow.target_account_id is not None:
            target = await sync_to_async(self.accounts.get_account)(
                organization_id, user_id, flow.target_account_id, manage=True
            )
            if target.provider != "chatgpt":
                raise NotFoundError("ProviderAccount", str(flow.target_account_id))
        if flow.shared and membership.role != "admin":
            raise AuthenticationError("Only org admins can share provider accounts")

        claimed, state = await sync_to_async(ProviderOAuthRepository.claim)(
            flow.id, organization_id, user_id
        )
        if state == "expired":
            return {"status": "expired"}
        if state == "polling":
            return {"status": "pending"}
        if state != "claimed" or claimed is None:
            return {"status": "no_flow" if state == "no_flow" else state}

        poll_token = claimed.poll_token
        device_auth_id, user_code = await sync_to_async(
            ProviderOAuthRepository.device_codes
        )(claimed)
        from .providers.base import ProviderError
        from .providers.chatgpt_oauth import OAuthError, poll_device_flow_once

        try:
            result = await poll_device_flow_once(device_auth_id, user_code)
            if result.status == "pending":
                await sync_to_async(ProviderOAuthRepository.release_to_pending)(
                    flow.id, poll_token
                )
                return {"status": "pending"}
            if result.status == "expired":
                await sync_to_async(ProviderOAuthRepository.set_terminal)(
                    flow.id, ProviderOAuthFlow.Status.EXPIRED, poll_token
                )
                return {"status": "expired"}
            if result.status == "denied":
                await sync_to_async(ProviderOAuthRepository.set_terminal)(
                    flow.id, ProviderOAuthFlow.Status.DENIED, poll_token
                )
                return {"status": "denied"}
            if result.tokens is None:
                await sync_to_async(ProviderOAuthRepository.release_to_pending)(
                    flow.id, poll_token
                )
                raise ProviderError("OAuth completed without tokens")

            credentials = {
                "access": result.tokens.access,
                "refresh": result.tokens.refresh,
                "expires": result.tokens.expires,
                "account_id": result.tokens.account_id or "",
                "residency": result.tokens.residency or "",
            }
            try:
                completed = await sync_to_async(self._complete)(
                    flow.id, organization_id, user, credentials, poll_token
                )
            except (AuthenticationError, NotFoundError):
                await sync_to_async(ProviderOAuthRepository.set_terminal)(
                    flow.id, ProviderOAuthFlow.Status.CANCELLED, poll_token
                )
                return {"status": "cancelled"}
            if not completed:
                return {"status": "cancelled"}
            return {"status": "connected", "account_id": result.tokens.account_id or ""}
        except BaseException as exc:
            await sync_to_async(ProviderOAuthRepository.release_to_pending)(
                flow.id, poll_token
            )
            if not isinstance(exc, (ProviderError, OAuthError)):
                raise
            if isinstance(exc, ProviderError):
                raise
            raise ProviderError(str(exc)) from exc

    def _complete(
        self,
        flow_id: uuid.UUID,
        organization_id: uuid.UUID,
        user: Any,
        credentials: dict[str, Any],
        poll_token: uuid.UUID,
    ) -> bool:
        """Save exact account and consume its flow as one transaction."""
        with self.accounts.repository.account_transaction():
            flow = ProviderOAuthRepository.claim_completion(
                flow_id, organization_id, int(user.id), poll_token
            )
            if flow is None:
                return False
            membership = self.organizations.require_active_membership(
                int(user.id), organization_id
            )
            if flow.shared and membership.role != "admin":
                raise AuthenticationError("Only org admins can share provider accounts")
            if flow.target_account_id is not None:
                target = self.accounts.get_account(
                    organization_id,
                    int(user.id),
                    flow.target_account_id,
                    manage=True,
                )
                if (
                    target.provider != "chatgpt"
                    or (target.user_id is None) != flow.shared
                ):
                    raise NotFoundError("ProviderAccount", str(flow.target_account_id))
            account = self.accounts.save_account(
                organization_id=organization_id,
                user=user,
                provider="chatgpt",
                payload={"label": flow.label, "shared": flow.shared, **credentials},
                account_id=flow.target_account_id,
            )
            self.accounts.selected_accounts(organization_id, int(user.id))
            ProviderOAuthRepository.mark_completed(
                flow,
                str(account.id),
                str(credentials.get("account_id") or ""),
            )
            from .providers.models_catalog import clear_models_cache

            clear_models_cache(str(organization_id))
            return True

    def cancel(
        self,
        *,
        organization_id: uuid.UUID,
        user: Any,
        flow_id: uuid.UUID | str,
    ) -> bool:
        """Cancel the specified flow within the caller's organization and user."""
        organization_id, user_id = self.accounts._ids(organization_id, user)
        self.organizations.require_active_membership(user_id, organization_id)
        return ProviderOAuthRepository.cancel(flow_id, organization_id, user_id)
