"""Business logic for named, organization-context provider accounts."""

from __future__ import annotations

import uuid
from typing import Any

from django.db import transaction

from apps.credentials.services import CredentialSvc
from apps.organizations.repositories import MembershipRepository
from common.exceptions import AuthenticationError, NotFoundError

from .enums import ProviderType
from .models import ProviderConnection
from .provider_account_repository import ProviderAccountRepository

CLAUDE_AGENT = "claude-agent"
SUPPORTED_PROVIDERS = (*ProviderType.values, CLAUDE_AGENT)
SECRET_FIELDS: dict[str, tuple[str, ...]] = {
    "openrouter": ("api_key",),
    "chatgpt": ("access", "refresh", "expires", "account_id", "residency"),
    "amazon-bedrock": (
        "access_key_id",
        "secret_access_key",
        "session_token",
        "bearer_token",
    ),
    "openai-compatible": ("api_key",),
    CLAUDE_AGENT: ("token",),
}
CONFIG_FIELDS: dict[str, tuple[str, ...]] = {
    "openrouter": ("base_url",),
    "chatgpt": (),
    "amazon-bedrock": ("region", "auth_method"),
    "openai-compatible": ("base_url", "models"),
    CLAUDE_AGENT: ("auth_type",),
}


class ProviderAccountService:
    """Manage provider account lifecycle, visibility, secrets, and selection."""

    def __init__(self, repository: type[ProviderAccountRepository] | None = None):
        self.repository = repository or ProviderAccountRepository
        self.secret_service = CredentialSvc()

    @staticmethod
    def _ids(organization_id: Any, user: Any) -> tuple[uuid.UUID, int]:
        return getattr(organization_id, "id", organization_id), getattr(
            user, "id", user
        )

    @staticmethod
    def _membership(organization_id: uuid.UUID, user_id: int, *, lock: bool = False):
        membership = MembershipRepository.get_for_user_and_org(
            user_id, organization_id, lock=lock
        )
        if membership is None or not membership.user.is_active:
            raise AuthenticationError("Active organization membership required")
        return membership

    @staticmethod
    def _provider(provider: str) -> str:
        cleaned = (provider or "").strip().lower()
        if cleaned not in SUPPORTED_PROVIDERS:
            raise ValueError(f"Unsupported provider '{cleaned}'")
        return cleaned

    @staticmethod
    def _parse_account_id(account_id: uuid.UUID | str) -> uuid.UUID:
        try:
            return uuid.UUID(str(account_id))
        except (TypeError, ValueError, AttributeError):
            raise NotFoundError("ProviderAccount", str(account_id)) from None

    @staticmethod
    def _lock_fence() -> None:
        """Serialize account mutations with provider-turn admission and use."""
        from .provider_turn_repository import ProviderTurnRepository

        ProviderTurnRepository.lock_mutex()

    @staticmethod
    def _assert_secret_binding(account: ProviderConnection) -> None:
        """Reject malformed or cross-account internal credential references."""
        if account.credential_id is None:
            return
        credential = account.credential
        if (
            credential is None
            or credential.service.credential_type != "provider_secret"
            or credential.service.organization_id != account.organization_id
            or credential.service.slug != f"provider-account-{account.provider}"
            or credential.organization_id
            != (account.organization_id if account.user_id is None else None)
            or credential.user_id != account.user_id
        ):
            raise NotFoundError("ProviderAccount", str(account.id))

    def _visible_account(
        self,
        organization_id: uuid.UUID,
        user_id: int,
        account_id: uuid.UUID | str,
        *,
        manage: bool = False,
    ) -> ProviderConnection:
        parsed_id = self._parse_account_id(account_id)
        account = self.repository.get_visible(
            organization_id, user_id, parsed_id, lock=False
        )
        if account is None:
            raise NotFoundError("ProviderAccount", str(account_id))
        if manage:
            membership = self._membership(organization_id, user_id)
            if account.user_id is None and membership.role != "admin":
                raise AuthenticationError(
                    "Only org admins can manage shared provider accounts"
                )
            if account.user_id is not None and account.user_id != user_id:
                raise NotFoundError("ProviderAccount", str(account_id))
        return account

    def get_account(
        self,
        organization_id: uuid.UUID,
        user_id: int,
        account_id: uuid.UUID | str,
        *,
        manage: bool = False,
    ) -> ProviderConnection:
        """Get a visible account, optionally verifying mutation permissions."""
        self._membership(organization_id, user_id)
        return self._visible_account(
            organization_id, user_id, account_id, manage=manage
        )

    def list_accounts(
        self, organization_id: uuid.UUID, user_id: int, provider: str | None = None
    ) -> list[dict[str, Any]]:
        """Return safe account metadata visible to the requesting member."""
        provider_filter = self._provider(provider) if provider is not None else None
        with transaction.atomic():
            self._lock_fence()
            self._membership(organization_id, user_id, lock=True)
            accounts = self.repository.list_visible(
                organization_id, user_id, provider_filter
            )
            selected = self._selected_accounts_locked(organization_id, user_id)
            result: list[dict[str, Any]] = []
            for account in accounts:
                secret = self.get_credentials(account) if account.credential_id else {}
                config = dict(account.config or {})
                selected_account = selected.get(account.provider)
                result.append(
                    {
                        "id": str(account.id),
                        "provider": account.provider,
                        "label": account.label,
                        "scope": account.scope,
                        "connected": account.credential_id is not None,
                        "is_active": bool(
                            selected_account is not None
                            and selected_account.id == account.id
                        ),
                        "can_manage": account.user_id == user_id
                        or self._is_admin(organization_id, user_id),
                        "base_url": config.get("base_url", ""),
                        "api_key_hint": self._hint_secret(account.provider, secret),
                        "account_id": secret.get("account_id", ""),
                        "region": config.get("region", ""),
                        "auth_method": config.get("auth_method", "")
                        or (
                            "bearer"
                            if secret.get("bearer_token")
                            else "access_keys"
                            if secret.get("access_key_id")
                            and secret.get("secret_access_key")
                            else ""
                        ),
                        "models": list(config.get("models", []) or []),
                        "auth_type": config.get("auth_type", ""),
                        "created_at": account.created_at,
                        "updated_at": account.updated_at,
                    }
                )
            return result

    def save_account(
        self,
        *,
        organization_id: uuid.UUID,
        user,
        provider: str,
        payload: dict[str, Any],
        account_id: uuid.UUID | str | None = None,
    ) -> ProviderConnection:
        """Create/update an account under the admission fence and current ACL."""
        organization_id, user_id = self._ids(organization_id, user)
        provider = self._provider(provider)
        parsed_id = (
            self._parse_account_id(account_id) if account_id is not None else None
        )
        if "shared" in payload and not isinstance(payload["shared"], bool):
            raise ValueError("shared must be a boolean")

        with transaction.atomic():
            self._lock_fence()
            membership = self._membership(organization_id, user_id, lock=True)
            existing = (
                self.repository.get_visible(
                    organization_id, user_id, parsed_id, lock=True
                )
                if parsed_id is not None
                else None
            )
            if parsed_id is not None and existing is None:
                raise NotFoundError("ProviderAccount", str(account_id))
            if existing is not None and existing.provider != provider:
                raise NotFoundError("ProviderAccount", str(account_id))

            is_admin = membership.role == "admin"
            requested_shared = (
                payload["shared"]
                if "shared" in payload
                else (existing.user_id is None if existing is not None else False)
            )
            if existing is not None and existing.user_id is None and not is_admin:
                raise AuthenticationError(
                    "Only org admins can manage shared provider accounts"
                )
            if existing is not None and existing.user_id not in (None, user_id):
                raise NotFoundError("ProviderAccount", str(account_id))
            if requested_shared and not is_admin:
                raise AuthenticationError("Only org admins can share provider accounts")
            user_owner_id = None if requested_shared else user_id
            old_user_id = existing.user_id if existing is not None else None
            old_config = dict(existing.config or {}) if existing else {}
            old_secret = self.get_credentials(existing) if existing else {}

            label = str(
                payload.get("label") or (existing.label if existing else "") or ""
            ).strip()
            if not label:
                label = provider.replace("-", " ").title()
            if len(label) > 255:
                raise ValueError("label must be at most 255 characters")
            existing_labels = self.repository.list_visible(
                organization_id, user_id, provider
            )
            if any(
                row.id != (existing.id if existing else None)
                and row.user_id == user_owner_id
                and row.label.casefold() == label.casefold()
                for row in existing_labels
            ):
                raise ValueError("An account with this label already exists")

            config = dict(old_config)
            for key in CONFIG_FIELDS[provider]:
                if key not in payload or payload[key] is None:
                    continue
                value = payload[key]
                if isinstance(value, str):
                    value = value.strip()
                    if not value and existing is not None:
                        continue
                config[key] = value
            secrets = dict(old_secret)
            for key in SECRET_FIELDS[provider]:
                value = payload.get(key)
                if value is not None:
                    value = value.strip() if isinstance(value, str) else value
                    if value != "":
                        secrets[key] = value
            self.validate_credentials(provider, payload, existing, config, secrets)

            if existing is None:
                credential = self.secret_service.create_provider_secret(
                    organization_id=organization_id,
                    user_id=user_owner_id,
                    provider=provider,
                    name=label,
                    value=secrets,
                    created_by=user,
                )
                account = self.repository.create(
                    organization_id=organization_id,
                    user_id=user_owner_id,
                    provider=provider,
                    label=label,
                    credential_id=credential.id,
                    config=config,
                )
                self._selected_accounts_locked(organization_id, user_id)
                return account

            scope_changed = old_user_id != user_owner_id
            if scope_changed and old_user_id is None:
                self._revoke_account(existing.id, allowed_user_id=user_id)

            credential_id = existing.credential_id
            created_credential = None
            if credential_id is None:
                created_credential = self.secret_service.create_provider_secret(
                    organization_id=organization_id,
                    user_id=user_owner_id,
                    provider=provider,
                    name=label,
                    value=secrets,
                    created_by=user,
                )
                credential_id = created_credential.id
            if created_credential is None and scope_changed:
                # Reassign while the account still has its old scope: the
                # repository validates the secret against that live binding.
                # The account and secret changes commit together under the
                # admission fence, so no reader observes the intermediate state.
                if not self.secret_service.reassign_provider_secret(
                    credential_id,
                    organization_id=organization_id,
                    previous_user_id=old_user_id,
                    user_id=user_owner_id,
                    provider=provider,
                    account_id=existing.id,
                ):
                    raise NotFoundError("ProviderAccount", str(existing.id))
            account = self.repository.update(
                existing,
                label=label,
                credential_id=credential_id,
                config=config,
                user_id=user_owner_id,
            )
            if created_credential is None and secrets != old_secret:
                if not self.secret_service.update_provider_secret(
                    credential_id,
                    secrets,
                    organization_id=organization_id,
                    user_id=user_owner_id,
                    provider=provider,
                    account_id=account.id,
                ):
                    raise NotFoundError("ProviderAccount", str(account.id))
            if scope_changed and old_user_id is None:
                self.repository.clear_selections_for_others(account.id, user_id)
                transaction.on_commit(lambda: self._notify_revoked(account.id))
            return account

    @staticmethod
    def validate_credentials(
        provider: str,
        payload: dict[str, Any],
        existing: ProviderConnection | None,
        config: dict[str, Any],
        secrets: dict[str, Any],
    ) -> None:
        """Validate provider-specific fields while accepting partial updates."""
        if provider == "openai-compatible":
            from .services import ProviderConfigService

            if not config.get("base_url") and existing is not None:
                config["base_url"] = str((existing.config or {}).get("base_url") or "")
            if not config.get("base_url"):
                raise ValueError("base_url is required")
            config["base_url"] = (
                ProviderConfigService.validate_openai_compatible_base_url(
                    str(config["base_url"])
                )
            )
            if not isinstance(config.get("models", []), list):
                raise ValueError("models must be a list")
            try:
                config["models"] = (
                    ProviderConfigService.normalize_openai_compatible_models(
                        config.get("models", [])
                    )
                )
            except ValueError:
                if existing is None or "models" in payload:
                    raise
                seen: set[str] = set()
                config["models"] = [
                    model
                    for item in config.get("models", [])
                    if (model := str(item or "").strip())
                    and model not in seen
                    and not seen.add(model)
                ]
        if provider == "claude-agent":
            auth_type = str(config.get("auth_type") or "api_token")
            if auth_type not in {"api_token", "subscription_token"}:
                raise ValueError("auth_type must be api_token or subscription_token")
            if (
                existing is not None
                and existing.auth_type != auth_type
                and not str(payload.get("token") or "").strip()
            ):
                raise ValueError("A token is required when changing auth_type")
            config["auth_type"] = auth_type
            token = secrets.get("token")
            if not isinstance(token, str) or not token:
                raise ValueError("token is required")
            if len(token) > 8192 or any(
                char.isspace() or ord(char) < 32 for char in token
            ):
                raise ValueError(
                    "token must be at most 8192 characters without "
                    "whitespace or control characters"
                )
            return
        if provider == "openrouter":
            from .providers.openrouter import DEFAULT_BASE_URL

            config["base_url"] = str(config.get("base_url") or DEFAULT_BASE_URL)
        if provider == "amazon-bedrock":
            config["region"] = str(config.get("region") or "us-east-1")
            auth_method = str(
                payload.get("auth_method") or config.get("auth_method") or ""
            )
            if auth_method and auth_method not in {"access_keys", "bearer"}:
                raise ValueError("auth_method must be 'access_keys' or 'bearer'")
            if auth_method:
                config["auth_method"] = auth_method
            has_bearer = bool(secrets.get("bearer_token"))
            has_sigv4 = bool(
                secrets.get("access_key_id") and secrets.get("secret_access_key")
            )
            if auth_method == "bearer":
                for key in ("access_key_id", "secret_access_key", "session_token"):
                    secrets.pop(key, None)
            elif auth_method == "access_keys":
                secrets.pop("bearer_token", None)
            if auth_method == "bearer" and not has_bearer:
                raise ValueError("bearer_token is required for bearer auth")
            if auth_method == "access_keys" and not has_sigv4:
                raise ValueError(
                    "access_key_id and secret_access_key are required "
                    "for access_keys auth"
                )
            if existing is None and not (has_bearer or has_sigv4):
                raise ValueError(
                    "Provide bearer_token or both access_key_id and secret_access_key"
                )
            return
        for field in SECRET_FIELDS[provider]:
            if field in {"expires", "account_id", "residency"}:
                continue
            if not secrets.get(field) and existing is None:
                if provider == "openai-compatible" and field == "api_key":
                    continue
                raise ValueError(f"{field} is required")

    def delete_account(
        self, organization_id: uuid.UUID, user_id: int, account_id: uuid.UUID | str
    ) -> None:
        """Soft-delete after canceling dependents within the shared fence."""
        parsed_id = self._parse_account_id(account_id)
        with transaction.atomic():
            self._lock_fence()
            membership = self._membership(organization_id, user_id, lock=True)
            account = self.repository.get_visible(
                organization_id, user_id, parsed_id, lock=True
            )
            if account is None:
                raise NotFoundError("ProviderAccount", str(account_id))
            if account.user_id is None and membership.role != "admin":
                raise AuthenticationError(
                    "Only org admins can manage shared provider accounts"
                )
            self._revoke_account(account.id)
            if account.credential_id:
                self.secret_service.delete_provider_secret(
                    account.credential_id,
                    organization_id=organization_id,
                    user_id=account.user_id,
                    provider=account.provider,
                    account_id=account.id,
                )
            self.repository.clear_selections(account.id)
            self.repository.soft_delete(account)
            transaction.on_commit(lambda: self._notify_revoked(account.id))

    def select_account(
        self,
        organization_id: uuid.UUID,
        user_id: int,
        provider: str,
        account_id: uuid.UUID | str | None,
    ) -> None:
        """Select a visible account or persist explicit no-account."""
        provider = self._provider(provider)
        selected_id = (
            self._parse_account_id(account_id) if account_id is not None else None
        )
        with transaction.atomic():
            self._lock_fence()
            self._membership(organization_id, user_id, lock=True)
            if selected_id is not None:
                account = self.repository.get_visible(
                    organization_id, user_id, selected_id, lock=True
                )
                if account is None or account.provider != provider:
                    raise NotFoundError("ProviderAccount", str(account_id))
            self.repository.set_selection(
                organization_id, user_id, provider, selected_id
            )

    def selected_accounts(
        self, organization_id: uuid.UUID, user_id: int
    ) -> dict[str, ProviderConnection]:
        """Return selected accounts, initializing only absent selection rows."""
        with transaction.atomic():
            self._lock_fence()
            self._membership(organization_id, user_id, lock=True)
            return self._selected_accounts_locked(organization_id, user_id)

    def _selected_accounts_locked(
        self, organization_id: uuid.UUID, user_id: int
    ) -> dict[str, ProviderConnection]:
        """Resolve selections while the account/turn mutex is held."""
        accounts = self.repository.list_visible(organization_id, user_id)
        by_provider: dict[str, list[ProviderConnection]] = {
            provider: [] for provider in SUPPORTED_PROVIDERS
        }
        for account in accounts:
            by_provider.setdefault(account.provider, []).append(account)
        selected: dict[str, ProviderConnection] = {}
        for provider in SUPPORTED_PROVIDERS:
            selection = self.repository.selection(organization_id, user_id, provider)
            if selection is None:
                rows = by_provider.get(provider, [])
                initial = rows[0] if rows else None
                if initial is not None:
                    winner = self.repository.initialize_selection(
                        organization_id, user_id, provider, initial.id
                    )
                    chosen = next(
                        (row for row in rows if row.id == winner.account_id), None
                    )
                    if chosen is not None:
                        selected[provider] = chosen
                continue
            if selection.account_id is None:
                continue
            account = next(
                (
                    row
                    for row in by_provider.get(provider, [])
                    if row.id == selection.account_id
                ),
                None,
            )
            if account is not None:
                selected[provider] = account
        return selected

    def get_credentials(self, account: ProviderConnection) -> dict[str, Any]:
        """Read a secret only after validating the live account binding."""
        if account.credential_id is None:
            return {}
        self._assert_secret_binding(account)
        return self.secret_service.get_provider_secret(
            account.credential_id,
            organization_id=account.organization_id,
            user_id=account.user_id,
            provider=account.provider,
            account_id=account.id,
        )

    def _save_legacy_account(
        self,
        *,
        organization_id: uuid.UUID,
        provider: str,
        payload: dict[str, Any],
    ) -> ProviderConnection:
        """Org-shared compatibility upsert; never touches personal accounts."""
        provider = self._provider(provider)
        with transaction.atomic():
            self._lock_fence()
            rows = self.repository.list_for_legacy_org_provider(
                organization_id, provider
            )
            if len(rows) > 1:
                raise ValueError(
                    f"Multiple shared accounts exist for provider '{provider}'"
                )
            existing = (
                self.repository.get_by_id(rows[0].id, lock=True) if rows else None
            )
            label = str(
                payload.get("label") or (existing.label if existing else provider)
            ).strip()[:255]
            old_secret = self.get_credentials(existing) if existing else {}
            secrets = dict(old_secret)
            for key in SECRET_FIELDS[provider]:
                value = payload.get(key)
                if isinstance(value, str):
                    value = value.strip()
                if value not in (None, ""):
                    secrets[key] = value
            config = dict(existing.config or {}) if existing else {}
            for key in CONFIG_FIELDS[provider]:
                value = payload.get(key)
                if isinstance(value, str):
                    value = value.strip()
                if value not in (None, ""):
                    config[key] = value
            if provider == "openai-compatible":
                from .services import ProviderConfigService

                config["base_url"] = (
                    ProviderConfigService.validate_openai_compatible_base_url(
                        str(config.get("base_url") or "")
                    )
                )
                config["models"] = list(
                    payload.get("models", config.get("models", [])) or []
                )
            if provider == "claude-agent":
                auth_type = str(
                    payload.get("auth_type")
                    or (existing.auth_type if existing else "api_token")
                )
                if auth_type not in {
                    "api_token",
                    "subscription_token",
                } or not secrets.get("token"):
                    raise ValueError("A valid Claude token and auth_type are required")
                config["auth_type"] = auth_type
            if existing is None:
                credential = self.secret_service.create_provider_secret(
                    organization_id=organization_id,
                    user_id=None,
                    provider=provider,
                    name=label,
                    value=secrets,
                    created_by=None,
                )
                return self.repository.create(
                    organization_id=organization_id,
                    user_id=None,
                    provider=provider,
                    label=label,
                    credential_id=credential.id,
                    config=config,
                )
            credential_id = existing.credential_id
            if credential_id is None:
                credential = self.secret_service.create_provider_secret(
                    organization_id=organization_id,
                    user_id=None,
                    provider=provider,
                    name=label,
                    value=secrets,
                    created_by=None,
                )
                credential_id = credential.id
                existing = self.repository.update(
                    existing,
                    label=label,
                    credential_id=credential_id,
                    config=config,
                    user_id=None,
                )
            elif any(
                payload.get(key) not in (None, "") for key in SECRET_FIELDS[provider]
            ):
                if not self.secret_service.update_provider_secret(
                    credential_id,
                    secrets,
                    organization_id=organization_id,
                    user_id=None,
                    provider=provider,
                    account_id=existing.id,
                ):
                    raise NotFoundError("ProviderAccount", str(existing.id))
            return self.repository.update(
                existing,
                label=label,
                credential_id=credential_id,
                config=config,
                user_id=None,
            )

    def delete_legacy_account(self, organization_id: uuid.UUID, provider: str) -> bool:
        """Delete the unambiguous org-shared account using the common fence."""
        provider = self._provider(provider)
        with transaction.atomic():
            self._lock_fence()
            rows = self.repository.list_for_legacy_org_provider(
                organization_id, provider
            )
            if len(rows) > 1:
                raise ValueError(
                    f"Multiple shared accounts exist for provider '{provider}'"
                )
            if not rows:
                return False
            account = self.repository.get_by_id(rows[0].id, lock=True)
            if account is None:
                return False
            self._revoke_account(account.id)
            if account.credential_id:
                self.secret_service.delete_provider_secret(
                    account.credential_id,
                    organization_id=organization_id,
                    user_id=None,
                    provider=provider,
                    account_id=account.id,
                )
            self.repository.clear_selections(account.id)
            self.repository.soft_delete(account)
            transaction.on_commit(lambda: self._notify_revoked(account.id))
            return True

    def update_credentials(
        self, account_id: uuid.UUID | str, credentials: dict[str, Any]
    ) -> None:
        """Persist refresh by exact ID, serializing merge with metadata writes."""
        parsed_id = self._parse_account_id(account_id)
        with transaction.atomic():
            self._lock_fence()
            account = self.repository.get_by_id(parsed_id, lock=True)
            if account is None or account.credential_id is None:
                raise NotFoundError("ProviderAccount", str(account_id))
            current = self.get_credentials(account)
            provider = self._provider(account.provider)
            for key in SECRET_FIELDS[provider]:
                value = credentials.get(key)
                if value is not None and value != "":
                    current[key] = value
            if not self.secret_service.update_provider_secret(
                account.credential_id,
                current,
                organization_id=account.organization_id,
                user_id=account.user_id,
                provider=provider,
                account_id=account.id,
            ):
                raise NotFoundError("ProviderAccount", str(account_id))

    @staticmethod
    def _is_admin(organization_id: uuid.UUID, user_id: int) -> bool:
        membership = MembershipRepository.get_for_user_and_org(user_id, organization_id)
        return bool(
            membership and membership.user.is_active and membership.role == "admin"
        )

    @staticmethod
    def _hint_secret(provider: str, credentials: dict[str, Any]) -> str:
        candidates = ("api_key", "bearer_token", "access", "token", "secret_access_key")
        secret = next(
            (str(credentials[key]) for key in candidates if credentials.get(key)), ""
        )
        if not secret:
            return ""
        # A suffix is not a mask when the whole key fits inside four chars.
        return f"••••{secret[-4:]}" if len(secret) > 4 else "••••"

    @staticmethod
    def _revoke_account(
        account_id: uuid.UUID, allowed_user_id: int | None = None
    ) -> None:
        """Revoke affected active turns in the caller's DB transaction."""
        from .provider_turn_repository import ProviderTurnRepository

        ProviderTurnRepository.revoke_account(
            account_id, allowed_user_id=allowed_user_id
        )

    @staticmethod
    def _notify_revoked(account_id: uuid.UUID) -> None:
        """Wake local answer listeners after the account change commits."""
        from .provider_turns import notify_account_revoked

        notify_account_revoked(account_id)
