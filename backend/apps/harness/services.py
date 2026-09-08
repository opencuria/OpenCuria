"""
Service layer for the harness provider configuration.

Handles org-wide default models, per-provider connections, and builds
provider adapters via :class:`~apps.harness.providers.resolver.ProviderResolver`.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import structlog

from common.exceptions import ConflictError, NotFoundError
from common.utils import decrypt_value, encrypt_value

from .models import ProviderConfig, ProviderConnection
from .providers.base import ProviderAdapter
from .providers.models_catalog import (
    ProviderModel,
    clear_models_cache,
    list_merged_provider_models,
)
from .providers.bedrock import BedrockAdapter
from .providers.chatgpt import ChatGPTAdapter
from .providers.model_ref import parse_model_ref
from .providers.openrouter import DEFAULT_BASE_URL, OpenRouterAdapter
from .providers.registry import ProviderRegistry, default_registry
from .providers.resolver import ProviderResolver
from .repositories import ProviderConfigRepository, ProviderConnectionRepository

log = structlog.get_logger(__name__)


def _ensure_default_adapters(registry: ProviderRegistry) -> None:
    """Register the built-in adapters if missing."""
    if "openrouter" not in registry:
        registry.register("openrouter", OpenRouterAdapter)
    if "chatgpt" not in registry:
        # ProviderConnection resolver constructs ChatGPTAdapter with OAuth
        # credentials; registration exposes the factory for discovery only.
        registry.register("chatgpt", ChatGPTAdapter)
    if "amazon-bedrock" not in registry:
        # ProviderConnection resolver constructs BedrockAdapter with AWS
        # credentials; registration exposes the factory for discovery only.
        registry.register("amazon-bedrock", BedrockAdapter)


class ProviderConfigService:
    """Business logic for org-wide provider configuration."""

    def __init__(
        self,
        repository: type[ProviderConfigRepository] | None = None,
        connection_repository: type[ProviderConnectionRepository] | None = None,
        registry: ProviderRegistry | None = None,
    ) -> None:
        self.repository = repository or ProviderConfigRepository
        self.connections = connection_repository or ProviderConnectionRepository
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
        """Create or update org-wide default models.

        ``api_key`` and ``base_url`` are accepted for backward compatibility
        and routed to the OpenRouter :class:`ProviderConnection` when provided.
        """
        normalized_url = (base_url or DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
        existing = self.repository.get_by_org(organization_id)
        key_provided = bool(api_key and api_key.strip())

        if existing is None:
            config = self.repository.create(
                organization_id=organization_id,
                default_model=default_model.strip(),
                small_model=small_model.strip(),
                computer_use_model=computer_use_model.strip(),
            )
            log.info("provider_config_created", organization_id=str(organization_id))
        else:
            config = self.repository.update(
                existing,
                default_model=default_model.strip(),
                small_model=small_model.strip(),
                computer_use_model=computer_use_model.strip(),
            )
            log.info("provider_config_updated", organization_id=str(organization_id))

        if key_provided:
            self.save_connection(
                organization_id=organization_id,
                provider="openrouter",
                credentials={"api_key": api_key.strip()},
                config={"base_url": normalized_url},
            )
        elif existing is not None:
            connection = self.connections.get_by_org_and_provider(
                organization_id,
                "openrouter",
            )
            if connection is not None and normalized_url != DEFAULT_BASE_URL:
                self.save_connection(
                    organization_id=organization_id,
                    provider="openrouter",
                    credentials=self.get_connection_credentials(connection),
                    config={"base_url": normalized_url},
                )

        clear_models_cache(str(organization_id))
        return config

    def get_config(self, organization_id: uuid.UUID) -> ProviderConfig:
        """Return the provider config for an organization or raise."""
        config = self.repository.get_by_org(organization_id)
        if config is None:
            raise NotFoundError("ProviderConfig", str(organization_id))
        return config

    def get_decrypted_api_key(self, organization_id: uuid.UUID) -> str:
        """Decrypt and return the OpenRouter API key (never log it)."""
        connection = self.connections.get_by_org_and_provider(
            organization_id,
            "openrouter",
        )
        if connection is None:
            raise NotFoundError("ProviderConnection", "openrouter")
        credentials = self.get_connection_credentials(connection)
        return str(credentials.get("api_key", "") or "")

    def delete_config(self, organization_id: uuid.UUID) -> None:
        """Delete the provider config for an organization."""
        deleted = self.repository.delete_by_org(organization_id)
        if deleted == 0:
            raise NotFoundError("ProviderConfig", str(organization_id))
        clear_models_cache(str(organization_id))
        log.info("provider_config_deleted", organization_id=str(organization_id))

    def get_connection(
        self,
        organization_id: uuid.UUID,
        provider: str,
    ) -> ProviderConnection | None:
        """Return one provider connection for an organization."""
        return self.connections.get_by_org_and_provider(organization_id, provider)

    def list_connections(self, organization_id: uuid.UUID) -> list[ProviderConnection]:
        """Return all provider connections for an organization."""
        return self.connections.list_by_org(organization_id)

    @staticmethod
    def get_connection_credentials(connection: ProviderConnection) -> dict[str, Any]:
        """Decrypt stored provider credentials as a JSON object."""
        if not (connection.credentials_encrypted or "").strip():
            return {}
        payload = decrypt_value(connection.credentials_encrypted)
        parsed = json.loads(payload)
        return parsed if isinstance(parsed, dict) else {}

    def save_connection(
        self,
        *,
        organization_id: uuid.UUID,
        provider: str,
        credentials: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> ProviderConnection:
        """Create or update a provider connection and invalidate the catalog."""
        encrypted = encrypt_value(json.dumps(credentials))
        connection = self.connections.upsert(
            organization_id=organization_id,
            provider=provider,
            credentials_encrypted=encrypted,
            config=dict(config or {}),
        )
        clear_models_cache(str(organization_id))
        log.info(
            "provider_connection_saved",
            organization_id=str(organization_id),
            provider=provider,
        )
        return connection

    def delete_connection(
        self,
        organization_id: uuid.UUID,
        provider: str,
    ) -> None:
        """Delete a provider connection when present."""
        deleted = self.connections.delete_by_org_and_provider(organization_id, provider)
        if not deleted:
            raise NotFoundError("ProviderConnection", provider)
        clear_models_cache(str(organization_id))
        log.info(
            "provider_connection_deleted",
            organization_id=str(organization_id),
            provider=provider,
        )

    def build_resolver(self, organization_id: uuid.UUID) -> ProviderResolver:
        """Build a per-run resolver for the organization's connections."""
        return ProviderResolver(
            organization_id,
            registry=self.registry,
            connection_repository=self.connections,
        )

    def adapter_from_config(
        self,
        config: ProviderConfig,
        provider_name: str = "openrouter",
    ) -> ProviderAdapter:
        """Build a provider adapter from stored org connections.

        Legacy helper kept for tests; production runs should use
        :meth:`build_resolver`.
        """
        return self.build_adapter(config.organization_id, provider_name=provider_name)

    def build_adapter(
        self,
        organization_id: uuid.UUID,
        provider_name: str = "openrouter",
    ) -> ProviderAdapter:
        """Build a provider adapter from stored org connections.

        Raises:
            KeyError: If the provider name is not registered.
            NotFoundError: If no connection exists for the organization.
        """
        self.get_config(organization_id)
        connection = self.connections.get_by_org_and_provider(
            organization_id,
            provider_name,
        )
        if connection is None:
            raise NotFoundError("ProviderConnection", provider_name)
        credentials = self.get_connection_credentials(connection)
        config = dict(connection.config or {})
        if provider_name == "openrouter":
            adapter = OpenRouterAdapter(
                api_key=str(credentials.get("api_key", "") or ""),
                base_url=str(config.get("base_url") or DEFAULT_BASE_URL),
            )
        elif provider_name == "chatgpt":
            adapter = ChatGPTAdapter(credentials=credentials)
        elif provider_name == "amazon-bedrock":
            adapter = BedrockAdapter(
                credentials=credentials,
                region=str(config.get("region") or "us-east-1"),
            )
        else:
            factory = self.registry.get(provider_name)
            adapter = factory(credentials=credentials, config=config)
        if not isinstance(adapter, ProviderAdapter):
            raise ConflictError(f"Provider {provider_name!r} did not build an adapter")
        return adapter

    def list_models(self, organization_id: uuid.UUID) -> list[ProviderModel]:
        """Return the merged catalog for all connected providers.

        Raises:
            NotFoundError: If no ProviderConfig exists for the organization.
        """
        self.get_config(organization_id)
        connections = self.connections.list_by_org(organization_id)
        if not connections:
            raise NotFoundError("ProviderConnection", str(organization_id))
        return list_merged_provider_models(organization_id=organization_id)

    def provider_connected_for_model(self, organization_id: uuid.UUID, model: str) -> str:
        """Return the provider id for *model* when a connection exists."""
        provider_id, _ = parse_model_ref(model)
        connection = self.connections.get_by_org_and_provider(
            organization_id,
            provider_id,
        )
        if connection is None:
            raise ValueError(f"Provider '{provider_id}' is not connected")
        return provider_id
