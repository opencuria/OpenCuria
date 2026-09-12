"""Tests for Agent-S runtime wiring: persisted config, temperature, max steps."""

from __future__ import annotations

from typing import Any

import pytest

from apps.harness.agent_s.config import AgentSRunConfig
from apps.harness.agent_s.harness import resolve_run_config
from apps.harness.runner import HarnessRunner, RunOptions
from apps.harness.services import AgentSConfigService


@pytest.mark.django_db(transaction=True)
def test_harness_service_resolves_agent_s_run_config(harness_workspace) -> None:
    """computeruse sessions load AgentSConfig with grounding fallback."""
    from apps.harness.harness_service import HarnessService

    org_id = harness_workspace.runner.organization_id
    AgentSConfigService().save_config(
        org_id,
        {
            "grounding_model": "",
            "grounding_width": 1000,
            "grounding_height": 1000,
            "model_temperature": None,
            "max_steps": 9,
            "max_trajectory_length": 4,
            "enable_reflection": False,
            "enable_code_agent": True,
            "screenshot_max_dimension": 1600,
            "action_pre_delay": 0.2,
            "action_post_delay": 0.3,
            "wait_delay": 1.5,
        },
    )
    config = HarnessService._resolve_agent_s_run_config(org_id, "openrouter/acme/main")
    assert isinstance(config, AgentSRunConfig)
    assert config.main_model == "openrouter/acme/main"
    assert config.grounding_model == "openrouter/acme/main"
    assert config.max_steps == 9
    assert config.model_temperature is None
    assert config.enable_reflection is False


def test_resolve_run_config_temperature_none_and_explicit() -> None:
    """None temperature survives; explicit values pass through verbatim."""
    default = resolve_run_config(effective_model="openrouter/m")
    assert default.model_temperature is None

    explicit_none = AgentSRunConfig(
        main_model="m", grounding_model="m", model_temperature=None
    )
    resolved_none = resolve_run_config(
        effective_model="m",
        run_options=type("O", (), {"agent_s_config": explicit_none})(),
    )
    assert resolved_none.model_temperature is None

    explicit_value = AgentSRunConfig(
        main_model="m", grounding_model="m", model_temperature=0.7
    )
    resolved_value = resolve_run_config(
        effective_model="m",
        run_options=type("O", (), {"agent_s_config": explicit_value})(),
    )
    assert resolved_value.model_temperature == pytest.approx(0.7)


@pytest.mark.asyncio
async def test_worker_create_state_temperature_none() -> None:
    """WorkerState keeps None temperature (no float(None) crash)."""
    from apps.harness.agent_s.worker import create_state

    state = create_state(platform="linux", worker_engine_params={"temperature": None})
    assert state.temperature is None
    # Agent-S CLI passes no temperature: the default path stays None
    # (provider default), it must NOT invent 0.0.
    state_default = create_state(platform="linux", worker_engine_params={})
    assert state_default.temperature is None
    state_value = create_state(
        platform="linux", worker_engine_params={"temperature": 0.7}
    )
    assert state_value.temperature == pytest.approx(0.7)


@pytest.mark.asyncio
async def test_harness_completion_adapter_temperature_none() -> None:
    """HarnessCompletionAdapter forwards None temperature to ChatOptions."""
    from collections.abc import AsyncIterator

    from apps.harness.agent_s.adapters import HarnessCompletionAdapter
    from apps.harness.providers.base import (
        Delta,
        ProviderAdapter,
        Usage,
    )
    from apps.harness.providers.resolver import ResolvedModel

    class FakeProvider(ProviderAdapter):
        name = "fake"

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def chat_stream(  # type: ignore[no-untyped-def]
            self, model, messages, tools, opts=None
        ) -> AsyncIterator[Delta]:
            self.calls.append({"opts": opts})
            yield Delta(text="ok", usage=Usage(1, 1, 2))

    provider = FakeProvider()

    def _resolve(model_ref: str) -> ResolvedModel:
        return ResolvedModel(
            adapter=provider,
            model_id="bare",
            provider="fake",
            context_length=0,
            max_output_tokens=0,
        )

    adapter = HarnessCompletionAdapter(
        _resolve, main_model="fake/main", grounding_model="fake/main"
    )
    wire = [
        {"role": "system", "content": [{"type": "text", "text": "sys"}]},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "do"},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64,QUJD",
                        "detail": "high",
                    },
                },
            ],
        },
    ]
    result = await adapter.complete(
        wire, purpose="worker", temperature=None, use_thinking=False
    )
    assert result.text == "ok"
    assert provider.calls[0]["opts"].temperature is None


@pytest.mark.asyncio
async def test_runner_computeruse_uses_persisted_max_steps(monkeypatch) -> None:
    """Persisted AgentSConfig.max_steps drives the run (not agent.steps)."""
    from apps.harness.providers.base import Usage
    from apps.harness.tools import agent_s_tool_registry

    seen: dict[str, Any] = {}

    async def _fake_run(**kwargs: Any):  # type: ignore[no-untyped-def]
        seen["max_steps"] = int(kwargs["config"].max_steps)
        seen["temperature"] = kwargs["config"].model_temperature

        class _Result:
            output = "ok"
            steps = 1
            usage = Usage()
            cost = 0.0
            finish_reason = "stop"
            metadata = {}

        return _Result()

    import apps.harness.agent_s.harness as harness_mod

    monkeypatch.setattr(harness_mod, "run_agent_s_computeruse", _fake_run)
    runner = HarnessRunner(
        model_resolver=lambda ref: (_ for _ in ()).throw(AssertionError("unused")),
        tools=agent_s_tool_registry(),
        accessor=None,
    )
    persisted = AgentSRunConfig(
        main_model="m", grounding_model="m", max_steps=7, model_temperature=None
    )
    result = await runner.run(
        "do it",
        "computeruse",
        "openrouter/m",
        "build",
        RunOptions(agent_s_config=persisted),
    )
    assert result.output == "ok"
    assert seen["max_steps"] == 7
    assert seen["temperature"] is None

    # Explicit RunOptions.max_steps caps the persisted budget.
    capped = await runner.run(
        "do it",
        "computeruse",
        "openrouter/m",
        "build",
        RunOptions(agent_s_config=persisted, max_steps=3),
    )
    assert capped.output == "ok"
    assert seen["max_steps"] == 3


@pytest.mark.asyncio
async def test_runner_computeruse_default_max_steps_without_persisted(
    monkeypatch,
) -> None:
    """Without persisted config the static agent default (15) still applies."""
    from apps.harness.providers.base import Usage
    from apps.harness.tools import agent_s_tool_registry

    seen: dict[str, Any] = {}

    async def _fake_run(**kwargs: Any):  # type: ignore[no-untyped-def]
        seen["max_steps"] = int(kwargs["config"].max_steps)

        class _Result:
            output = "ok"
            steps = 1
            usage = Usage()
            cost = 0.0
            finish_reason = "stop"
            metadata = {}

        return _Result()

    import apps.harness.agent_s.harness as harness_mod

    monkeypatch.setattr(harness_mod, "run_agent_s_computeruse", _fake_run)
    runner = HarnessRunner(
        model_resolver=lambda ref: (_ for _ in ()).throw(AssertionError("unused")),
        tools=agent_s_tool_registry(),
        accessor=None,
    )
    await runner.run("do it", "computeruse", "openrouter/m", "build", RunOptions())
    assert seen["max_steps"] == 15


@pytest.mark.django_db(transaction=True)
def test_validate_provider_for_run_grounding_connected_and_missing(
    harness_workspace,
) -> None:
    """Explicit grounding models need a connection; empty falls back to main."""
    from apps.harness.harness_service import HarnessService
    from apps.harness.repositories import HarnessSessionRepository
    from apps.harness.services import ProviderConfigService

    org_id = harness_workspace.runner.organization_id
    ProviderConfigService().save_config(
        organization_id=org_id,
        default_model="openrouter/acme/main",
    )
    ProviderConfigService().save_connection(
        organization_id=org_id,
        provider="openrouter",
        credentials={"api_key": "sk-main"},
        config={"base_url": "https://openrouter.ai/api/v1"},
    )
    service = HarnessService()
    session = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=org_id,
        title="computeruse run",
        agent_name="computeruse",
        mode="build",
        model="openrouter/acme/main",
    )
    # Empty grounding falls back to main (no extra connection needed).
    assert service.validate_provider_for_run(org_id, session) == "openrouter/acme/main"

    AgentSConfigService().save_config(
        org_id, {"grounding_model": "openai-compatible/uitars"}
    )
    with pytest.raises(ValueError, match="openai-compatible"):
        service.validate_provider_for_run(org_id, session)

    ProviderConfigService().save_connection(
        organization_id=org_id,
        provider="openai-compatible",
        credentials={"api_key": ""},
        config={
            "base_url": "https://grounding.example/v1",
            "models": ["uitars"],
        },
    )
    assert service.validate_provider_for_run(org_id, session) == "openrouter/acme/main"
