"""Tests for the OpenRouter model catalog normalize/fetch/cache helpers."""

from __future__ import annotations

import json

import httpx
import pytest

from apps.harness.providers.base import ProviderAuthError, ProviderResponseError
from apps.harness.providers.models_catalog import (
    clear_models_cache,
    fetch_openrouter_models,
    list_cached_provider_models,
    normalize_openrouter_catalog,
    normalize_reasoning_effort,
)


def test_normalize_openrouter_catalog_extracts_effort_and_tools() -> None:
    """Reasoning efforts and tool support are copied from OpenRouter fields."""
    models = normalize_openrouter_catalog(
        {
            "data": [
                {
                    "id": "openai/gpt-5",
                    "name": "GPT-5",
                    "supported_parameters": ["tools", "reasoning"],
                    "reasoning": {
                        "supported_efforts": ["low", "medium", "high"],
                        "default_effort": "medium",
                    },
                },
                {"id": "plain/model", "name": "Plain", "supported_parameters": []},
                {"id": ""},
            ]
        }
    )
    assert len(models) == 2
    assert models[0].id == "openai/gpt-5"
    assert models[0].reasoning_efforts == ("low", "medium", "high")
    assert models[0].default_effort == "medium"
    assert models[0].supports_tools is True
    assert models[1].reasoning_efforts == ()
    assert models[1].supports_tools is False


def test_normalize_reasoning_effort_rejects_unknown() -> None:
    """Unknown effort tokens raise ValueError; empty stays empty."""
    assert normalize_reasoning_effort("") == ""
    assert normalize_reasoning_effort("High") == "high"
    with pytest.raises(ValueError):
        normalize_reasoning_effort("turbo")


def test_fetch_openrouter_models_happy_path() -> None:
    """GET /models is parsed into normalized catalog entries."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models")
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "acme/fast",
                        "name": "Fast",
                        "supported_parameters": ["tools"],
                    }
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    models = fetch_openrouter_models(api_key="test-key", client=client)
    assert [m.id for m in models] == ["acme/fast"]


def test_fetch_openrouter_models_auth_error() -> None:
    """HTTP 401 surfaces as ProviderAuthError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="nope")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderAuthError):
        fetch_openrouter_models(api_key="bad", client=client)


def test_fetch_openrouter_models_http_error() -> None:
    """Non-auth HTTP errors surface as ProviderResponseError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderResponseError):
        fetch_openrouter_models(api_key="k", client=client)


def test_list_cached_provider_models_hits_cache() -> None:
    """A second list for the same key does not refetch."""
    clear_models_cache()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            json={"data": [{"id": "cached/one", "name": "One"}]},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    first = list_cached_provider_models(
        cache_key="org-1", api_key="k", client=client
    )
    second = list_cached_provider_models(
        cache_key="org-1", api_key="k", client=client
    )
    assert first == second
    assert calls["n"] == 1
    clear_models_cache("org-1")
    json.dumps(first[0].to_dict())
