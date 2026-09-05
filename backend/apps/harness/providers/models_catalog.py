"""
OpenRouter model catalog fetch, normalize, and in-memory cache.

The browser never talks to OpenRouter directly. The backend proxies
``GET {base_url}/models`` with the org API key and returns a UI-ready list.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from .base import ProviderAuthError, ProviderResponseError, ProviderTimeoutError
from .openrouter import DEFAULT_BASE_URL

log = structlog.get_logger(__name__)

MODELS_PATH = "/models"
CACHE_TTL_SECONDS = 15 * 60
FETCH_TIMEOUT_SECONDS = 30.0

# Known OpenRouter effort tokens (plus empty = provider default).
ALLOWED_REASONING_EFFORTS = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
)


@dataclass(frozen=True)
class ProviderModel:
    """Normalized catalog entry for the composer model picker."""

    id: str
    name: str
    reasoning_efforts: tuple[str, ...]
    default_effort: str
    supports_tools: bool

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping."""
        return {
            "id": self.id,
            "name": self.name,
            "reasoning_efforts": list(self.reasoning_efforts),
            "default_effort": self.default_effort,
            "supports_tools": self.supports_tools,
        }


class ModelsCatalogCache:
    """Process-local TTL cache keyed by organization id."""

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
        self._entries.pop(key, None)


_catalog_cache = ModelsCatalogCache()


def clear_models_cache(key: str | None = None) -> None:
    """Invalidate the process-local catalog cache (tests and config saves)."""
    _catalog_cache.invalidate(key)


def normalize_reasoning_effort(value: str | None) -> str:
    """Return a stored effort token, or ``""`` when unset.

    Raises:
        ValueError: If *value* is not a known effort token.
    """
    stripped = (value or "").strip().lower()
    if not stripped:
        return ""
    if stripped not in ALLOWED_REASONING_EFFORTS:
        raise ValueError(f"Invalid reasoning_effort {value!r}")
    return stripped


def normalize_openrouter_model(raw: Any) -> ProviderModel | None:
    """Map one OpenRouter ``/models`` object to :class:`ProviderModel`."""
    if not isinstance(raw, dict):
        return None
    model_id = raw.get("id")
    if not isinstance(model_id, str) or not model_id.strip():
        return None
    name_raw = raw.get("name")
    if isinstance(name_raw, str) and name_raw.strip():
        name = name_raw.strip()
    else:
        name = model_id

    supported = raw.get("supported_parameters")
    if not isinstance(supported, list):
        supported = []
    supported_names = {str(item) for item in supported}
    supports_tools = "tools" in supported_names

    efforts: list[str] = []
    default_effort = ""
    reasoning = raw.get("reasoning")
    if isinstance(reasoning, dict):
        raw_efforts = reasoning.get("supported_efforts") or []
        if isinstance(raw_efforts, list):
            for item in raw_efforts:
                token = str(item).strip().lower()
                if token in ALLOWED_REASONING_EFFORTS:
                    efforts.append(token)
        raw_default = reasoning.get("default_effort")
        if isinstance(raw_default, str):
            candidate = raw_default.strip().lower()
            if candidate in ALLOWED_REASONING_EFFORTS:
                default_effort = candidate
    elif "reasoning" in supported_names or "reasoning_effort" in supported_names:
        efforts = ["low", "medium", "high"]

    if default_effort and default_effort not in efforts and efforts:
        default_effort = efforts[0]
    elif not default_effort and efforts:
        default_effort = efforts[0] if "medium" not in efforts else "medium"

    return ProviderModel(
        id=model_id.strip(),
        name=name,
        reasoning_efforts=tuple(efforts),
        default_effort=default_effort,
        supports_tools=supports_tools,
    )


def normalize_openrouter_catalog(payload: Any) -> list[ProviderModel]:
    """Extract and normalize the ``data`` array from an OpenRouter catalog."""
    items: Any
    if isinstance(payload, dict):
        items = payload.get("data", [])
    elif isinstance(payload, list):
        items = payload
    else:
        items = []
    if not isinstance(items, list):
        return []
    models: list[ProviderModel] = []
    seen: set[str] = set()
    for raw in items:
        model = normalize_openrouter_model(raw)
        if model is None or model.id in seen:
            continue
        seen.add(model.id)
        models.append(model)
    return models


def fetch_openrouter_models(
    *,
    api_key: str,
    base_url: str = DEFAULT_BASE_URL,
    client: httpx.Client | None = None,
) -> list[ProviderModel]:
    """GET ``{base_url}/models`` and return normalized catalog entries.

    Raises:
        ProviderAuthError: On HTTP 401/403.
        ProviderTimeoutError: On network timeouts.
        ProviderResponseError: On other HTTP errors or malformed payloads.
    """
    if not api_key:
        raise ValueError("api_key must not be empty")
    url = f"{base_url.rstrip('/')}{MODELS_PATH}"
    headers = {"Authorization": f"Bearer {api_key}"}
    owned = client is None
    http = client or httpx.Client(timeout=FETCH_TIMEOUT_SECONDS)
    try:
        response = http.get(url, headers=headers)
    except httpx.TimeoutException as exc:
        raise ProviderTimeoutError(
            f"OpenRouter models request timed out: {exc}",
            provider="openrouter",
        ) from exc
    except httpx.HTTPError as exc:
        raise ProviderResponseError(
            f"OpenRouter models request failed: {exc}",
            provider="openrouter",
        ) from exc
    finally:
        if owned:
            http.close()

    if response.status_code in (401, 403):
        raise ProviderAuthError(
            f"OpenRouter auth failed ({response.status_code})",
            provider="openrouter",
        )
    if response.status_code >= 400:
        snippet = (response.text or "")[:500]
        raise ProviderResponseError(
            f"OpenRouter models error ({response.status_code}): {snippet}",
            provider="openrouter",
            status_code=response.status_code,
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise ProviderResponseError(
            "OpenRouter models response was not JSON",
            provider="openrouter",
        ) from exc
    return normalize_openrouter_catalog(payload)


def list_cached_provider_models(
    *,
    cache_key: str,
    api_key: str,
    base_url: str = DEFAULT_BASE_URL,
    client: httpx.Client | None = None,
    force_refresh: bool = False,
) -> list[ProviderModel]:
    """Return cached models when fresh, otherwise fetch and store."""
    if not force_refresh:
        cached = _catalog_cache.get(cache_key)
        if cached is not None:
            return cached
    models = fetch_openrouter_models(api_key=api_key, base_url=base_url, client=client)
    _catalog_cache.set(cache_key, models)
    log.info("provider_models_fetched", cache_key=cache_key, count=len(models))
    return models
