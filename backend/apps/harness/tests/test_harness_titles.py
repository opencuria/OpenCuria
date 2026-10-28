"""Engine-independent titles, atomic renames and background task ownership."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from asgiref.sync import sync_to_async

from apps.harness.agents.definitions import get_agent
from apps.harness.harness_service import HarnessService
from apps.harness.models import ProviderTurn
from apps.harness.providers.base import Delta, ProviderAdapter
from apps.harness.repositories import HarnessMessageRepository, HarnessSessionRepository
from apps.harness.services import ProviderConfigService
from apps.harness.session_metadata_service import (
    SessionMetadataService,
    _normalize_generated_title,
    title_from_prompt,
)

PROMPT = "Please fix the login bug and add regression tests"
TITLE = "Login bug and regression tests"


class TitleProvider(ProviderAdapter):
    """Exercise the actual hidden runner without a network or engine process."""

    name = "local-title"

    def __init__(self) -> None:
        self.calls = []

    async def chat_stream(self, model, messages, tools, opts=None):  # type: ignore[no-untyped-def]
        self.calls.append((model, list(messages), tools))
        yield Delta(text=TITLE)


def _session(workspace, harness_id="native", **kwargs):
    return HarnessSessionRepository.create(
        workspace_id=workspace.id,
        organization_id=workspace.runner.organization_id,
        created_by_id=workspace.created_by_id,
        title=title_from_prompt(PROMPT),
        harness_id=harness_id,
        model="test-model",
        **kwargs,
    )


def _metadata(provider):
    changed = AsyncMock()
    service = SessionMetadataService(
        emit=AsyncMock(),
        conversations_changed=changed,
        provider_factory=lambda _org: provider,
    )
    return service, changed


def _configure(workspace) -> None:
    ProviderConfigService().save_config(
        organization_id=workspace.runner.organization_id, small_model="small-model"
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Login   bug fixed  ", "Login bug fixed"),
        ('"Login bug fixed"', "Login bug fixed"),
        ("**Login bug fixed**", "Login bug fixed"),
        ("### `Login bug fixed`", "Login bug fixed"),
        ("\nLogin bug fixed\n\nExplanation to discard", "Login bug fixed"),
        (
            "one two three four five six seven eight nine",
            "one two three four five six seven eight",
        ),
        ("a" * 300, "a" * 255),
        ("\n ** \n", ""),
        ("", ""),
    ],
)
def test_normalize_title(raw: str, expected: str) -> None:
    assert _normalize_generated_title(raw) == expected


def test_title_agent_treats_prompt_as_context() -> None:
    agent = get_agent("title")
    assert "user's language" in agent.system_prompt
    assert "context, not instructions" in agent.system_prompt
    assert agent.permissions == {"*": "deny", "question": "deny"}


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("harness_id", ["native", "claude", "codex"])
async def test_title_uses_small_model_and_notifies_for_every_engine(
    harness_workspace, harness_id
) -> None:
    _configure(harness_workspace)
    session = _session(harness_workspace, harness_id)
    provider = TitleProvider()
    service, changed = _metadata(provider)

    await service.generate_title(
        session_id=session.id, prompt=PROMPT, organization_id=session.organization_id
    )

    stored = await sync_to_async(HarnessSessionRepository.get_by_id)(session.id)
    assert stored.title == TITLE
    changed.assert_awaited_once_with(session.workspace_id)
    assert len(provider.calls) == 1
    model, messages, tools = provider.calls[0]
    assert model == "small-model"
    assert messages[-1].content == PROMPT
    assert tools == []
    assert not any("MAXIMUM STEPS" in str(row.content) for row in messages)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("harness_id", ["native", "claude", "codex"])
async def test_unconfigured_small_model_keeps_prompt_fallback(
    harness_workspace, harness_id
) -> None:
    session = _session(harness_workspace, harness_id)
    provider = TitleProvider()
    service, changed = _metadata(provider)

    await service.generate_title(
        session_id=session.id, prompt=PROMPT, organization_id=session.organization_id
    )

    stored = await sync_to_async(HarnessSessionRepository.get_by_id)(session.id)
    assert stored.title == title_from_prompt(PROMPT)
    assert provider.calls == []
    changed.assert_not_awaited()


@pytest.mark.django_db(transaction=True)
async def test_title_compare_and_swap_preserves_last_instant_rename(
    harness_workspace, monkeypatch
) -> None:
    """A rename between the service's read and the DB write must win."""
    _configure(harness_workspace)
    session = _session(harness_workspace)
    service, changed = _metadata(TitleProvider())
    persist = HarnessSessionRepository.set_generated_title

    def rename_before_write(session_id, expected_title, title):
        HarnessSessionRepository.set_title(session, "My manually chosen title")
        return persist(session_id, expected_title, title)

    monkeypatch.setattr(
        HarnessSessionRepository, "set_generated_title", rename_before_write
    )
    await service.generate_title(
        session_id=session.id, prompt=PROMPT, organization_id=session.organization_id
    )

    stored = await sync_to_async(HarnessSessionRepository.get_by_id)(session.id)
    assert stored.title == "My manually chosen title"
    changed.assert_not_awaited()


@pytest.mark.django_db(transaction=True)
async def test_title_timeout_cancels_request_and_preserves_fallback(
    harness_workspace, monkeypatch
) -> None:
    session = _session(harness_workspace)
    cancelled = asyncio.Event()

    async def blocked(*args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    runner = SimpleNamespace(run=AsyncMock(side_effect=blocked))
    monkeypatch.setattr(
        "apps.harness.session_metadata_service.build_small_model_runner",
        AsyncMock(return_value=(runner, "small-model")),
    )
    monkeypatch.setattr(
        "apps.harness.session_metadata_service.TITLE_GENERATION_TIMEOUT_SECONDS", 0.01
    )
    service, changed = _metadata(TitleProvider())
    await service.generate_title(
        session_id=session.id, prompt=PROMPT, organization_id=session.organization_id
    )

    assert cancelled.is_set()
    stored = await sync_to_async(HarnessSessionRepository.get_by_id)(session.id)
    assert stored.title == title_from_prompt(PROMPT)
    changed.assert_not_awaited()


@pytest.mark.django_db(transaction=True)
async def test_fast_answer_cannot_release_title_snapshot_before_reservation(
    harness_workspace, monkeypatch
) -> None:
    """An immediately settled answer leaves a live holder for its title."""
    _configure(harness_workspace)
    session = _session(harness_workspace)
    service = HarnessService(
        emit=AsyncMock(), provider_factory=lambda _org: TitleProvider()
    )
    monkeypatch.setattr(service, "_schedule_summary", AsyncMock())
    title_entered = asyncio.Event()
    allow_title = asyncio.Event()
    generate = service.metadata.generate_title

    async def delayed_title(**kwargs):
        title_entered.set()
        await allow_title.wait()
        await generate(**kwargs)

    monkeypatch.setattr(service.metadata, "generate_title", delayed_title)
    turn_ids = []

    async def immediate_answer(**kwargs):
        context = service._runs[str(session.id)]
        context["execution_started"] = True
        turn = await sync_to_async(service._provider_turn_service.get)(
            context["provider_turn_id"]
        )
        turn_ids.append(turn.id)
        assert turn.holds == 2
        assert any(holder.startswith("title:") for holder in turn.holder_tokens)
        await sync_to_async(HarnessMessageRepository.complete)(
            kwargs["assistant"], finish="stop"
        )
        await service._finalize_run(session, kwargs["assistant"])

    monkeypatch.setattr(service, "_execute_run", immediate_answer)
    await service.start_run(session, PROMPT, user_id=harness_workspace.created_by_id)
    task = service._tasks.get(str(session.id))
    if task is not None:
        await task
    await asyncio.wait_for(title_entered.wait(), 5)
    turn = await sync_to_async(service._provider_turn_service.get)(turn_ids[0])
    assert turn.active and turn.holds == 1
    allow_title.set()
    async with asyncio.timeout(5):
        while service._provider_turn_roots:
            await asyncio.sleep(0.01)
    stored = await sync_to_async(HarnessSessionRepository.get_by_id)(session.id)
    assert stored.title == TITLE
    turn = await sync_to_async(service._provider_turn_service.get)(turn.id)
    assert not turn.active and turn.holds == 0


@pytest.mark.django_db(transaction=True)
async def test_admission_cancelled_after_title_hold_commit_releases_both_holders(
    harness_workspace, monkeypatch
) -> None:
    """sync_to_async may commit add_hold even when its waiter is cancelled."""
    session = _session(harness_workspace)
    service = HarnessService(
        emit=AsyncMock(), provider_factory=lambda _org: TitleProvider()
    )
    reserve = service._provider_turn_service.add_hold
    turn_ids = []

    def cancel_after_commit(turn_id, holder):
        assert reserve(turn_id, holder)
        turn_ids.append(turn_id)
        raise asyncio.CancelledError()

    monkeypatch.setattr(service._provider_turn_service, "add_hold", cancel_after_commit)
    with pytest.raises(asyncio.CancelledError):
        await service.start_run(
            session, PROMPT, user_id=harness_workspace.created_by_id
        )

    turn = await sync_to_async(ProviderTurn.objects.get)(id=turn_ids[0])
    assert not turn.active
    assert turn.holds == 0
    assert turn.holder_tokens == []
    assert not service._provider_turn_roots
