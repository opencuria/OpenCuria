"""
Per-model provider resolution for harness runs.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import structlog
from asgiref.sync import sync_to_async

from common.utils import decrypt_value, encrypt_value

from ..repositories import ProviderConnectionRepository
from .base import ProviderAdapter
from .bedrock import DEFAULT_REGION, BedrockAdapter, resolve_bedrock_model_id
from .chatgpt import ChatGPTAdapter
from .model_ref import namespaced_model_id, parse_model_ref
from .models_catalog import ProviderModel, get_cached_org_catalog
from .openai_compatible import OpenAICompatibleAdapter
from .openrouter import DEFAULT_BASE_URL, OpenRouterAdapter
from .registry import ProviderRegistry, default_registry

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ResolvedModel:
    """Provider adapter and limits for one model reference."""

    adapter: ProviderAdapter
    model_id: str
    provider: str
    context_length: int
    max_output_tokens: int


class StaticModelResolver:
    """Wrap a single adapter so tests can inject :class:`FakeProvider`."""

    def __init__(
        self,
        provider: ProviderAdapter,
        *,
        provider_name: str | None = None,
    ) -> None:
        self._provider = provider
        self._provider_name = provider_name or getattr(provider, "name", "unknown")

    def resolve(self, model_ref: str) -> ResolvedModel:
        """Always return *provider* with the bare model id from *model_ref*."""
        provider_id, bare_id = parse_model_ref(model_ref)
        model_id = bare_id or model_ref.strip()
        if provider_id == "amazon-bedrock":
            model_id = resolve_bedrock_model_id(model_id, DEFAULT_REGION)
        return ResolvedModel(
            adapter=self._provider,
            model_id=model_id,
            provider=self._provider_name,
            context_length=0,
            max_output_tokens=0,
        )


class ProviderResolver:
    """Resolve model refs to connected provider adapters for an organization."""

    def __init__(
        self,
        organization_id: uuid.UUID,
        *,
        registry: ProviderRegistry | None = None,
        connection_repository: type[ProviderConnectionRepository] | None = None,
    ) -> None:
        self._organization_id = organization_id
        self._registry = registry or default_registry
        self._connections = connection_repository or ProviderConnectionRepository
        self._connection_by_provider = {
            connection.provider: connection
            for connection in self._connections.list_by_org(organization_id)
        }
        self._adapters: dict[str, ProviderAdapter] = {}
        self._catalog_by_id: dict[str, ProviderModel] | None = None

    def resolve(self, model_ref: str) -> ResolvedModel:
        """Build or reuse an adapter for the provider implied by *model_ref*."""
        provider_id, bare_id = parse_model_ref(model_ref)
        if not bare_id and not model_ref.strip():
            raise ValueError("model reference must not be empty")
        connection = self._connection_by_provider.get(provider_id)
        if connection is None:
            raise ValueError(f"Provider '{provider_id}' is not connected")
        adapter = self._get_adapter(provider_id, connection)
        api_model_id = bare_id
        if provider_id == "amazon-bedrock":
            region = str((connection.config or {}).get("region") or DEFAULT_REGION)
            api_model_id = resolve_bedrock_model_id(bare_id, region)
        catalog = self._catalog_entry(model_ref)
        context_length = catalog.context_length if catalog else 0
        max_output_tokens = catalog.max_output_tokens if catalog else 0
        return ResolvedModel(
            adapter=adapter,
            model_id=api_model_id,
            provider=provider_id,
            context_length=context_length,
            max_output_tokens=max_output_tokens,
        )

    def _catalog_entry(self, model_ref: str) -> ProviderModel | None:
        cached = get_cached_org_catalog(self._organization_id)
        if cached is None:
            return None
        if self._catalog_by_id is None:
            self._catalog_by_id = {model.id: model for model in cached}
        namespaced = namespaced_model_id(model_ref)
        if namespaced in self._catalog_by_id:
            return self._catalog_by_id[namespaced]
        stripped = (model_ref or "").strip()
        return self._catalog_by_id.get(stripped)

    def _get_adapter(self, provider_id: str, connection: Any) -> ProviderAdapter:
        cached = self._adapters.get(provider_id)
        if cached is not None:
            return cached
        adapter = self._build_adapter(provider_id, connection)
        self._adapters[provider_id] = adapter
        return adapter

    def _build_adapter(self, provider_id: str, connection: Any) -> ProviderAdapter:
        credentials = self._decrypt_credentials(connection.credentials_encrypted)
        config = dict(connection.config or {})
        if provider_id == "openrouter":
            api_key = str(credentials.get("api_key", "") or "")
            base_url = str(config.get("base_url") or DEFAULT_BASE_URL)
            adapter = OpenRouterAdapter(api_key=api_key, base_url=base_url)
        elif provider_id == "chatgpt":
            adapter = ChatGPTAdapter(
                credentials=credentials,
                on_tokens_refreshed=self._chatgpt_tokens_refreshed,
            )
        elif provider_id == "amazon-bedrock":
            region = str(config.get("region") or DEFAULT_REGION)
            adapter = BedrockAdapter(credentials=credentials, region=region)
        elif provider_id == "openai-compatible":
            base_url = str(config.get("base_url", "") or "").strip()
            if not base_url:
                raise ValueError(
                    "Provider 'openai-compatible' needs base_url in config"
                )
            api_key = str(credentials.get("api_key", "") or "")
            adapter = OpenAICompatibleAdapter(base_url=base_url, api_key=api_key)
        else:
            factory = self._registry.get(provider_id)
            adapter = factory(credentials=credentials, config=config)
        if not isinstance(adapter, ProviderAdapter):
            raise TypeError(f"Provider {provider_id!r} did not build an adapter")
        return adapter

    async def _chatgpt_tokens_refreshed(self, credentials: dict[str, Any]) -> None:
        encrypted = encrypt_value(json.dumps(credentials))
        connection = self._connection_by_provider.get("chatgpt")
        if connection is None:
            log.warning(
                "chatgpt_token_refresh_missing_connection",
                organization_id=str(self._organization_id),
            )
            return
        connection.credentials_encrypted = encrypted

        def _persist() -> None:
            connection.save(
                update_fields=["credentials_encrypted", "updated_at"],
            )

        await sync_to_async(_persist)()

    @staticmethod
    def _decrypt_credentials(credentials_encrypted: str) -> dict[str, Any]:
        if not (credentials_encrypted or "").strip():
            return {}
        payload = decrypt_value(credentials_encrypted)
        parsed = json.loads(payload)
        return parsed if isinstance(parsed, dict) else {}


def wrap_provider_adapter(provider: ProviderAdapter) -> Callable[[str], ResolvedModel]:
    """Return a resolver callable for a single injected adapter (tests)."""
    return StaticModelResolver(provider).resolve
