"""Account-scoped provider catalog discovery and revision-aware caching.

Authorize selected or answer-frozen accounts before reading the cache, resolve
secrets through the account service and compose provider protocol catalogs.
Provider parsing and static capabilities remain independent in providers.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import replace
from typing import Any

import httpx
import structlog

from .provider_accounts import ProviderAccountService
from .providers import models_catalog as provider_catalog
from .providers.bedrock import DEFAULT_REGION
from .providers.models_catalog import ProviderModel
from .providers.openrouter import DEFAULT_BASE_URL

log = structlog.get_logger(__name__)

CACHE_TTL_SECONDS = 15 * 60


class ModelsCatalogCache:
    """Process-local TTL cache keyed by organization and account revisions."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[float, list[ProviderModel]]] = {}

    def get(self, key: str) -> list[ProviderModel] | None:
        """Return a cached catalog when present and unexpired."""
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, models = entry
        if time.monotonic() >= expires_at:
            self._entries.pop(key, None)
            return None
        return models

    def set(self, key: str, models: list[ProviderModel]) -> None:
        """Store *models* under *key* with the default TTL."""
        self._entries[key] = (time.monotonic() + CACHE_TTL_SECONDS, models)

    def invalidate(self, key: str | None = None) -> None:
        """Drop one cache entry, or the entire cache when *key* is None."""
        if key is None:
            self._entries.clear()
            return
        for entry in tuple(self._entries):
            if entry == key or entry.startswith(key + ":"):
                self._entries.pop(entry, None)


_catalog_cache = ModelsCatalogCache()


def clear_models_cache(key: str | None = None) -> None:
    """Invalidate the process-local catalog cache (tests and config saves)."""
    _catalog_cache.invalidate(key)


def _namespace_openrouter_model(model: ProviderModel) -> ProviderModel:
    """Prefix OpenRouter ids with ``openrouter/`` for multi-provider catalogs."""
    bare_id = model.id.removeprefix("openrouter/")
    return replace(
        model,
        id=f"openrouter/{bare_id}",
        provider="openrouter",
    )


def _fetch_openrouter_catalog_for_connection(
    connection: Any,
    *,
    client: httpx.Client | None = None,
) -> list[ProviderModel]:
    credentials = ProviderAccountService().get_credentials(connection)
    api_key = str(credentials.get("api_key", "") or "")
    if not api_key.strip():
        return []
    base_url = str((connection.config or {}).get("base_url") or DEFAULT_BASE_URL)
    models = provider_catalog.fetch_openrouter_models(
        api_key=api_key, base_url=base_url, client=client
    )
    return [_namespace_openrouter_model(model) for model in models]


def selected_catalog_connections(
    organization_id: uuid.UUID,
    user_id: int,
    account_ids: dict[str, str] | None,
) -> list[Any]:
    """Resolve and authorize account identities before consulting a model cache."""
    service = ProviderAccountService()
    if account_ids is None:
        return list(service.selected_accounts(organization_id, user_id).values())
    connections = []
    for provider, account_id in account_ids.items():
        account = service.get_account(organization_id, user_id, account_id)
        if account.provider != provider:
            raise ValueError("Provider account does not match the requested provider")
        connections.append(account)
    return connections


def catalog_cache_key(organization_id: uuid.UUID, connections: list[Any]) -> str:
    """Include account/config revision so other workers cannot serve stale data."""
    identities = sorted(
        f"{row.provider}={row.id}@{row.updated_at.isoformat()}" for row in connections
    )
    return f"{organization_id}:accounts:" + ",".join(identities)


def get_cached_account_catalog(
    organization_id: uuid.UUID, connections: list[Any]
) -> list[ProviderModel] | None:
    """Read model limits without doing provider network I/O during resolution."""
    return _catalog_cache.get(catalog_cache_key(organization_id, connections))


def list_merged_provider_models(
    *,
    organization_id: uuid.UUID,
    user_id: int,
    client: httpx.Client | None = None,
    force_refresh: bool = False,
    account_ids: dict[str, str] | None = None,
) -> list[ProviderModel]:
    """Merge only selected, currently authorized provider accounts."""
    connections = selected_catalog_connections(
        organization_id,
        user_id,
        account_ids,
    )
    cache_key = catalog_cache_key(organization_id, connections)
    if not force_refresh:
        cached = _catalog_cache.get(cache_key)
        if cached is not None:
            return cached

    merged: list[ProviderModel] = []
    for connection in connections:
        if connection.provider == "openrouter":
            merged.extend(
                _fetch_openrouter_catalog_for_connection(connection, client=client)
            )
        elif connection.provider == "chatgpt":
            merged.extend(provider_catalog.chatgpt_models())
        elif connection.provider == "amazon-bedrock":
            region = str((connection.config or {}).get("region") or DEFAULT_REGION)
            merged.extend(provider_catalog.bedrock_models(region))
        elif connection.provider == "openai-compatible":
            raw_models = (connection.config or {}).get("models", [])
            ids = raw_models if isinstance(raw_models, list) else []
            merged.extend(
                provider_catalog.openai_compatible_models([str(item) for item in ids])
            )

    _catalog_cache.set(cache_key, merged)
    log.info(
        "provider_models_merged",
        organization_id=cache_key,
        count=len(merged),
        providers=[connection.provider for connection in connections],
    )
    return merged
