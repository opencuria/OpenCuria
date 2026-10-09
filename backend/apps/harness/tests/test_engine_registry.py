"""Extensible harness engine factory registry tests."""

from __future__ import annotations

from typing import Any

import pytest

from apps.harness.engines.base import HarnessEngine
from apps.harness.engines.service_runtime import (
    DefaultEngineRegistry,
    default_engine_registry,
)


class StubEngine:
    """Small implementation used to exercise injected engine factories."""

    async def run(
        self,
        prompt: str,
        agent: str,
        model: str,
        mode: str,
        opts: Any,
    ) -> Any:
        raise NotImplementedError


def test_register_creates_engine_with_factory_specific_kwargs() -> None:
    registry = DefaultEngineRegistry()
    expected = StubEngine()
    factory_calls: list[dict[str, Any]] = []

    def factory(**kwargs: Any) -> HarnessEngine:
        factory_calls.append(kwargs)
        return expected

    registry.register("test-harness", factory)
    created = registry.create("test-harness", custom_option="value")

    assert created is expected
    assert factory_calls == [{"custom_option": "value"}]


def test_registration_and_lookup_normalize_engine_names() -> None:
    registry = DefaultEngineRegistry()
    expected = StubEngine()
    registry.register("  Test-Harness  ", lambda **_kwargs: expected)

    assert registry.create("TEST-HARNESS") is expected


def test_duplicate_registration_is_rejected_after_normalization() -> None:
    registry = DefaultEngineRegistry()
    registry.register("test-harness", lambda **_kwargs: StubEngine())

    with pytest.raises(ValueError, match="already registered"):
        registry.register(" TEST-HARNESS ", lambda **_kwargs: StubEngine())


def test_unknown_engine_is_rejected() -> None:
    registry = DefaultEngineRegistry()

    with pytest.raises(ValueError, match="Unknown harness engine 'missing'"):
        registry.create("missing")


def test_invalid_engine_name_and_factory_are_rejected() -> None:
    registry = DefaultEngineRegistry()

    with pytest.raises(ValueError, match="non-empty token"):
        registry.register("bad engine", lambda **_kwargs: StubEngine())
    with pytest.raises(TypeError, match="must be callable"):
        registry.register("test-harness", None)  # type: ignore[arg-type]


def test_native_factory_preserves_runner_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.harness import runner as runner_module

    runner_args: dict[str, Any] = {}
    expected = StubEngine()

    def fake_runner(**kwargs: Any) -> StubEngine:
        runner_args.update(kwargs)
        return expected

    monkeypatch.setattr(runner_module, "HarnessRunner", fake_runner)
    provider = object()
    model_resolver = object()
    tools = object()
    accessor = object()
    emit = object()

    created = default_engine_registry().create(
        "native",
        provider=provider,
        model_resolver=model_resolver,
        tools=tools,
        accessor=accessor,
        emit=emit,
    )

    assert created is expected
    assert runner_args["provider"] is provider
    assert runner_args["model_resolver"] is model_resolver
    assert runner_args["tools"] is tools
    assert runner_args["accessor"] is accessor
    assert runner_args["emit"] is emit
