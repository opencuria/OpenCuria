"""Durable command ownership and independent execution lifetime contracts."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from asgiref.sync import sync_to_async
from django.db import IntegrityError, transaction

from apps.harness.command_models import HarnessCommand
from apps.harness.command_repository import HarnessCommandRepository as Commands
from apps.harness.command_service import (
    HarnessCommandClient,
    HarnessCommandDispatcher,
    RemoteHarnessService,
)
from apps.harness.execution_worker import ExecutionWorker
from apps.harness.harness_service import HarnessService
from apps.harness.tests.test_harness_service import FakeProvider, _session
from common.exceptions import NotFoundError
from common.process_ownership import ExecutionProcessGuard


@pytest.mark.django_db(transaction=True)
def test_command_identity_ownership_and_encryption_are_persistent():
    row = Commands.enqueue("start", {"prompt": "sensitive prompt"})
    assert "sensitive prompt" not in row.payload_encrypted
    assert Commands.decode_payload(row)["prompt"] == "sensitive prompt"
    first_owner, other_owner = uuid.uuid4(), uuid.uuid4()
    claimed = Commands.claim(first_owner)
    assert claimed.id == row.id
    assert claimed.state == HarnessCommand.State.CLAIMED
    assert Commands.claim(other_owner) is None
    assert not Commands.complete(row.id, other_owner, {"ok": True})
    assert Commands.complete(
        row.id, first_owner, {"ok": True, "value": "private result"}
    )
    result = Commands.get(row.id)
    assert result.payload_encrypted == ""
    assert "private result" not in result.result_encrypted
    assert Commands.decode_result(result) == {"ok": True, "value": "private result"}
    assert not Commands.complete(row.id, first_owner, {"ok": False})
    with transaction.atomic(), pytest.raises(IntegrityError):
        HarnessCommand.objects.create(
            kind="start", state="claimed", payload_encrypted=""
        )


@pytest.mark.django_db(transaction=True)
def test_crashed_claims_are_settled_without_replaying_effects():
    claimed = Commands.enqueue("start", {"prompt": "one"})
    owner = uuid.uuid4()
    Commands.claim(owner)
    queued = Commands.enqueue("abort", {"session_id": str(uuid.uuid4())})
    assert Commands.interrupt_claimed() == 1
    assert Commands.interrupt_claimed() == 0
    assert (
        Commands.decode_result(Commands.get(claimed.id))["error"]["code"]
        == "execution_interrupted"
    )
    assert Commands.claim(uuid.uuid4()).id == queued.id


class HeldProvider(FakeProvider):
    def __init__(self):
        super().__init__([])
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def chat_stream(self, *args, **kwargs):
        self.entered.set()
        await self.release.wait()
        async for delta in super().chat_stream(*args, **kwargs):
            yield delta


def worker_for(service, tmp_path):
    if isinstance(service, SimpleNamespace):
        service.shutdown_runs = AsyncMock()
        service.request_stop = AsyncMock()

    async def scheduler_start(*, before_dispatch):
        await before_dispatch()

    return ExecutionWorker(
        service,
        guard=ExecutionProcessGuard(tmp_path / "execution.lock"),
        scheduler=SimpleNamespace(
            start=AsyncMock(side_effect=scheduler_start), stop=AsyncMock()
        ),
        notifications=SimpleNamespace(start=AsyncMock(), stop=AsyncMock()),
        recovery=SimpleNamespace(start=AsyncMock(), stop=AsyncMock()),
        start_transport=AsyncMock(),
        stop_transport=AsyncMock(),
        interval=0.001,
    )


@pytest.mark.django_db(transaction=True)
async def test_api_waiter_can_disappear_without_cancelling_native_run(
    harness_workspace, tmp_path
):
    provider = HeldProvider()
    service = HarnessService(provider_factory=lambda _org: provider, emit=AsyncMock())
    worker = worker_for(service, tmp_path)
    session = await sync_to_async(_session)(harness_workspace)
    api = RemoteHarnessService(
        HarnessService(), client=HarnessCommandClient(interval=0.001)
    )
    first_api = asyncio.create_task(
        api.start_run(session, "hello", organization_id=session.organization_id)
    )
    try:

        async def queued():
            while not await sync_to_async(HarnessCommand.objects.exists)():
                await asyncio.sleep(0.001)

        await asyncio.wait_for(queued(), 3)
        first_api.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_api
        # An API process disappeared after committing intent. The execution
        # owner can still admit it and keep the native task running.
        await worker.start()
        await asyncio.wait_for(provider.entered.wait(), 3)
        assert service.is_running(session.id)
        await asyncio.sleep(0.01)
        assert service.is_running(session.id)
        second_api = RemoteHarnessService(
            HarnessService(), client=HarnessCommandClient(interval=0.001)
        )
        stopped = await asyncio.wait_for(second_api.abort_run(session.id), 3)
        assert stopped.status == "idle"
        assert not service.is_running(session.id)
    finally:
        provider.release.set()
        await worker.stop()
        worker.guard.release_failed_start()


@pytest.mark.django_db(transaction=True)
async def test_worker_handles_control_while_admission_waits_and_shutdown_settles(
    tmp_path,
):
    entered = asyncio.Event()
    gate = asyncio.Event()
    session_id = uuid.uuid4()

    async def start_run(*args, **kwargs):
        entered.set()
        await gate.wait()
        return SimpleNamespace(id=uuid.uuid4())

    service = SimpleNamespace(
        conversations=SimpleNamespace(
            get_session=Mock(return_value=SimpleNamespace(id=session_id))
        ),
        start_run=AsyncMock(side_effect=start_run),
        abort_run=AsyncMock(return_value=SimpleNamespace(id=session_id)),
        run_registry=SimpleNamespace(records={}),
    )
    worker = worker_for(service, tmp_path)
    await worker.start()
    start = await sync_to_async(Commands.enqueue)(
        "start", {"session_id": str(session_id), "prompt": "hello"}
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        client = HarnessCommandClient(interval=0.001)
        assert await asyncio.wait_for(
            client.call("abort", {"session_id": str(session_id)}), 3
        ) == str(session_id)
        await asyncio.wait_for(worker.stop(), 3)
        completed = await sync_to_async(Commands.get)(start.id)
        assert completed.state == HarnessCommand.State.COMPLETED
        assert (
            Commands.decode_result(completed)["error"]["code"]
            == "execution_interrupted"
        )
    finally:
        gate.set()
        await worker.stop()
        worker.guard.release_failed_start()


async def test_dispatcher_allowlist_and_session_gate_identity():
    session_id, foreign = uuid.uuid4(), uuid.uuid4()
    service = SimpleNamespace(
        conversations=SimpleNamespace(
            get_session=Mock(return_value=SimpleNamespace(id=session_id))
        ),
        interactions=SimpleNamespace(
            resolve_permission=AsyncMock(
                side_effect=NotFoundError("PermissionRequest", str(foreign))
            )
        ),
    )
    dispatcher = HarnessCommandDispatcher(service)
    with pytest.raises(ValueError, match="Unknown"):
        await dispatcher.execute("_execute_run", {"session_id": str(session_id)})
    with pytest.raises(NotFoundError):
        await dispatcher.execute(
            "permission",
            {
                "session_id": str(session_id),
                "request_id": str(foreign),
                "response": "once",
            },
        )
    assert (
        service.interactions.resolve_permission.await_args.kwargs["request_id"]
        == foreign
    )


async def test_worker_cannot_recover_while_another_execution_owner_lives(tmp_path):
    owner = ExecutionProcessGuard(tmp_path / "execution.lock")
    owner.acquire()
    worker = worker_for(SimpleNamespace(), tmp_path)
    try:
        with pytest.raises(RuntimeError, match="one execution worker"):
            await worker.start()
        worker.recovery.start.assert_not_awaited()
        worker.scheduler.start.assert_not_awaited()
        worker.start_transport.assert_not_awaited()
    finally:
        owner.release_failed_start()


@pytest.mark.django_db(transaction=True)
async def test_preentry_command_cancellation_is_completed_by_supervisor(tmp_path):
    service = SimpleNamespace(run_registry=SimpleNamespace(records={}))
    worker = worker_for(service, tmp_path)
    queued = await sync_to_async(Commands.enqueue)("start", {"prompt": "hello"})
    claimed = await sync_to_async(Commands.claim)(worker.owner_id)
    entered = False

    async def never_entered():
        nonlocal entered
        entered = True

    task = asyncio.create_task(never_entered())
    worker._commands[task] = claimed
    await worker.stop()
    result = await sync_to_async(Commands.get)(queued.id)
    assert not entered
    assert result.state == HarnessCommand.State.COMPLETED
    assert Commands.decode_result(result)["error"]["code"] == "execution_interrupted"


@pytest.mark.django_db(transaction=True)
async def test_result_storage_failure_retries_reply_without_repeating_effect(tmp_path):
    from django.db import OperationalError

    calls = 0

    class Storage(Commands):
        @staticmethod
        def complete(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OperationalError("temporary storage outage")
            return Commands.complete(*args)

    service = SimpleNamespace(run_registry=SimpleNamespace(records={}))
    worker = worker_for(service, tmp_path)
    worker.repository = Storage
    worker.dispatcher.execute = AsyncMock(return_value="already performed")
    queued = await sync_to_async(Commands.enqueue)("start", {"prompt": "hello"})
    claimed = await sync_to_async(Commands.claim)(worker.owner_id)
    await worker._execute(claimed)
    worker.dispatcher.execute.assert_awaited_once()
    result = await sync_to_async(Commands.get)(queued.id)
    assert Commands.decode_result(result) == {"ok": True, "value": "already performed"}
    assert calls == 2


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("control_kind", [None, "abort", "delete"])
async def test_shutdown_during_completion_backoff_preserves_success(
    tmp_path, monkeypatch, control_kind
):
    """A cancelled reply waiter cannot turn already performed effects into failure."""
    from django.db import OperationalError

    calls = 0
    backoff_entered = asyncio.Event()
    retry_storage = asyncio.Event()
    original_sleep = asyncio.sleep
    interval = 0.1234567

    async def controlled_sleep(delay):
        if delay == interval:
            backoff_entered.set()
            await retry_storage.wait()
        else:
            await original_sleep(delay)

    class Storage(Commands):
        @staticmethod
        def complete(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OperationalError("temporary storage outage")
            return Commands.complete(*args)

    worker = worker_for(SimpleNamespace(), tmp_path)
    worker.repository = Storage
    worker.interval = interval
    worker.dispatcher.execute = AsyncMock(return_value="effect already succeeded")
    session_id = uuid.uuid4()
    queued = await sync_to_async(Commands.enqueue)(
        "start", {"session_id": str(session_id), "prompt": "hello"}
    )
    claimed = await sync_to_async(Commands.claim)(worker.owner_id)
    monkeypatch.setattr("apps.harness.execution_worker.asyncio.sleep", controlled_sleep)
    command = asyncio.create_task(worker._execute(claimed))
    worker._commands[command] = claimed
    try:
        await asyncio.wait_for(backoff_entered.wait(), 3)
        if control_kind is None:
            stopping = asyncio.create_task(worker.stop())
        else:
            await sync_to_async(Commands.enqueue)(
                control_kind, {"session_id": str(session_id)}
            )
            claimed_control = await sync_to_async(Commands.claim)(
                worker.owner_id, allowed_kinds={control_kind}
            )
            stopping = asyncio.create_task(worker._execute(claimed_control))
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(stopping), 0.02)
        # A second stop/caller cancellation must still retain the same outcome.
        command.cancel()
        assert not command.done()
        assert (await sync_to_async(Commands.get)(queued.id)).state == "claimed"
        retry_storage.set()
        await asyncio.wait_for(stopping, 3)
        result = await sync_to_async(Commands.get)(queued.id)
        assert Commands.decode_result(result) == {
            "ok": True,
            "value": "effect already succeeded",
        }
        assert worker.dispatcher.execute.await_count == (
            1 if control_kind is None else 2
        )
        assert calls == (2 if control_kind is None else 3)
    finally:
        retry_storage.set()
        await asyncio.gather(command, return_exceptions=True)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("kind", ["abort", "delete"])
def test_prioritized_control_fences_only_earlier_queued_admissions(kind):
    session_id, other_session = uuid.uuid4(), uuid.uuid4()
    running = Commands.enqueue("start", {"session_id": str(session_id)})
    owner = uuid.uuid4()
    assert Commands.claim(owner).id == running.id
    earlier = [
        Commands.enqueue(name, {"session_id": str(session_id)})
        for name in ("start", "edit")
    ]
    unrelated = Commands.enqueue("start", {"session_id": str(other_session)})
    control = Commands.enqueue(kind, {"session_id": str(session_id)})
    later = Commands.enqueue("start", {"session_id": str(session_id)})
    assert Commands.claim(owner, allowed_kinds={kind}).id == control.id
    for row in earlier:
        result = Commands.get(row.id)
        assert result.state == "completed"
        assert Commands.decode_result(result)["error"]["code"] == "execution_superseded"
    assert Commands.get(running.id).state == "claimed"
    assert Commands.get(unrelated.id).state == "queued"
    assert Commands.get(later.id).state == "queued"
    assert Commands.claim(owner, allowed_kinds=set()) is None
    assert Commands.claim(owner, allowed_kinds={"start", "edit"}).id == unrelated.id


@pytest.mark.django_db(transaction=True)
async def test_blocked_admissions_are_bounded_and_cannot_starve_control(tmp_path):
    """Backpressure stays in durable storage while a reserved control lane drains."""
    admissions_ready, control_ready = asyncio.Event(), asyncio.Event()
    release_admissions, release_controls = asyncio.Event(), asyncio.Event()
    admissions = 0
    controls = 0
    max_admissions = 0
    max_controls = 0
    control_completions = []

    async def dispatch(kind, payload):
        nonlocal admissions, controls, max_admissions, max_controls
        if kind in {"start", "edit"}:
            admissions += 1
            max_admissions = max(max_admissions, admissions)
            if admissions == 2:
                admissions_ready.set()
            try:
                await release_admissions.wait()
            finally:
                admissions -= 1
        else:
            controls += 1
            max_controls = max(max_controls, controls)
            control_ready.set()
            try:
                await release_controls.wait()
                control_completions.append(kind)
            finally:
                controls -= 1
        return "completed"

    worker = worker_for(SimpleNamespace(), tmp_path)
    worker.max_admissions = 2
    worker.max_controls = 1
    worker.dispatcher.execute = AsyncMock(side_effect=dispatch)
    for kind in ["start"] * 8 + ["edit"] * 2 + ["abort", "permission", "question"]:
        await sync_to_async(Commands.enqueue)(kind, {"session_id": str(uuid.uuid4())})

    async def pending_admissions():
        return await sync_to_async(
            HarnessCommand.objects.filter(
                kind__in={"start", "edit"},
                state="queued",
            ).count
        )()

    async def wait_for(predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(0.001)

        await asyncio.wait_for(wait(), 3)

    await worker.start()
    try:
        await asyncio.wait_for(admissions_ready.wait(), 3)
        await asyncio.wait_for(control_ready.wait(), 3)
        assert await pending_admissions() == 8
        assert len(worker._commands) == 3
        assert (
            await sync_to_async(
                HarnessCommand.objects.filter(
                    kind__in={"permission", "question"},
                    state="queued",
                ).count
            )()
        ) == 2
        release_controls.set()
        await wait_for(lambda: len(control_completions) == 3)
        assert await pending_admissions() == 8
        assert max_admissions == 2 and max_controls == 1
        release_admissions.set()
        await wait_for(lambda: worker.dispatcher.execute.await_count == 13)
    finally:
        release_admissions.set()
        release_controls.set()
        await worker.stop()
        worker.guard.release_failed_start()


@pytest.mark.parametrize("admissions,controls", [(0, 1), (1, 0), (-1, 1)])
def test_execution_capacity_must_be_positive(admissions, controls):
    with pytest.raises(ValueError, match="positive"):
        ExecutionWorker(
            SimpleNamespace(),
            scheduler=Mock(),
            notifications=Mock(),
            recovery=Mock(),
            max_admissions=admissions,
            max_controls=controls,
        )


@pytest.mark.django_db(transaction=True)
def test_queue_rejects_unknown_kinds_and_disagreeing_session_identity():
    session_id = uuid.uuid4()
    with pytest.raises(ValueError, match="Unknown"):
        Commands.enqueue("execute_arbitrary_method", {})
    with pytest.raises(ValueError, match="session identity"):
        Commands.enqueue("start", {"session_id": str(uuid.uuid4())}, session_id)
    row = Commands.enqueue("start", {"session_id": str(session_id).upper()}, session_id)
    assert row.session_id == session_id
    assert HarnessCommand.objects.count() == 1


@pytest.mark.django_db(transaction=True)
async def test_repeated_poller_cancellation_cannot_abandon_committing_claim(tmp_path):
    import threading

    entered, release = threading.Event(), threading.Event()

    class Storage(Commands):
        @classmethod
        def claim(cls, *args, **kwargs):
            entered.set()
            assert release.wait(3)
            return super().claim(*args, **kwargs)

    worker = worker_for(SimpleNamespace(), tmp_path)
    worker.repository = Storage
    worker.dispatcher.execute = AsyncMock(return_value="finished")
    queued = await sync_to_async(Commands.enqueue)("start", {"prompt": "hello"})
    await worker.start()
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        worker._poller.cancel()
        await asyncio.sleep(0)
        worker._poller.cancel()
        release.set()
        await asyncio.wait_for(worker.stop(), 3)
        assert (await sync_to_async(Commands.get)(queued.id)).state == "completed"
    finally:
        release.set()
        await worker.stop()
        worker.guard.release_failed_start()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("already_cancelling", [False, True])
@pytest.mark.parametrize("admission_kind", ["start", "edit"])
@pytest.mark.parametrize("control_kind", ["abort", "delete"])
async def test_control_joins_claimed_admission_before_it_can_enter_service(
    tmp_path, admission_kind, control_kind, already_cancelling
):
    """Session rehydration cannot resume an older start after an acknowledged stop."""
    loading, rolling_back = asyncio.Event(), asyncio.Event()
    stop_intent = asyncio.Event()
    finish_load, finish_rollback = asyncio.Event(), asyncio.Event()
    session_id = uuid.uuid4()
    session = SimpleNamespace(id=session_id)
    trace = []

    async def start_run(*args, **kwargs):
        trace.append("later_started")
        return SimpleNamespace(id=uuid.uuid4())

    async def control(*args):
        trace.append("controlled")
        return session

    service = SimpleNamespace(
        conversations=SimpleNamespace(get_session=Mock(return_value=session)),
        start_run=AsyncMock(side_effect=start_run),
        edit_user_message=AsyncMock(),
        abort_run=AsyncMock(side_effect=control),
        delete_session=AsyncMock(side_effect=control),
    )
    worker = worker_for(service, tmp_path)

    async def request_stop(_):
        trace.append("stop_intent")
        stop_intent.set()

    service.request_stop.side_effect = request_stop
    original_dispatch = worker.dispatcher.execute

    async def delayed_session_load(kind, payload):
        if kind == admission_kind and payload.get("prompt") == "older":
            loading.set()
            try:
                await finish_load.wait()
            finally:
                trace.append("rollback")
                rolling_back.set()
                await finish_rollback.wait()
        return await original_dispatch(kind, payload)

    worker.dispatcher.execute = AsyncMock(side_effect=delayed_session_load)
    older = await sync_to_async(Commands.enqueue)(
        admission_kind,
        {
            "session_id": str(session_id),
            "prompt": "older",
        },
    )
    claimed = await sync_to_async(Commands.claim)(worker.owner_id)
    worker._launch_command(claimed)
    older_task = next(iter(worker._commands))
    await asyncio.wait_for(loading.wait(), 3)
    if already_cancelling:
        older_task.cancel()
        await asyncio.wait_for(rolling_back.wait(), 3)
    stop = await sync_to_async(Commands.enqueue)(
        control_kind, {"session_id": str(session_id)}
    )
    claimed_stop = await sync_to_async(Commands.claim)(
        worker.owner_id, allowed_kinds={control_kind}
    )
    later = await sync_to_async(Commands.enqueue)(
        "start",
        {
            "session_id": str(session_id),
            "prompt": "later",
        },
    )
    assert (
        await sync_to_async(Commands.claim)(
            worker.owner_id, allowed_kinds={"start", "edit"}
        )
        is None
    )
    worker._launch_command(claimed_stop)
    control_task = next(
        task for task, row in worker._commands.items() if row.id == stop.id
    )
    try:
        await asyncio.wait_for(stop_intent.wait(), 3)
        await asyncio.wait_for(rolling_back.wait(), 3)
        before_control = (
            ["rollback", "stop_intent"]
            if already_cancelling
            else ["stop_intent", "rollback"]
        )
        assert trace == before_control
        assert older_task.cancelling() == 1
        service.abort_run.assert_not_awaited()
        service.delete_session.assert_not_awaited()
        # It stays queued while cleanup holds the root's explicit stop intent.
        assert (
            await sync_to_async(Commands.claim)(
                worker.owner_id, allowed_kinds={"start"}
            )
            is None
        )
        finish_rollback.set()
        await asyncio.wait_for(control_task, 3)
        assert trace == [*before_control, "controlled"]
        assert service.start_run.await_count == 0
        service.edit_user_message.assert_not_awaited()
        result = await sync_to_async(Commands.get)(older.id)
        assert (
            Commands.decode_result(result)["error"]["code"] == "execution_interrupted"
        )
        later_claim = await sync_to_async(Commands.claim)(
            worker.owner_id, allowed_kinds={"start"}
        )
        assert later_claim.id == later.id
        await worker._execute(later_claim)
        service.start_run.assert_awaited_once()
        assert trace[-1] == "later_started"
    finally:
        finish_load.set()
        finish_rollback.set()
        await worker.stop()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("control_kind", ["abort", "delete"])
async def test_control_settles_a_claimed_admission_cancelled_before_entry(
    tmp_path, control_kind
):
    session_id = uuid.uuid4()
    worker = worker_for(SimpleNamespace(), tmp_path)
    worker.dispatcher.execute = AsyncMock(return_value="controlled")
    older = await sync_to_async(Commands.enqueue)(
        "start", {"session_id": str(session_id)}
    )
    claimed = await sync_to_async(Commands.claim)(worker.owner_id)
    control = await sync_to_async(Commands.enqueue)(
        control_kind, {"session_id": str(session_id)}
    )
    claimed_control = await sync_to_async(Commands.claim)(
        worker.owner_id, allowed_kinds={control_kind}
    )
    worker._launch_command(claimed)
    # Await inline so the control acts before the newly created admission task
    # has had its first event-loop turn.
    await worker._execute(claimed_control)
    worker.dispatcher.execute.assert_awaited_once_with(
        control_kind, {"session_id": str(session_id)}
    )
    worker.service.request_stop.assert_awaited_once_with(session_id)
    result = await sync_to_async(Commands.get)(older.id)
    assert Commands.decode_result(result)["error"]["code"] == "execution_interrupted"
    assert (await sync_to_async(Commands.get)(control.id)).state == "completed"


@pytest.mark.django_db(transaction=True)
def test_later_start_waits_for_queued_and_claimed_stop_without_blocking_other_sessions():
    session_id = uuid.uuid4()
    control = Commands.enqueue("abort", {"session_id": str(session_id)})
    later = Commands.enqueue("start", {"session_id": str(session_id)})
    unrelated = Commands.enqueue("start", {"session_id": str(uuid.uuid4())})
    owner = uuid.uuid4()
    assert Commands.claim(owner, allowed_kinds={"start"}).id == unrelated.id
    assert Commands.claim(owner, allowed_kinds={"start"}) is None
    assert Commands.claim(owner, allowed_kinds={"abort"}).id == control.id
    assert Commands.claim(owner, allowed_kinds={"start"}) is None
    Commands.complete(control.id, owner, {"ok": True})
    assert Commands.claim(owner, allowed_kinds={"start"}).id == later.id


@pytest.mark.django_db(transaction=True)
async def test_control_marks_registered_native_admission_as_explicit_user_stop(
    harness_workspace, tmp_path
):
    """Cancelling an admitted reply must retain the user's abort provenance."""
    from apps.harness.models import HarnessMessage

    busy = asyncio.Event()
    release_busy = asyncio.Event()
    provider = HeldProvider()

    async def emit(event, payload):
        if event == "harness.session_status" and payload.get("status") == "busy":
            busy.set()
            await release_busy.wait()

    service = HarnessService(provider_factory=lambda _: provider, emit=emit)
    session = await sync_to_async(_session)(harness_workspace)
    worker = worker_for(service, tmp_path)
    await worker.start()
    try:
        await sync_to_async(Commands.enqueue)(
            "start",
            {
                "session_id": str(session.id),
                "prompt": "hello",
                "organization_id": str(session.organization_id),
            },
        )
        await asyncio.wait_for(busy.wait(), 3)
        await asyncio.wait_for(provider.entered.wait(), 3)
        client = HarnessCommandClient(interval=0.001)
        await asyncio.wait_for(client.call("abort", {"session_id": session.id}), 3)
        assistant = await sync_to_async(HarnessMessage.objects.get)(
            session_id=session.id,
            role="assistant",
        )
        assert assistant.finish == "aborted"
        assert assistant.error == "aborted by user"
        assert not service.is_running(session.id)
    finally:
        release_busy.set()
        provider.release.set()
        await worker.stop()
        worker.guard.release_failed_start()
