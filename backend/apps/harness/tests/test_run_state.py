"""Local run ownership remains coherent through admission and cancellation."""

from __future__ import annotations

import asyncio

import pytest

from apps.harness.run_state import RunPhase, RunRegistry


async def test_admission_transfers_single_owner_and_resets_next_turn() -> None:
    registry = RunRegistry()
    owner = asyncio.current_task()
    registry.begin_admission("session", owner)
    record = registry.get("session")
    assert record.admission is owner
    assert record.phase == RunPhase.ADMITTING
    task = asyncio.create_task(asyncio.sleep(0))
    registry.start_execution("session", task)
    assert record.task is task
    assert record.admission is None
    assert registry.get_admission("session") is None
    assert record.phase == RunPhase.RUNNING
    record.begin_finalization()
    assert registry.finalizing("session")
    await task
    registry.release_task("session")
    registry.begin_admission("session", owner)
    assert registry.get("session").phase == RunPhase.ADMITTING
    assert not registry.get("session").execution_started


async def test_second_live_execution_owner_is_rejected() -> None:
    registry = RunRegistry()
    first = asyncio.create_task(asyncio.sleep(100))
    second = asyncio.create_task(asyncio.sleep(100))
    try:
        registry.start_execution("session", first)
        with pytest.raises(RuntimeError, match="live execution owner"):
            registry.start_execution("session", second)
        assert registry.get("session").task is first
    finally:
        first.cancel()
        second.cancel()
        await asyncio.gather(first, second, return_exceptions=True)


def test_cleanup_waits_for_all_record_obligations() -> None:
    registry = RunRegistry()
    registry.link_parent("child", "parent")
    context = {}
    registry.attach_context("child", context)
    registry.request_abort("child")
    assert len(registry.records) == 1
    record = registry.get("child")
    assert record.context is context
    assert registry.get_context("child") is context
    assert record.parent_id == "parent"
    assert record.abort_requested
    registry.release_context("child")
    registry.unlink_parent("child")
    assert registry.get("child") is record
    registry.clear_abort("child")
    assert registry.get("child") is None


def test_registries_are_instance_local() -> None:
    first, second = RunRegistry(), RunRegistry()
    first.request_abort("session")
    first.link_parent("session", None)
    assert first.get_parent("session", "fallback") is None
    assert second.get_task("session") is None
    assert second.get_admission("session") is None
    assert second.get_context("session") == {}
    assert second.get_parent("session", "fallback") == "fallback"
    assert not second.is_aborted("session")
    assert not second.records


async def test_preparation_is_owned_before_first_await_and_rejects_duplicate() -> None:
    from dataclasses import fields
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from apps.harness.run_admission import RunAdmission, RunAdmissionPorts
    from common.exceptions import ConflictError

    registry = RunRegistry()
    dependencies = {field.name: Mock() for field in fields(RunAdmissionPorts)}
    dependencies["run_registry"] = registry
    entered, release = asyncio.Event(), asyncio.Event()

    async def prepare(*args, **kwargs):
        entered.set()
        await release.wait()
        return object()

    admission = RunAdmission(
        RunAdmissionPorts(**dependencies), SimpleNamespace(prepare=prepare)
    )
    admission._admit = AsyncMock(return_value="assistant")
    session = SimpleNamespace(id="session")
    task = asyncio.create_task(admission.start(session, "first"))
    await entered.wait()
    assert registry.get("session").admission is task
    assert registry.get("session").phase == RunPhase.ADMITTING
    with pytest.raises(ConflictError, match="active run"):
        await admission.start(session, "second")
    release.set()
    assert await task == "assistant"
    assert registry.get("session") is None


async def test_failed_preparation_releases_local_ownership() -> None:
    from dataclasses import fields
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from apps.harness.run_admission import RunAdmission, RunAdmissionPorts

    registry = RunRegistry()
    dependencies = {field.name: Mock() for field in fields(RunAdmissionPorts)}
    dependencies["run_registry"] = registry
    admission = RunAdmission(
        RunAdmissionPorts(**dependencies),
        SimpleNamespace(prepare=AsyncMock(side_effect=ValueError("invalid skills"))),
    )
    with pytest.raises(ValueError, match="invalid skills"):
        await admission.start(SimpleNamespace(id="session"), "first")
    assert registry.get("session") is None


async def test_done_execution_owner_fences_readmission_until_cleanup_releases() -> None:
    """A cancelled pre-entry task still owns its shell before its callback runs."""
    from dataclasses import fields
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from apps.harness.run_admission import RunAdmission, RunAdmissionPorts
    from common.exceptions import ConflictError

    registry = RunRegistry()
    task = asyncio.create_task(asyncio.sleep(100))
    registry.start_execution("session", task)
    registry.attach_context("session", {"message_id": "old-shell"})
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    record = registry.get("session")
    assert task.done()
    assert record.is_running
    dependencies = {field.name: Mock() for field in fields(RunAdmissionPorts)}
    dependencies["run_registry"] = registry
    preparation = SimpleNamespace(prepare=AsyncMock())
    admission = RunAdmission(RunAdmissionPorts(**dependencies), preparation)
    with pytest.raises(ConflictError, match="active run"):
        await admission.start(SimpleNamespace(id="session"), "replacement")
    preparation.prepare.assert_not_awaited()
    record.begin_finalization()
    registry.release_task("session")
    assert record.is_running  # Durable shell cleanup is still outstanding.
    registry.release_context("session")
    assert registry.get("session") is None


async def test_cancelled_reservation_joins_commit_and_releases_persistent_busy() -> (
    None
):
    """Cancelling the coroutine cannot cancel a reservation's database thread."""
    import threading
    import uuid
    from dataclasses import fields
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from apps.harness.run_admission import RunAdmission, RunAdmissionPorts
    from apps.harness.run_preparation import PreparedRun

    registry = RunRegistry()
    session = SimpleNamespace(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        status="idle",
        last_message_at=None,
        updated_at=None,
    )
    durable_status = {"value": "idle"}
    committed, release = threading.Event(), threading.Event()

    def reserve(*args, **kwargs):
        durable_status["value"] = "busy"
        committed.set()
        if not release.wait(5):
            raise RuntimeError("Test did not release the reservation thread")
        return True

    def mark_status(row, status):
        durable_status["value"] = str(status)
        row.status = status

    prepared = PreparedRun(
        session=session,
        prompt="prompt",
        org_id=uuid.uuid4(),
        workspace_id=str(session.workspace_id),
        run_user_id=None,
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
    dependencies = {field.name: Mock() for field in fields(RunAdmissionPorts)}
    dependencies.update(
        run_registry=registry,
        reserve_run=reserve,
        sessions=SimpleNamespace(mark_status=mark_status),
    )
    admission = RunAdmission(
        RunAdmissionPorts(**dependencies),
        SimpleNamespace(prepare=AsyncMock(return_value=prepared)),
    )
    caller = asyncio.create_task(admission.start(session, "prompt"))
    try:
        assert await asyncio.to_thread(committed.wait, 5)
        caller.cancel()
        await asyncio.sleep(0)
        assert not caller.done()
        assert registry.get(str(session.id)) is not None
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert durable_status["value"] == "idle"
        assert registry.get(str(session.id)) is None
    finally:
        release.set()
        await asyncio.gather(caller, return_exceptions=True)
