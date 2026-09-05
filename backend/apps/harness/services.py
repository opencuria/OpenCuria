"""
Service layer for the harness provider configuration.

Handles encryption/decryption of the provider API key and builds
provider adapters from the stored org-wide config.
"""

from __future__ import annotations

import uuid

import structlog

from common.exceptions import ConflictError, NotFoundError
from common.utils import decrypt_value, encrypt_value

from .models import ProviderConfig
from .providers.base import ProviderAdapter
from .providers.models_catalog import (
    ProviderModel,
    clear_models_cache,
    list_cached_provider_models,
)
from .providers.openrouter import DEFAULT_BASE_URL, OpenRouterAdapter
from .providers.registry import ProviderRegistry, default_registry
from .repositories import ProviderConfigRepository

log = structlog.get_logger(__name__)


def _ensure_default_adapters(registry: ProviderRegistry) -> None:
    """Register the built-in adapters if missing."""
    if "openrouter" not in registry:
        registry.register("openrouter", OpenRouterAdapter)


class ProviderConfigService:
    """Business logic for org-wide provider configuration."""

    def __init__(
        self,
        repository: type[ProviderConfigRepository] | None = None,
        registry: ProviderRegistry | None = None,
    ) -> None:
        self.repository = repository or ProviderConfigRepository
        self.registry = registry or default_registry
        _ensure_default_adapters(self.registry)

    def save_config(
        self,
        *,
        organization_id: uuid.UUID,
        api_key: str = "",
        base_url: str = DEFAULT_BASE_URL,
        default_model: str = "",
        small_model: str = "",
        computer_use_model: str = "",
    ) -> ProviderConfig:
        """Create or update the provider config for an organization."""
        normalized_url = (base_url or DEFAULT_BASE_URL).strip() or (DEFAULT_BASE_URL)
        existing = self.repository.get_by_org(organization_id)
        key_provided = bool(api_key and api_key.strip())

        if existing is None:
            if not key_provided:
                raise ValueError("api_key must not be empty")
            encrypted = encrypt_value(api_key.strip())
            config = self.repository.create(
                organization_id=organization_id,
                api_key_encrypted=encrypted,
                base_url=normalized_url,
                default_model=default_model.strip(),
                small_model=small_model.strip(),
                computer_use_model=computer_use_model.strip(),
            )
            clear_models_cache(str(organization_id))
            log.info("provider_config_created", organization_id=str(organization_id))
            return config

        encrypted = (
            encrypt_value(api_key.strip())
            if key_provided
            else existing.api_key_encrypted
        )
        config = self.repository.update(
            existing,
            api_key_encrypted=encrypted,
            base_url=normalized_url,
            default_model=default_model.strip(),
            small_model=small_model.strip(),
            computer_use_model=computer_use_model.strip(),
        )
        clear_models_cache(str(organization_id))
        log.info("provider_config_updated", organization_id=str(organization_id))
        return config

    def get_config(self, organization_id: uuid.UUID) -> ProviderConfig:
        """Return the provider config for an organization or raise."""
        config = self.repository.get_by_org(organization_id)
        if config is None:
            raise NotFoundError("ProviderConfig", str(organization_id))
        return config

    def get_decrypted_api_key(self, organization_id: uuid.UUID) -> str:
        """Decrypt and return the provider API key (never log it)."""
        config = self.get_config(organization_id)
        return decrypt_value(config.api_key_encrypted)

    def delete_config(self, organization_id: uuid.UUID) -> None:
        """Delete the provider config for an organization."""
        deleted = self.repository.delete_by_org(organization_id)
        if deleted == 0:
            raise NotFoundError("ProviderConfig", str(organization_id))
        clear_models_cache(str(organization_id))
        log.info("provider_config_deleted", organization_id=str(organization_id))

    def adapter_from_config(
        self,
        config: ProviderConfig,
        provider_name: str = "openrouter",
    ) -> ProviderAdapter:
        """Build a provider adapter from an already-loaded config row.

        Does not query the database. Callers that already fetched *config*
        (including via ``sync_to_async``) should use this instead of
        :meth:`build_adapter` to avoid a second ORM hit.

        Raises:
            KeyError: If the provider name is not registered.
            ConflictError: If the registered factory does not return an adapter.
        """
        factory = self.registry.get(provider_name)
        adapter = factory(
            api_key=decrypt_value(config.api_key_encrypted),
            base_url=config.base_url or DEFAULT_BASE_URL,
        )
        if not isinstance(adapter, ProviderAdapter):
            raise ConflictError(f"Provider {provider_name!r} did not build an adapter")
        return adapter

    def build_adapter(
        self,
        organization_id: uuid.UUID,
        provider_name: str = "openrouter",
    ) -> ProviderAdapter:
        """Build a provider adapter from the stored org config.

        Raises:
            KeyError: If the provider name is not registered.
            NotFoundError: If no config exists for the organization.
        """
        return self.adapter_from_config(
            self.get_config(organization_id), provider_name=provider_name
        )

    def list_models(self, organization_id: uuid.UUID) -> list[ProviderModel]:
        """Return the OpenRouter catalog for *organization_id*.

        Raises:
            NotFoundError: If no config exists or the API key is empty.
        """
        config = self.get_config(organization_id)
        if not config.api_key_encrypted:
            raise NotFoundError("ProviderConfig", str(organization_id))
        api_key = decrypt_value(config.api_key_encrypted)
        if not api_key.strip():
            raise NotFoundError("ProviderConfig", str(organization_id))
        return list_cached_provider_models(
            cache_key=str(organization_id),
            api_key=api_key,
            base_url=config.base_url or DEFAULT_BASE_URL,
        )
