"""Cancellation after commit must not hide persisted admission identities."""

from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import fields
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from asgiref.sync import sync_to_async

from apps.harness.run_admission import RunAdmission, RunAdmissionPorts
from apps.harness.run_preparation import PreparedRun
from apps.harness.run_state import RunRegistry


class CommitBarrier:
    """A synchronous write committed its result but has not returned it yet."""

    def __init__(self) -> None:
        self.committed = threading.Event()
        self.release = threading.Event()

    def wait_after_commit(self) -> None:
        self.committed.set()
        if not self.release.wait(5):
            raise RuntimeError("Test did not release committed write")


@pytest.fixture
async def admission_fixture():
    registry = RunRegistry()
    session = SimpleNamespace(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        parent_id=None,
        status="idle",
        last_message_at=None,
        updated_at=None,
        skill_ids=[],
        reasoning_effort="",
    )
    rows = []
    hold = {"active": False}
    sidecar = SimpleNamespace(id=uuid.uuid4(), holder_tokens=["answer-holder"])
    owner = SimpleNamespace(
        stop_heartbeat=AsyncMock(),
        begin_close=AsyncMock(),
        finalize_turn=AsyncMock(return_value=True),
    )
    created = {"attempt": False}
    spawned = []

    def create(**values):
        row = SimpleNamespace(id=uuid.uuid4(), **values)
        rows.append(row)
        return row

    def admit(**kwargs):
        hold["active"] = True
        return sidecar

    def finish_once(turn_id, token):
        assert turn_id == sidecar.id
        assert token == "answer-holder"
        hold["active"] = False

    def create_attempt(**kwargs):
        created["attempt"] = True
        return owner

    def mark_status(row, status):
        row.status = status

    def spawn(coro):
        task = asyncio.create_task(coro)
        spawned.append(task)
        return task

    messages = SimpleNamespace(
        create=Mock(side_effect=create),
        count_for_session=Mock(return_value=1),
        list_for_session=Mock(return_value=[]),
        complete=Mock(),
        dismiss_prior_notices=Mock(),
        discard_edit_run=Mock(),
    )
    providers = SimpleNamespace(
        admit=Mock(side_effect=admit),
        finish_once=Mock(side_effect=finish_once),
    )
    run_service = SimpleNamespace(create_attempt=Mock(side_effect=create_attempt))
    dependencies = {field.name: Mock() for field in fields(RunAdmissionPorts)}
    dependencies.update(
        run_registry=registry,
        provider_turn_roots={},
        messages=messages,
        provider_turn_service=providers,
        reserve_run=Mock(return_value=True),
        sessions=SimpleNamespace(
            mark_status=Mock(side_effect=mark_status), clear_summary=Mock()
        ),
        history=SimpleNamespace(build=AsyncMock(return_value=[])),
        run_service_for_engine=lambda: run_service,
        user_abort_requested=Mock(return_value=False),
        spawn_background=spawn,
        monitor_provider_turn=AsyncMock(),
        commit_message_edit=AsyncMock(),
    )
    prepared = PreparedRun(
        session=session,
        prompt="prompt",
        org_id=uuid.uuid4(),
        workspace_id=str(session.workspace_id),
        run_user_id=42,
        scheduled=False,
        account_ids={},
        max_depth=3,
        depth=0,
        edit_message_id=None,
        key=str(session.id),
        engine_id="native",
        inherited_context={},
        skill_bodies=[],
        edit_model="",
        edit_effort="",
        resolved_model="model",
        resolved_provider="provider",
    )
    preparation = SimpleNamespace(prepare=AsyncMock(return_value=prepared))
    ports = RunAdmissionPorts(**dependencies)
    admission = RunAdmission(ports, preparation)
    yield SimpleNamespace(
        registry=registry,
        session=session,
        rows=rows,
        hold=hold,
        sidecar=sidecar,
        owner=owner,
        created=created,
        messages=messages,
        providers=providers,
        run_service=run_service,
        ports=ports,
        prepared=prepared,
        preparation=preparation,
        admission=admission,
    )
    for task in spawned:
        if not task.done():
            task.cancel()
    await asyncio.gather(*spawned, return_exceptions=True)


async def cancel_during_committed_write(admission, session, barrier):
    caller = asyncio.create_task(admission.start(session, "prompt"))
    try:
        assert await asyncio.to_thread(barrier.committed.wait, 5)
        caller.cancel()
        await asyncio.sleep(0)
        assert not caller.done()
        barrier.release.set()
        with pytest.raises(asyncio.CancelledError):
            await caller
    finally:
        barrier.release.set()
        await asyncio.gather(caller, return_exceptions=True)


async def test_sidecar_identity_is_captured_and_hold_released_after_cancellation(
    admission_fixture,
):
    data = admission_fixture
    barrier = CommitBarrier()
    original = data.providers.admit.side_effect

    def write(**kwargs):
        result = original(**kwargs)
        barrier.wait_after_commit()
        return result

    data.providers.admit.side_effect = write
    await cancel_during_committed_write(data.admission, data.session, barrier)
    data.providers.finish_once.assert_called_once_with(data.sidecar.id, "answer-holder")
    assert not data.hold["active"]
    assert data.session.status == "idle"
    assert data.registry.get(str(data.session.id)) is None
    data.messages.complete.assert_called_once()
    assert data.messages.complete.call_args.args[0].role == "assistant"


async def test_external_attempt_identity_is_captured_and_finalized_after_cancellation(
    admission_fixture,
):
    from dataclasses import replace

    data = admission_fixture
    barrier = CommitBarrier()
    original = data.run_service.create_attempt.side_effect
    data.preparation.prepare.return_value = replace(data.prepared, engine_id="codex")

    def write(**kwargs):
        result = original(**kwargs)
        barrier.wait_after_commit()
        return result

    data.run_service.create_attempt.side_effect = write
    await cancel_during_committed_write(data.admission, data.session, barrier)
    assert data.created["attempt"]
    data.owner.stop_heartbeat.assert_awaited_once()
    data.owner.begin_close.assert_awaited_once()
    data.owner.finalize_turn.assert_awaited_once()
    assert not data.hold["active"]
    assert data.session.status == "idle"
    assert data.registry.get(str(data.session.id)) is None


@pytest.mark.parametrize("role", ["user", "assistant"])
async def test_edit_rollback_discards_committed_shell_identity(
    admission_fixture,
    role,
):
    from dataclasses import replace

    data = admission_fixture
    barrier = CommitBarrier()
    edit_id = uuid.uuid4()
    data.preparation.prepare.return_value = replace(
        data.prepared, edit_message_id=edit_id
    )
    data.messages.list_for_session.return_value = [SimpleNamespace(id=edit_id)]
    original = data.messages.create.side_effect

    def write(**kwargs):
        row = original(**kwargs)
        if kwargs["role"] == role:
            barrier.wait_after_commit()
        return row

    data.messages.create.side_effect = write
    await cancel_during_committed_write(data.admission, data.session, barrier)
    expected = [row.id for row in data.rows]
    data.messages.discard_edit_run.assert_called_once_with(
        data.session.id,
        message_ids=expected,
        last_message_at=None,
        updated_at=None,
    )
    assert data.session.status == "idle"
    assert data.registry.get(str(data.session.id)) is None


async def test_committed_edit_is_not_rolled_back_as_an_uncommitted_replacement(
    admission_fixture,
):
    from dataclasses import replace

    data = admission_fixture
    barrier = CommitBarrier()
    edit_id = uuid.uuid4()
    data.preparation.prepare.return_value = replace(
        data.prepared, edit_message_id=edit_id
    )
    data.messages.list_for_session.return_value = [SimpleNamespace(id=edit_id)]

    def commit(*args, **kwargs):
        barrier.wait_after_commit()

    async def edit(*args, **kwargs):
        await sync_to_async(commit)(*args, **kwargs)

    data.admission = RunAdmission(
        replace(data.ports, commit_message_edit=edit),
        data.preparation,
    )
    await cancel_during_committed_write(data.admission, data.session, barrier)
    data.messages.discard_edit_run.assert_not_called()
    assert data.session.status == "idle"
    assert not data.hold["active"]


async def test_commit_fence_preserves_write_failure_as_cancellation_cause():
    barrier = CommitBarrier()
    captured = Mock()

    def write():
        barrier.wait_after_commit()
        raise ValueError("transaction failed")

    caller = asyncio.create_task(RunAdmission._commit(sync_to_async(write)(), captured))
    try:
        assert await asyncio.to_thread(barrier.committed.wait, 5)
        caller.cancel()
        barrier.release.set()
        with pytest.raises(asyncio.CancelledError) as stopped:
            await caller
        assert isinstance(stopped.value.__cause__, ValueError)
        assert str(stopped.value.__cause__) == "transaction failed"
        captured.assert_not_called()
    finally:
        barrier.release.set()
        await asyncio.gather(caller, return_exceptions=True)


async def test_commit_fence_rethrows_underlying_error_without_caller_cancellation():
    failure = ValueError("write failed")
    with pytest.raises(ValueError, match="write failed"):
        await RunAdmission._commit(AsyncMock(side_effect=failure)(), Mock())


async def test_commit_fence_survives_repeated_cancellation_before_capture():
    barrier = CommitBarrier()
    captured = []

    def write():
        barrier.wait_after_commit()
        return "committed-row"

    caller = asyncio.create_task(
        RunAdmission._commit(sync_to_async(write)(), captured.append)
    )
    try:
        assert await asyncio.to_thread(barrier.committed.wait, 5)
        caller.cancel()
        await asyncio.sleep(0)
        caller.cancel()
        await asyncio.sleep(0)
        assert not caller.done()
        barrier.release.set()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert captured == ["committed-row"]
    finally:
        barrier.release.set()
        await asyncio.gather(caller, return_exceptions=True)
