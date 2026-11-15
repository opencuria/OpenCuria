"""
Provider catalog protocol parsing, fetches and static capabilities.

These functions consume explicit protocol inputs and never authorize accounts
or resolve stored secrets. Account-aware discovery and caching belong to
``model_catalog_service``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from .base import ProviderAuthError, ProviderResponseError, ProviderTimeoutError
from .bedrock import DEFAULT_REGION, resolve_bedrock_model_id
from .openrouter import DEFAULT_BASE_URL

MODELS_PATH = "/models"
FETCH_TIMEOUT_SECONDS = 30.0

# Known OpenRouter effort tokens (plus empty = provider default).
ALLOWED_REASONING_EFFORTS = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
)


def _coerce_nonneg_int(value: Any) -> int:
    """Coerce *value* to a non-negative int; return 0 when invalid."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float) and value.is_integer():
        return max(0, int(value))
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return 0
        try:
            return max(0, int(stripped))
        except ValueError:
            return 0
    return 0


@dataclass(frozen=True)
class ProviderModel:
    """Normalized catalog entry for the composer model picker."""

    id: str
    name: str
    reasoning_efforts: tuple[str, ...]
    default_effort: str
    supports_tools: bool
    context_length: int = 0
    max_output_tokens: int = 0
    provider: str = "openrouter"


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

    top_provider = raw.get("top_provider")
    top_provider_dict = top_provider if isinstance(top_provider, dict) else None

    if "context_length" in raw:
        context_length = _coerce_nonneg_int(raw.get("context_length"))
    elif top_provider_dict is not None:
        context_length = _coerce_nonneg_int(top_provider_dict.get("context_length"))
    else:
        context_length = 0

    if top_provider_dict is not None:
        max_output_tokens = _coerce_nonneg_int(
            top_provider_dict.get("max_completion_tokens")
        )
    else:
        max_output_tokens = 0

    return ProviderModel(
        id=model_id.strip(),
        name=name,
        reasoning_efforts=tuple(efforts),
        default_effort=default_effort,
        supports_tools=supports_tools,
        context_length=context_length,
        max_output_tokens=max_output_tokens,
        provider="openrouter",
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


_CHATGPT_MODEL_SPECS: tuple[dict[str, Any], ...] = (
    {
        "id": "gpt-5.6-sol",
        "name": "GPT-5.6 Sol",
        "reasoning_efforts": (
            "none",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        ),
        "default_effort": "medium",
        "context_length": 1_050_000,
        "max_output_tokens": 128_000,
    },
    {
        "id": "gpt-5.6-terra",
        "name": "GPT-5.6 Terra",
        "reasoning_efforts": (
            "none",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        ),
        "default_effort": "medium",
        "context_length": 1_050_000,
        "max_output_tokens": 128_000,
    },
    {
        "id": "gpt-5.6-luna",
        "name": "GPT-5.6 Luna",
        "reasoning_efforts": (
            "none",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        ),
        "default_effort": "medium",
        "context_length": 1_050_000,
        "max_output_tokens": 128_000,
    },
    {
        "id": "gpt-6-astra",
        "name": "GPT-6 Astra",
        "reasoning_efforts": ("low", "medium", "high", "xhigh", "max"),
        "default_effort": "medium",
        "context_length": 1_050_000,
        "max_output_tokens": 128_000,
    },
    {
        "id": "gpt-6-sol",
        "name": "GPT-6 Sol",
        "reasoning_efforts": (
            "none",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        ),
        "default_effort": "medium",
        "context_length": 1_050_000,
        "max_output_tokens": 128_000,
    },
    {
        "id": "gpt-6-luna",
        "name": "GPT-6 Luna",
        "reasoning_efforts": (
            "none",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        ),
        "default_effort": "medium",
        "context_length": 1_050_000,
        "max_output_tokens": 128_000,
    },
    {
        "id": "gpt-6.1-sol",
        "name": "GPT-6.1 Sol",
        "reasoning_efforts": (
            "none",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        ),
        "default_effort": "medium",
        "context_length": 1_050_000,
        "max_output_tokens": 128_000,
    },
)


def chatgpt_models() -> list[ProviderModel]:
    """Return the static ChatGPT OAuth allowlist catalog."""
    models: list[ProviderModel] = []
    for spec in _CHATGPT_MODEL_SPECS:
        model_id = str(spec["id"])
        models.append(
            ProviderModel(
                id=f"chatgpt/{model_id}",
                name=str(spec["name"]),
                reasoning_efforts=tuple(spec["reasoning_efforts"]),
                default_effort=str(spec["default_effort"]),
                supports_tools=True,
                context_length=int(spec["context_length"]),
                max_output_tokens=int(spec["max_output_tokens"]),
                provider="chatgpt",
            )
        )
    return models


_BEDROCK_MODEL_SPECS: tuple[dict[str, Any], ...] = (
    {
        "id": "anthropic.claude-opus-4-6-v1",
        "name": "Claude Opus 4.6",
        "reasoning_efforts": ("low", "medium", "high", "xhigh"),
        "default_effort": "medium",
        "supports_tools": True,
        "context_length": 1_000_000,
        "max_output_tokens": 128_000,
    },
    {
        "id": "anthropic.claude-sonnet-4-5",
        "name": "Claude Sonnet 4.5",
        "reasoning_efforts": ("low", "medium", "high", "xhigh"),
        "default_effort": "medium",
        "supports_tools": True,
        "context_length": 200_000,
        "max_output_tokens": 64_000,
    },
    {
        "id": "anthropic.claude-opus-4-5",
        "name": "Claude Opus 4.5",
        "reasoning_efforts": ("low", "medium", "high", "xhigh"),
        "default_effort": "medium",
        "supports_tools": True,
        "context_length": 200_000,
        "max_output_tokens": 64_000,
    },
    {
        "id": "anthropic.claude-haiku-4-5",
        "name": "Claude Haiku 4.5",
        "reasoning_efforts": ("low", "medium", "high", "xhigh"),
        "default_effort": "medium",
        "supports_tools": True,
        "context_length": 200_000,
        "max_output_tokens": 64_000,
    },
    {
        "id": "amazon.nova-pro-v1:0",
        "name": "Amazon Nova Pro",
        "reasoning_efforts": (),
        "default_effort": "",
        "supports_tools": True,
        "context_length": 300_000,
        "max_output_tokens": 5_120,
    },
    {
        "id": "amazon.nova-lite-v1:0",
        "name": "Amazon Nova Lite",
        "reasoning_efforts": (),
        "default_effort": "",
        "supports_tools": True,
        "context_length": 300_000,
        "max_output_tokens": 5_120,
    },
    {
        "id": "amazon.nova-micro-v1:0",
        "name": "Amazon Nova Micro",
        "reasoning_efforts": (),
        "default_effort": "",
        "supports_tools": False,
        "context_length": 128_000,
        "max_output_tokens": 5_120,
    },
    {
        "id": "meta.llama3-3-70b-instruct-v1:0",
        "name": "Meta Llama 3.3 70B Instruct",
        "reasoning_efforts": (),
        "default_effort": "",
        "supports_tools": True,
        "context_length": 128_000,
        "max_output_tokens": 2_048,
    },
    {
        "id": "meta.llama3-2-90b-instruct-v1:0",
        "name": "Meta Llama 3.2 90B Instruct",
        "reasoning_efforts": (),
        "default_effort": "",
        "supports_tools": True,
        "context_length": 128_000,
        "max_output_tokens": 2_048,
    },
    {
        "id": "deepseek.r1-v1:0",
        "name": "DeepSeek R1",
        "reasoning_efforts": ("low", "medium", "high"),
        "default_effort": "medium",
        "supports_tools": False,
        "context_length": 128_000,
        "max_output_tokens": 32_768,
    },
)


def bedrock_models(region: str) -> list[ProviderModel]:
    """Return the curated Amazon Bedrock catalog for *region*."""
    resolved_region = region or DEFAULT_REGION
    models: list[ProviderModel] = []
    for spec in _BEDROCK_MODEL_SPECS:
        bare_id = str(spec["id"])
        resolved_id = resolve_bedrock_model_id(bare_id, resolved_region)
        models.append(
            ProviderModel(
                id=f"amazon-bedrock/{resolved_id}",
                name=str(spec["name"]),
                reasoning_efforts=tuple(spec["reasoning_efforts"]),
                default_effort=str(spec["default_effort"]),
                supports_tools=bool(spec["supports_tools"]),
                context_length=int(spec["context_length"]),
                max_output_tokens=int(spec["max_output_tokens"]),
                provider="amazon-bedrock",
            )
        )
    return models


def openai_compatible_models(model_ids: list[str]) -> list[ProviderModel]:
    """Return catalog entries for manually configured model ids.

    Endpoints without ``/models`` are listed by hand, so no network
    fetch happens here. Manually configured models are marked
    ``supports_tools=False`` with zero limits.
    """
    models: list[ProviderModel] = []
    seen: set[str] = set()
    for raw in model_ids:
        bare = str(raw or "").strip()
        if not bare or bare in seen:
            continue
        seen.add(bare)
        models.append(
            ProviderModel(
                id=f"openai-compatible/{bare}",
                name=bare,
                reasoning_efforts=(),
                default_effort="",
                supports_tools=False,
                context_length=0,
                max_output_tokens=0,
                provider="openai-compatible",
            )
        )
    return models
