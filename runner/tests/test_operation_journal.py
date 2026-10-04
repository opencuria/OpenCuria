"""Durable runner lifecycle delivery and interruption contracts."""

import asyncio
from unittest.mock import AsyncMock
import pytest
from src.journal import Journal, OperationExecutor


def command():
    return {
        "task_id": "task",
        "operation_id": "task",
        "attempt": 1,
        "target": "workspace",
        "runner_id": "runner",
        "workspace_id": "workspace",
        "env_vars": {"TOKEN": "DO_NOT_STORE"},
        "files": [{"content": "SECRET"}],
    }


def test_reopen_interrupted_never_reexecutes(tmp_path):
    journal = Journal(str(tmp_path))
    assert journal.begin("task:create_workspace", command())
    journal.close()
    journal = Journal(str(tmp_path))
    assert not journal.begin("task:create_workspace", command())
    event, data = journal.pending()[0]
    assert event == "workspace:error"
    assert "intervention" in data["error"]
    assert data["workspace_id"] == "workspace"
    assert b"DO_NOT_STORE" not in (tmp_path / "operations.sqlite3").read_bytes()
    assert b"SECRET" not in (tmp_path / "operations.sqlite3").read_bytes()


def test_terminal_replay_ack_retains_tombstone(tmp_path):
    journal = Journal(str(tmp_path))
    data = command()
    journal.begin("task:stop_workspace", data)
    journal.finish("task", "workspace:stopped", {**data, "credentials_present": False})
    journal.acknowledge({**data, "attempt": 2})
    assert journal.pending()
    journal.acknowledge(data)
    assert journal.pending() == []
    journal.close()
    journal = Journal(str(tmp_path))
    assert not journal.begin("task:stop_workspace", data)
    assert journal.pending("task")[0][1]["credentials_present"] is False
    with pytest.raises(ValueError):
        journal.begin("task:stop_workspace", {**data, "target": "other"})


@pytest.mark.asyncio
async def test_duplicate_and_outage_dont_abort_work(tmp_path):
    class Socket:
        def __init__(self):
            self.handlers = {"/": {}}
            self.emit = AsyncMock(side_effect=ConnectionError("offline"))

        def on(self, event, handler):
            self.handlers["/"][event] = handler

    from src.journal import COMMANDS

    sio = Socket()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def handler(data):
        calls.append(data)
        entered.set()
        await release.wait()
        await sio.emit(
            "workspace:created", {"task_id": "task", "workspace_id": "workspace"}
        )

    sio.handlers["/"] = {event: handler for event in COMMANDS}
    executor = OperationExecutor(sio, Journal(str(tmp_path)))
    wrapped = sio.handlers["/"]["task:create_workspace"]
    first = asyncio.create_task(wrapped(command()))
    await entered.wait()
    await wrapped(command())
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert executor.running
    release.set()
    await asyncio.gather(*executor.running.values())
    assert len(calls) == 1
    assert executor.journal.pending()[0][0] == "workspace:created"
    sio.emit = AsyncMock()
    executor.emit = sio.emit
    await executor.replay()
    assert sio.emit.call_args.args[0] == "operation:result"


def test_journal_exclusive_process_and_terminal_sanitization(tmp_path):
    journal = Journal(str(tmp_path))
    with pytest.raises(BlockingIOError):
        Journal(str(tmp_path))
    journal.begin("task:stop_workspace", command())
    journal.finish("task", "workspace:error", {**command(), "error": "secret-token"})
    assert "secret-token" not in str(journal.pending())
    journal.close()
    reopened = Journal(str(tmp_path))
    assert reopened.pending()
    reopened.close()


def test_checkpoint_survives_restart_only_exact_incarnation(tmp_path):
    journal = Journal(str(tmp_path))
    incarnation = {"domain_uuid": "domain", "disk": {"inode": 1, "device": 2}}
    journal.checkpoint("workspace", incarnation, {"credentials_present": False})
    journal.close()
    reopened = Journal(str(tmp_path))
    assert reopened.proof("workspace", incarnation) == {"credentials_present": False}
    assert (
        reopened.proof("workspace", {**incarnation, "domain_uuid": "replacement"})
        is None
    )
    reopened.close()


@pytest.mark.asyncio
async def test_inspect_running_and_interrupted_are_distinct(tmp_path):
    from src.journal import COMMANDS

    class Socket:
        def __init__(self):
            self.handlers = {"/": {}}
            self.emit = AsyncMock()

        def on(self, event, handler):
            self.handlers["/"][event] = handler

    sio = Socket()
    gate = asyncio.Event()

    async def handler(data):
        await gate.wait()
        await sio.emit("workspace:error", {"error": "SECRET", "task_id": "task"})

    sio.handlers["/"] = {event: handler for event in COMMANDS}
    executor = OperationExecutor(sio, Journal(str(tmp_path)))
    work = asyncio.create_task(sio.handlers["/"]["task:stop_workspace"](command()))
    await asyncio.sleep(0)
    # Lightweight replay renews an executing lease without recovery scans.
    executor.reconciler = AsyncMock(side_effect=AssertionError("heavy recovery"))
    await executor.replay(recover=False)
    executor.reconciler.assert_not_awaited()
    assert any(
        call.args[0] == "operation:heartbeat" for call in executor.emit.await_args_list
    )
    live = await executor.inspect(command())
    assert live["status"] == "running" and not live["execution_finished"]
    gate.set()
    await work
    terminal = await executor.inspect(command())
    assert terminal["execution_finished"] and terminal["outcome_known"]
    assert "SECRET" not in str(terminal)
    assert (await executor.inspect({**command(), "runner_id": "other"}))[
        "status"
    ] == "unknown"
    executor.journal.close()


@pytest.mark.asyncio
async def test_unrecorded_identity_can_inspect_fresh_quiescence_without_execution(
    tmp_path,
):
    from src.journal import COMMANDS

    class Socket:
        def __init__(self):
            self.handlers = {"/": {}}
            self.emit = AsyncMock()

        def on(self, event, handler):
            self.handlers["/"][event] = handler

    sio = Socket()
    handler = AsyncMock()
    sio.handlers["/"] = {event: handler for event in COMMANDS}
    observer = type(
        "Observer",
        (),
        {
            "inventory_for_runner": AsyncMock(
                return_value={"complete": True, "runtimes": []}
            )
        },
    )()
    executor = OperationExecutor(sio, Journal(str(tmp_path)), observer=observer)
    result = await executor.inspect(command())
    assert result["status"] == "unknown"
    assert result["execution_finished"] and result["quiescent"]
    assert not result["outcome_known"]
    handler.assert_not_called()
    executor.journal.close()


@pytest.mark.asyncio
async def test_safe_transient_start_retry_is_bounded_and_terminal_only_after_finish(
    tmp_path, monkeypatch
):
    from src.journal import COMMANDS

    class Socket:
        def __init__(self):
            self.handlers = {"/": {}}
            self.emit = AsyncMock()

        def on(self, event, handler):
            self.handlers["/"][event] = handler

    sio = Socket()
    calls = []

    async def handler(data):
        calls.append(data)
        await sio.emit("workspace:error", {"error": "transient secret"})
        assert not executor.journal.pending()  # effect not finished yet

    sio.handlers["/"] = {event: handler for event in COMMANDS}
    observer = type(
        "Observer",
        (),
        {
            "workspace_incarnation": AsyncMock(
                return_value=({"resource_id": "disk"}, "exited")
            )
        },
    )()
    executor = OperationExecutor(sio, Journal(str(tmp_path)), observer=observer)
    monkeypatch.setattr("src.journal.asyncio.sleep", AsyncMock())
    await sio.handlers["/"]["task:resume_workspace"](command())
    assert len(calls) == 3
    result = executor.journal.pending()[0][1]
    assert result["execution_finished"] and result["outcome_known"]
    assert "secret" not in str(result)
    assert not executor.running
    executor.journal.close()


@pytest.mark.asyncio
async def test_finished_capture_enospc_with_exact_scrub_checkpoint_stays_known(
    tmp_path,
):
    from src.journal import COMMANDS

    class Socket:
        def __init__(self):
            self.handlers = {"/": {}}
            self.emit = AsyncMock()

        def on(self, event, handler):
            self.handlers["/"][event] = handler

    sio = Socket()

    async def handler(data):
        await sio.emit(
            "image_artifact:failed", {"error": "No space left on device /secret"}
        )

    sio.handlers["/"] = {event: handler for event in COMMANDS}
    incarnation = {"resource_id": "same-disk"}
    observer = type(
        "Observer",
        (),
        {"workspace_incarnation": AsyncMock(return_value=(incarnation, "exited"))},
    )()
    journal = Journal(str(tmp_path))
    journal.checkpoint(
        "workspace",
        incarnation,
        {"credentials_present": False, "event": "workspace:stopped", "state": "exited"},
    )
    journal.close()
    executor = OperationExecutor(sio, Journal(str(tmp_path)), observer=observer)
    await sio.handlers["/"]["task:create_image_artifact"](command())
    result = executor.journal.pending()[0][1]
    assert result["execution_finished"] and result["outcome_known"]
    assert result["diagnostic_code"] == "insufficient_storage"
    assert "/secret" not in str(result)
    executor.journal.close()


@pytest.mark.asyncio
async def test_sealed_unrecorded_identity_cannot_execute_delayed_recipe(tmp_path):
    from src.journal import COMMANDS

    class Socket:
        def __init__(self):
            self.handlers = {"/": {}}
            self.emit = AsyncMock()

        def on(self, event, handler):
            self.handlers["/"][event] = handler

    sio = Socket()
    handler = AsyncMock()
    sio.handlers["/"] = {event: handler for event in COMMANDS}
    observer = type(
        "Observer",
        (),
        {
            "inventory_for_runner": AsyncMock(
                return_value={"complete": True, "runtimes": []}
            )
        },
    )()
    executor = OperationExecutor(sio, Journal(str(tmp_path)), observer=observer)
    sealed = await executor.seal(command())
    assert sealed["status"] == "terminal" and sealed["execution_finished"]
    with pytest.raises(ValueError, match="collision"):
        await sio.handlers["/"]["task:create_workspace"](command())
    handler.assert_not_called()
    executor.journal.close()


@pytest.mark.asyncio
async def test_effect_then_generic_exception_is_unknown(tmp_path):
    from src.journal import COMMANDS

    class Socket:
        def __init__(self):
            self.handlers = {"/": {event: handler for event in COMMANDS}}
            self.emit = AsyncMock()

        def on(self, event, handler):
            self.handlers["/"][event] = handler

    async def handler(data):
        (tmp_path / "effect").write_text("effect already happened")
        raise RuntimeError("after effect")

    socket = Socket()
    executor = OperationExecutor(socket, Journal(str(tmp_path / "journal")))
    await socket.handlers["/"]["task:create_workspace"](command())
    event, result = executor.journal.pending()[0]
    assert event == "workspace:error"
    assert result["execution_finished"] is True
    assert result["outcome_known"] is False
    assert "intervention" in result["error"]
    await socket.handlers["/"]["task:create_workspace"](command())
    assert executor.journal.pending()[0][1] == result


def test_running_readiness_allows_normal_writes_but_scrub_requires_exact_stat(tmp_path):
    journal = Journal(str(tmp_path))
    original = {
        "metadata": {"disks": [{"inode": 1, "mtime_ns": 1, "ctime_ns": 1, "size": 10}]}
    }
    changed = {
        "metadata": {"disks": [{"inode": 1, "mtime_ns": 2, "ctime_ns": 2, "size": 20}]}
    }
    journal.checkpoint("ws", original, {"state": "running", "initialized": True})
    assert journal.proof("ws", changed)
    journal.checkpoint("ws", original, {"state": "exited", "event": "scrubbed"})
    assert journal.proof("ws", changed) is None
    journal.close()


def test_observed_unexpected_boot_permanently_revokes_scrub(tmp_path):
    journal = Journal(str(tmp_path))
    incarnation = {"disk": "unchanged"}
    journal.checkpoint("ws", incarnation, {"state": "exited", "event": "scrubbed"})
    journal.observe_workspace("ws", "running")
    journal.observe_workspace("ws", "exited")
    assert journal.proof("ws", incarnation) is None
    journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("credentials_present", [False, True, None])
async def test_resume_result_checkpoint_matches_terminal_envelope(
    tmp_path, credentials_present
):
    """Persist the same acknowledged presence for replay and incarnation proof."""
    from src.journal import COMMANDS

    class Socket:
        def __init__(self):
            self.handlers = {"/": {}}
            self.emit = AsyncMock()

        def on(self, event, handler):
            self.handlers["/"][event] = handler

    sio = Socket()

    async def handler(data):
        await sio.emit(
            "workspace:resumed",
            {
                "task_id": data["task_id"],
                "workspace_id": data["workspace_id"],
                "credentials_present": credentials_present,
            },
        )

    sio.handlers["/"] = {event: handler for event in COMMANDS}
    incarnation = {"resource_id": "disk"}
    observer = type(
        "Observer",
        (),
        {"workspace_incarnation": AsyncMock(return_value=(incarnation, "running"))},
    )()
    journal = Journal(str(tmp_path))
    executor = OperationExecutor(sio, journal, observer=observer)
    await sio.handlers["/"]["task:resume_workspace"](command())
    event, result = journal.pending()[0]
    assert event == "workspace:resumed"
    assert result["credentials_present"] is credentials_present
    proof = journal.proof("workspace", incarnation)
    assert proof["credentials_present"] is credentials_present
    assert proof["event"] == event
    assert proof["operation_id"] == result["operation_id"]
    assert proof["state"] == "running"
    emitted = [
        c.args[1]
        for c in executor.emit.call_args_list
        if c.args[0] == "operation:result"
    ]
    assert emitted[-1]["data"]["credentials_present"] is credentials_present
    journal.close()
