"""Tests for the provider registry."""

from __future__ import annotations

import pytest

from apps.harness.providers.base import ProviderAdapter
from apps.harness.providers.openrouter import OpenRouterAdapter
from apps.harness.providers.registry import ProviderRegistry


class _DummyAdapter(ProviderAdapter):
    """Minimal adapter for registry tests."""

    name = "dummy"

    async def chat_stream(self, model, messages, tools, opts=None):  # type: ignore[override]
        raise NotImplementedError
        yield


def test_register_and_lookup() -> None:
    """Registered factories and instances resolve by name."""
    registry = ProviderRegistry()
    registry.register("openrouter", OpenRouterAdapter)
    assert registry.get("OpenRouter") is OpenRouterAdapter
    assert "openrouter" in registry
    assert registry.names() == ["openrouter"]


def test_register_adapter_instance() -> None:
    """Adapter instances register under their name attribute."""
    registry = ProviderRegistry()
    registry.register_adapter(_DummyAdapter())
    assert "dummy" in registry


def test_unknown_provider_raises() -> None:
    """Looking up an unregistered provider raises KeyError."""
    registry = ProviderRegistry()
    with pytest.raises(KeyError, match="Unknown provider"):
        registry.get("does-not-exist")


def test_empty_name_rejected() -> None:
    """Empty provider names are rejected."""
    registry = ProviderRegistry()
    with pytest.raises(ValueError):
        registry.register("  ", OpenRouterAdapter)
