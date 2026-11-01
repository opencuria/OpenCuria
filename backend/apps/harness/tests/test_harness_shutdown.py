"""Graceful execution-worker teardown owns runs, metadata and cleanup callbacks."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from apps.harness.background_tasks import BackgroundTaskSupervisor
from apps.harness.harness_service import HarnessService
from apps.harness.run_state import RunPhase
from common.exceptions import ConflictError


async def test_shutdown_cancels_runs_without_user_abort_and_waits_for_finalization():
    service = HarnessService()
    entered, finalizing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    observed = []

    async def run():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            observed.append(
                service._user_abort_requested(SimpleNamespace(id="run", parent_id=None))
            )
            raise
        finally:
            service.run_registry.ensure("run").begin_finalization()
            finalizing.set()
            await release.wait()
            service.run_registry.release_context("run")
            service.run_registry.release_task("run", asyncio.current_task())

    task = service._spawn_background(run())
    service.run_registry.attach_context("run", {"message_id": "shell"})
    service.run_registry.start_execution("run", task)
    await entered.wait()
    stopping = asyncio.create_task(service.shutdown_runs())
    await finalizing.wait()
    assert service.run_registry.get("run").phase == RunPhase.FINALIZING
    assert not stopping.done()
    assert not service.run_registry.aborts
    with pytest.raises(ConflictError, match="worker is stopping"):
        await service.start_run(SimpleNamespace(id="new"), "prompt")
    release.set()
    await stopping
    assert observed == [False]
    assert task.cancelled()
    assert service.run_registry.get("run") is None
    await service.shutdown_runs()  # Idempotent and admission remains closed.


async def test_shutdown_does_not_cancel_an_owner_already_finalizing():
    service = HarnessService()
    entered, release = asyncio.Event(), asyncio.Event()
    persisted = []

    async def finalize():
        service.run_registry.ensure("run").begin_finalization()
        entered.set()
        await release.wait()
        persisted.append("terminal message")
        service.run_registry.release_task("run", asyncio.current_task())

    task = service._spawn_background(finalize())
    service.run_registry.start_execution("run", task)
    await entered.wait()
    stopping = asyncio.create_task(service.shutdown_runs())
    await asyncio.sleep(0)
    assert not task.cancelling()
    assert not stopping.done()
    release.set()
    await stopping
    assert persisted == ["terminal message"]


async def test_shutdown_joins_replacement_finalizer_of_preentry_cancellation():
    service = HarnessService()
    finalized = []

    async def unstarted():
        pytest.fail("Execution must be cancelled before entry")

    async def cleanup():
        await asyncio.sleep(0)
        finalized.append("shell settled")
        service.run_registry.release_context("run")
        service.run_registry.release_task("run", asyncio.current_task())

    task = service._spawn_background(unstarted())
    service.run_registry.start_execution("run", task)
    service.run_registry.attach_context("run", {"message_id": "shell"})

    def done(completed):
        if completed.cancelled():
            service.run_registry.ensure("run").begin_finalization()
            finalizer = service._spawn_background(cleanup())
            service.run_registry.start_execution("run", finalizer)

    task.add_done_callback(done)
    task.cancel()
    await service.shutdown_runs()
    assert finalized == ["shell settled"]
    assert service.run_registry.get("run") is None


async def test_shutdown_cancels_admission_and_waits_for_its_rollback():
    service = HarnessService()
    entered, rollback, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def admit():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            rollback.set()
            await release.wait()
            service.run_registry.release_admission("startup", asyncio.current_task())

    task = asyncio.create_task(admit())
    service.run_registry.begin_admission("startup", task)
    await entered.wait()
    stopping = asyncio.create_task(service.shutdown_runs())
    await rollback.wait()
    assert not stopping.done()
    assert not service.run_registry.aborts
    release.set()
    await stopping
    assert task.cancelled()
    assert service.run_registry.get("startup") is None


async def test_metadata_shutdown_releases_holds_and_drains_callback_cleanup():
    service = HarnessService()
    entered = [asyncio.Event() for _ in range(3)]
    finished = []
    holds = {"answer", "title", "summary"}
    state = SimpleNamespace(active=True, holds=3, monitor_required=True)

    def finish_once(turn_id, holder):
        holds.discard(holder)
        state.holds = len(holds)
        state.active = bool(holds) or state.monitor_required

    def clear_monitor(turn_id):
        state.monitor_required = False
        state.active = bool(holds)

    service._provider_turns = SimpleNamespace(
        finish_once=Mock(side_effect=finish_once),
        get=Mock(return_value=state),
        clear_monitor_required=Mock(side_effect=clear_monitor),
    )

    async def work(name, index):
        entered[index].set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.append(name)

    context = {
        "provider_turn_id": "turn",
        "provider_turn_holder": "answer",
        "provider_followup_tasks": {},
    }
    service._provider_turn_roots["turn"] = context
    service.run_registry.attach_context("run", context)
    for index, name in enumerate(("title", "summary")):
        task = service._spawn_background(work(name, index))
        service._watch_provider_followup(context, task, name)
        task.add_done_callback(
            lambda _done, holder=name: service._spawn_background(
                service._finish_provider_hold("turn", holder),
            ),
        )
    context["provider_monitor_task"] = service._spawn_background(work("monitor", 2))
    await asyncio.gather(*(event.wait() for event in entered))
    await service.shutdown_runs()
    assert sorted(finished) == ["monitor", "summary", "title"]
    assert holds == set()
    assert not state.active
    assert service._provider_turn_roots == {}
    assert not service.run_registry.aborts


async def test_supervisor_waits_for_cleanup_spawned_by_done_callback():
    supervisor = BackgroundTaskSupervisor()
    release = asyncio.Event()
    cleanup_started = asyncio.Event()

    async def cleanup():
        cleanup_started.set()
        await release.wait()

    original = asyncio.create_task(asyncio.sleep(0))
    supervisor.track(original)
    original.add_done_callback(
        lambda _: supervisor.track(asyncio.create_task(cleanup()))
    )
    stopping = asyncio.create_task(supervisor.shutdown(AsyncMock()))
    await cleanup_started.wait()
    assert not stopping.done()
    release.set()
    await stopping


async def test_cancelled_shutdown_caller_still_joins_owned_cleanup():
    supervisor = BackgroundTaskSupervisor()
    entered, release = asyncio.Event(), asyncio.Event()

    async def settle():
        entered.set()
        await release.wait()

    caller = asyncio.create_task(supervisor.shutdown(settle))
    await entered.wait()
    caller.cancel()
    await asyncio.sleep(0)
    assert not caller.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await caller
    await supervisor.shutdown(settle)


async def test_supervisor_propagates_teardown_failure_after_draining():
    supervisor = BackgroundTaskSupervisor()
    finished = []

    async def cleanup():
        await asyncio.sleep(0)
        finished.append(True)

    supervisor.track(asyncio.create_task(cleanup()))
    with pytest.raises(RuntimeError, match="persistence failed"):
        await supervisor.shutdown(
            AsyncMock(side_effect=RuntimeError("persistence failed"))
        )
    assert finished == [True]


@pytest.mark.django_db(transaction=True)
async def test_shutdown_persists_native_interruption_and_releases_real_provider_turn(
    harness_workspace,
    monkeypatch,
):
    """A graceful worker exit leaves no busy session or durable answer holder."""
    from asgiref.sync import sync_to_async

    from apps.harness.providers.base import Delta, ProviderAdapter
    from apps.harness.repositories import (
        HarnessMessageRepository,
        HarnessSessionRepository,
    )

    running, title_running = asyncio.Event(), asyncio.Event()

    class BlockingProvider(ProviderAdapter):
        name = "shutdown-test"

        async def chat_stream(self, model, messages, tools, opts=None):
            running.set()
            await asyncio.Event().wait()
            yield Delta(text="unreachable")

    async def title(**kwargs):
        title_running.set()
        await asyncio.Event().wait()

    service = HarnessService(
        emit=AsyncMock(),
        provider_factory=lambda _: BlockingProvider(),
    )
    monkeypatch.setattr(service.metadata, "generate_title", title)
    session = await sync_to_async(HarnessSessionRepository.create)(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        created_by_id=harness_workspace.created_by_id,
        title="Shutdown test",
        model="fake-model",
        agent_name="build",
        mode="build",
    )
    assistant = await service.start_run(
        session,
        "keep running",
        user_id=harness_workspace.created_by_id,
    )
    await asyncio.wait_for(running.wait(), 5)
    await asyncio.wait_for(title_running.wait(), 5)
    context = service.run_registry.get(str(session.id)).context
    turn_id = context["provider_turn_id"]
    await service.shutdown_runs()
    stored_session = await sync_to_async(HarnessSessionRepository.get_by_id)(session.id)
    stored_message = await sync_to_async(HarnessMessageRepository.get_required)(
        assistant.id
    )
    turn = await sync_to_async(service._provider_turn_service.get)(turn_id)
    assert stored_session.status == "idle"
    assert stored_message.finish == "error"
    assert "aborted by user" not in stored_message.error.lower()
    assert "interrupted" in stored_message.error.lower()
    assert not turn.active and turn.holds == 0
    assert service._provider_turn_roots == {}
    assert service.run_registry.get(str(session.id)) is None


async def test_unconfirmed_external_cleanup_retains_durable_answer_snapshot():
    """Stopping the worker cannot assert that an external runner process died."""
    import uuid

    from apps.harness.provider_account_events import (
        notify_account_revoked,
        register_revocation_listener,
    )

    account, revoked = uuid.uuid4(), asyncio.Event()
    watched = register_revocation_listener(
        [str(account)],
        loop=asyncio.get_running_loop(),
        event=revoked,
    )
    service = HarnessService()
    holds = {"external-answer", "title"}
    state = SimpleNamespace(active=True, holds=2, monitor_required=False)

    def finish(turn_id, holder):
        holds.discard(holder)
        state.holds = len(holds)
        state.active = bool(holds)

    service._provider_turns = SimpleNamespace(
        finish_once=Mock(side_effect=finish),
        get=Mock(return_value=state),
        clear_monitor_required=Mock(),
    )
    entered = asyncio.Event()

    async def title():
        entered.set()
        await asyncio.Event().wait()

    task = service._spawn_background(title())
    context = {
        "provider_turn_id": "external-turn",
        "provider_turn_holder": "external-answer",
        "cleanup_confirmed": False,
        "provider_revocation_accounts": watched,
        "provider_revocation_event": revoked,
        "provider_followup_tasks": {"title": task},
    }
    service._provider_turn_roots["external-turn"] = context
    await entered.wait()
    await service.shutdown_runs()
    assert task.cancelled()
    assert holds == {"external-answer"}
    assert state.active and state.holds == 1
    assert not context.get("provider_turn_finished")
    notify_account_revoked(account)
    await asyncio.sleep(0)
    assert not revoked.is_set()
