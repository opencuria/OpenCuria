"""
Per-model provider resolution for harness runs.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import structlog
from asgiref.sync import sync_to_async

from ..provider_accounts import ProviderAccountService
from ..repositories import ProviderConnectionRepository
from .base import ChatOptions, Delta, LLMMessage, ProviderAdapter, ToolSchema
from .bedrock import DEFAULT_REGION, BedrockAdapter, resolve_bedrock_model_id
from .chatgpt import ChatGPTAdapter
from .chatgpt_credentials import get_fresh_chatgpt_credentials
from .model_ref import namespaced_model_id, parse_model_ref
from .models_catalog import (
    ProviderModel,
    get_cached_account_catalog,
    selected_catalog_connections,
)
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


class AccountBoundAdapter(ProviderAdapter):
    """Authorize answer dependencies asynchronously before every provider call."""

    def __init__(
        self, adapter: ProviderAdapter, account_id: str, callback: Callable
    ) -> None:
        self.adapter = adapter
        self.name = adapter.name
        self.account_id = account_id
        self.callback = callback

    async def chat_stream(
        self,
        model: str,
        messages: list[LLMMessage],
        tools: list[ToolSchema],
        opts: ChatOptions | None = None,
    ) -> AsyncIterator[Delta]:
        if inspect.iscoroutinefunction(self.callback):
            await self.callback(self.account_id)
        else:
            result = await sync_to_async(self.callback)(self.account_id)
            if inspect.isawaitable(result):
                await result
        stream = self.adapter.chat_stream(model, messages, tools, opts)
        try:
            async for delta in stream:
                yield delta
        finally:
            await stream.aclose()


class ProviderResolver:
    """Resolve model refs to connected provider adapters for an organization."""

    def __init__(
        self,
        organization_id: uuid.UUID,
        *,
        registry: ProviderRegistry | None = None,
        connection_repository: type[ProviderConnectionRepository] | None = None,
        user_id: int | None = None,
        account_ids: dict[str, str] | None = None,
        on_account_used: Callable | None = None,
    ) -> None:
        self._organization_id = organization_id
        self._user_id = user_id
        self._registry = registry or default_registry
        self._connections = connection_repository or ProviderConnectionRepository
        connections = selected_catalog_connections(
            organization_id,
            user_id,
            account_ids,
            self._connections,
        )
        self._connection_by_provider = {
            row.provider: row for row in connections if row.provider != "claude-agent"
        }
        self._on_account_used = on_account_used
        self._adapters: dict[str, ProviderAdapter] = {}
        cached = get_cached_account_catalog(organization_id, connections) or []
        self._catalog_by_id = {model.id: model for model in cached}

    def prepare_adapters(self) -> None:
        """Read vault credentials off the event loop before sync model routing."""
        for provider, connection in self._connection_by_provider.items():
            self._get_adapter(provider, connection)

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
        if self._on_account_used is not None:
            adapter = AccountBoundAdapter(
                adapter, str(connection.id), self._on_account_used
            )
        self._adapters[provider_id] = adapter
        return adapter

    def _build_adapter(self, provider_id: str, connection: Any) -> ProviderAdapter:
        credentials = ProviderAccountService().get_credentials(connection)
        config = dict(connection.config or {})
        if provider_id == "openrouter":
            api_key = str(credentials.get("api_key", "") or "")
            base_url = str(config.get("base_url") or DEFAULT_BASE_URL)
            adapter = OpenRouterAdapter(api_key=api_key, base_url=base_url)
        elif provider_id == "chatgpt":
            adapter = ChatGPTAdapter(
                credentials=credentials,
                fresh_credentials=self._chatgpt_credentials_callback(connection),
            )
        elif provider_id == "amazon-bedrock":
            if config.get("auth_method") == "access_keys":
                credentials.pop("bearer_token", None)
            elif config.get("auth_method") == "bearer":
                credentials = {"bearer_token": credentials.get("bearer_token", "")}
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

    def _chatgpt_credentials_callback(self, connection: Any) -> Callable:
        """Freeze exact account identity and initiating authority, not selection."""
        organization_id = self._organization_id
        user_id = self._user_id
        account_id = connection.id

        async def fresh_credentials(**kwargs: Any) -> dict[str, Any]:
            # Legacy internal org-only resolution may use shared accounts, but
            # cannot refresh without a live initiating member's authority.
            if user_id is None:
                from .base import ProviderAuthError

                raise ProviderAuthError(
                    "ChatGPT refresh requires an initiating user", provider="chatgpt"
                )
            return await get_fresh_chatgpt_credentials(
                organization_id, user_id, account_id, **kwargs
            )

        return fresh_credentials

    async def _chatgpt_tokens_refreshed(self, credentials: dict[str, Any]) -> None:
        connection = self._connection_by_provider.get("chatgpt")
        if connection is None:
            return
        await sync_to_async(ProviderAccountService().update_credentials)(
            connection.id,
            credentials,
        )


def wrap_provider_adapter(provider: ProviderAdapter) -> Callable[[str], ResolvedModel]:
    """Return a resolver callable for a single injected adapter (tests)."""
    return StaticModelResolver(provider).resolve
