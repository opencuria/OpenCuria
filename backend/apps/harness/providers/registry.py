"""
Registry for LLM provider adapters.

Adapters register a factory (class or callable) under a short name
(e.g. ``"openrouter"``). New providers can be added without changing
existing code by registering a new factory.
"""

from __future__ import annotations

from collections.abc import Callable

from .base import ProviderAdapter

AdapterFactory = Callable[..., ProviderAdapter]


class ProviderRegistry:
    """Maps provider names to adapter factories."""

    def __init__(self) -> None:
        self._factories: dict[str, AdapterFactory] = {}

    def register(self, name: str, factory: AdapterFactory) -> None:
        """Register a factory under *name*."""
        key = name.strip().lower()
        if not key:
            raise ValueError("Provider name must not be empty")
        self._factories[key] = factory

    def register_adapter(self, adapter: ProviderAdapter) -> None:
        """Register an existing adapter instance under its ``name``."""
        key = adapter.name.strip().lower()
        if not key:
            raise ValueError("Adapter name must not be empty")
        captured = adapter
        self._factories[key] = lambda *a, **k: captured

    def get(self, name: str) -> AdapterFactory:
        """Return the factory registered under *name*.

        Raises:
            KeyError: If no factory is registered under *name*.
        """
        key = name.strip().lower()
        try:
            return self._factories[key]
        except KeyError as exc:
            raise KeyError(f"Unknown provider: {name!r}") from exc

    def names(self) -> list[str]:
        """Return all registered provider names."""
        return sorted(self._factories)

    def __contains__(self, name: object) -> bool:
        """Return True if *name* is a registered provider."""
        return isinstance(name, str) and name.strip().lower() in self._factories


default_registry = ProviderRegistry()
