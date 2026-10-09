"""Factories for engines used by the persistent harness service."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from apps.harness.providers.base import ChatOptions

from .base import HarnessEngine

EngineFactory = Callable[..., HarnessEngine]


def _create_native_engine(**kwargs: Any) -> HarnessEngine:
    """Lazily construct the built-in harness loop with its existing ports."""
    from apps.harness.runner import HarnessRunner

    return HarnessRunner(
        provider=kwargs.get("provider"),
        model_resolver=kwargs.get("model_resolver"),
        tools=kwargs["tools"],
        accessor=kwargs.get("accessor"),
        emit=kwargs.get("emit"),
        chat_options=kwargs.get("chat_options") or ChatOptions(),
    )


def _create_claude_engine(**kwargs: Any) -> HarnessEngine:
    """Lazily construct Claude with only the ports its adapter supports."""
    from .claude.engine import ClaudeEngine

    return ClaudeEngine(
        tools=kwargs["tools"],
        accessor=kwargs["accessor"],
        emit=kwargs.get("emit"),
        auth_env=kwargs.get("auth_env"),
        external_session_id=kwargs.get("external_session_id", ""),
        session_store=kwargs.get("session_store"),
        on_binding=kwargs.get("on_binding"),
        on_child_event=kwargs.get("on_child_event"),
        on_owner=kwargs.get("on_owner"),
        evaluator=kwargs.get("evaluator"),
        effort=kwargs.get("effort", "high"),
    )


class DefaultEngineRegistry:
    """Create engines from a name-to-factory registry."""

    def __init__(self) -> None:
        self._factories: dict[str, EngineFactory] = {}
        self.register("native", _create_native_engine)
        self.register("claude", _create_claude_engine)

    @staticmethod
    def _normalize_name(name: str) -> str:
        """Normalize and validate an engine registration name."""
        if not isinstance(name, str):
            raise ValueError("Engine name must be a non-empty string")
        normalized = name.strip().lower()
        if not normalized or any(character.isspace() for character in normalized):
            raise ValueError("Engine name must be a non-empty token")
        return normalized

    def register(self, name: str, factory: EngineFactory) -> None:
        """Register a factory; duplicate normalized names are rejected."""
        normalized = self._normalize_name(name)
        if not callable(factory):
            raise TypeError("Engine factory must be callable")
        if normalized in self._factories:
            raise ValueError(f"Harness engine {normalized!r} is already registered")
        self._factories[normalized] = factory

    def create(self, harness_id: str, **kwargs: Any) -> HarnessEngine:
        """Create a registered engine, passing its factory-specific options."""
        engine_id = self._normalize_name(harness_id or "native")
        try:
            factory = self._factories[engine_id]
        except KeyError:
            raise ValueError(f"Unknown harness engine {harness_id!r}") from None
        return factory(**kwargs)


def default_engine_registry() -> DefaultEngineRegistry:
    """Return the default registry (kept lazy to avoid engine import cycles)."""
    return DefaultEngineRegistry()


__all__ = ["DefaultEngineRegistry", "default_engine_registry"]
