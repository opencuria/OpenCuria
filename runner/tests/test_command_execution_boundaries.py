"""Application ports expose durable execution without intercepting a transport."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from src.application import RunnerApplication
from src.commands import CommandResult, LifecycleCommands
from src.config import RunnerSettings
from src.journal import Journal, OperationExecutor
from src.service import WorkspaceService
from src.workspace_evidence import WorkspaceEvidence


def command(**updates):
    return {
        "operation_id": "operation",
        "task_id": "operation",
        "attempt": 1,
        "target": "workspace",
        "runner_id": "runner",
        **updates,
    }


async def test_executor_requires_only_commands_storage_and_publisher(tmp_path):
    """No socket, handler table, or implicit emission capture is required."""
    journal = Journal(str(tmp_path))
    commands = SimpleNamespace(
        execute=AsyncMock(
            return_value=CommandResult(
                "workspace:created", {"credentials_present": False}
            )
        )
    )
    publisher = AsyncMock(side_effect=ConnectionError("offline"))
    executor = OperationExecutor(journal, commands, publisher)
    try:
        await executor.execute("task:create_workspace", command())
        assert executor.emit is publisher
        commands.execute.assert_awaited_once_with("task:create_workspace", command())
        assert journal.pending()[0][0] == "workspace:created"
        # Replacing an outbound connection does not alter command execution.
        executor.emit = AsyncMock()
        await executor.execute("task:create_workspace", command())
        assert commands.execute.await_count == 1
        assert executor.emit.await_args.args[0] == "operation:result"
        with pytest.raises(ValueError, match="collision"):
            await executor.execute(
                "task:create_workspace", command(runner_id="foreign")
            )
        assert commands.execute.await_count == 1
    finally:
        await executor.drain()
        journal.close()


async def test_unrecognized_command_and_missing_identity_have_no_effect(tmp_path):
    journal = Journal(str(tmp_path))
    commands = SimpleNamespace(execute=AsyncMock())
    executor = OperationExecutor(journal, commands, AsyncMock())
    try:
        with pytest.raises(ValueError, match="Unsupported"):
            await executor.execute("task:unknown", command())
        assert await executor.execute("task:create_workspace", {}) is None
        commands.execute.assert_not_awaited()
        assert journal.unfinished() == []
    finally:
        journal.close()


async def test_command_progress_failure_does_not_abort_image_effect(tmp_path):
    async def build_image(**kwargs):
        await kwargs["progress_callback"]("still working")
        return {"image_path": "/images/result.qcow2"}

    service = SimpleNamespace(
        images=SimpleNamespace(build_image=AsyncMock(side_effect=build_image))
    )
    journal = Journal(str(tmp_path))
    progress = AsyncMock(side_effect=ConnectionError("offline"))
    commands = LifecycleCommands(service, WorkspaceEvidence(service, journal), progress)
    try:
        outcome = await commands.execute("task:build_image", command())
        assert outcome.event == "image:built"
        assert outcome.data["image_path"] == "/images/result.qcow2"
        progress.assert_awaited_once()
        assert journal.pending() == []  # persistence belongs to the executor
    finally:
        journal.close()


async def test_application_wires_evidence_before_transport_and_releases_writer(
    tmp_path,
):
    settings = RunnerSettings(state_dir=str(tmp_path))
    service = WorkspaceService(SimpleNamespace(), settings)
    application = RunnerApplication(service, settings)
    transport = application.interface
    original_emit = transport._sio.emit
    assert original_emit.__func__.__module__ == "socketio.async_client"
    assert application.operations.commands._evidence.journal is application.journal
    assert service.journal is application.journal
    assert service.lifecycle.checkpoint_hook.__self__.journal is application.journal
    # Keep this test about ownership and lifecycle rather than real connections.
    transport.stop = AsyncMock()
    await application.stop()
    transport.stop.assert_awaited_once()
    reopened = Journal(str(tmp_path))
    reopened.close()
