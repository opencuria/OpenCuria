"""Characterization of shared small-model runner construction."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from functools import partial
from unittest.mock import Mock

import pytest

from apps.harness.providers.base import (
    ChatOptions,
    Delta,
    LLMMessage,
    ProviderAdapter,
    ToolSchema,
)
from apps.harness.providers.resolver import AccountBoundAdapter, StaticModelResolver
from apps.harness.runner import RunOptions
from apps.harness.services import ProviderConfigService
from apps.harness.small_model_service import build_small_model_runner
from apps.organizations.models import Organization


class RecordingProvider(ProviderAdapter):
    """Record settings passed by a real text-only harness runner."""

    name = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple[str, ChatOptions | None]] = []

    async def chat_stream(
        self,
        model: str,
        messages: list[LLMMessage],
        tools: list[ToolSchema],
        opts: ChatOptions | None = None,
    ) -> AsyncIterator[Delta]:
        """Return one answer and ensure hidden agents have no tools."""
        assert not tools
        self.calls.append((model, opts))
        yield Delta(text="Generated text")


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("small_model", [None, "", "   "])
async def test_unconfigured_small_model_never_builds_a_provider(
    organization: Organization, small_model: str | None
) -> None:
    """Missing configuration and blank model names both disable generation."""
    if small_model is not None:
        ProviderConfigService().save_config(
            organization_id=organization.id, small_model=small_model
        )
    factory = Mock(side_effect=AssertionError("Provider must not be built"))

    assert (
        await build_small_model_runner(organization.id, provider_factory=factory)
        is None
    )
    factory.assert_not_called()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("effort", ["", " HIGH "])
async def test_injected_provider_keeps_small_model_and_reasoning_effort(
    organization: Organization, effort: str
) -> None:
    """Runner construction preserves model routing and normalized effort."""
    ProviderConfigService().save_config(
        organization_id=organization.id,
        small_model=" openrouter/small-model ",
        small_effort=effort,
    )
    provider = RecordingProvider()
    factory = Mock(return_value=provider)
    small = await build_small_model_runner(organization.id, provider_factory=factory)
    assert small is not None
    runner, model = small

    result = await runner.run(
        "Describe the change",
        "commit_message",
        model,
        "build",
        RunOptions(auto_approve=True),
    )

    assert model == "openrouter/small-model"
    assert result.output == "Generated text"
    factory.assert_called_once_with(organization.id)
    assert len(provider.calls) == 1
    called_model, options = provider.calls[0]
    assert called_model == "small-model"
    assert options is not None
    assert options.reasoning_effort == (effort.strip().lower() or None)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("with_turn", [False, True])
async def test_resolver_prepares_off_event_loop_and_keeps_account_authority(
    organization: Organization, monkeypatch: pytest.MonkeyPatch, with_turn: bool
) -> None:
    """Frozen accounts and initiating user reach the resolver without ORM on loop."""
    ProviderConfigService().save_config(
        organization_id=organization.id, small_model="openrouter/small-model"
    )
    provider = RecordingProvider()
    account_id = str(uuid.uuid4())
    account_ids = {"openrouter": account_id}
    turn_id = uuid.uuid4() if with_turn else None
    turns = Mock()
    prepared: list[bool] = []

    def prepare() -> None:
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        prepared.append(True)

    def build_resolver(
        self: ProviderConfigService,
        org_id: uuid.UUID,
        *,
        user_id: int | None,
        account_ids: dict[str, str] | None,
        on_account_used: Callable[[str], None] | None,
    ) -> Mock:
        assert org_id == organization.id
        assert user_id == 42
        assert account_ids == {"openrouter": account_id}
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        if with_turn:
            assert on_account_used is not None
            adapter = AccountBoundAdapter(provider, account_id, on_account_used)
        else:
            assert on_account_used is None
            adapter = provider
        resolved = StaticModelResolver(adapter)
        return Mock(prepare_adapters=prepare, resolve=resolved.resolve)

    monkeypatch.setattr(ProviderConfigService, "build_resolver", build_resolver)
    monkeypatch.delenv("DJANGO_ALLOW_ASYNC_UNSAFE", raising=False)

    small = await build_small_model_runner(
        organization.id,
        user_id=42,
        account_ids=account_ids,
        on_account_used=partial(turns.register_use, turn_id) if turn_id else None,
    )

    assert small is not None
    assert prepared == [True]
    turns.register_use.assert_not_called()
    assert provider.calls == []
    runner, model = small
    result = await runner.run(
        "Describe the change",
        "commit_message",
        model,
        "build",
        RunOptions(auto_approve=True),
    )
    assert result.output == "Generated text"
    if with_turn:
        turns.register_use.assert_called_once_with(turn_id, account_id)
    else:
        turns.register_use.assert_not_called()
