"""Lifecycle admission and durable publication under delayed or failed storage."""

import asyncio
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from src.config import RunnerSettings
from src.journal import COMMANDS, Journal, OperationExecutor, _disk_helper_active
from src.runtime.inventory import RuntimeInventory, StorageResource
from src.service import WorkspaceService


def command(operation_id: str = "first", target: str = "workspace") -> dict:
    """Return an exact lifecycle identity with independent resource selection."""
    return {
        "task_id": operation_id,
        "operation_id": operation_id,
        "attempt": 1,
        "target": target,
        "runner_id": "runner",
        "workspace_id": target,
    }


class Socket:
    """Minimal Socket.IO event surface preserving executor emission interception."""

    def __init__(self, handler) -> None:
        self.handlers = {"/": {event: handler for event in COMMANDS}}
        self.emit = AsyncMock()

    def on(self, event, handler) -> None:
        self.handlers["/"][event] = handler


@pytest.mark.asyncio
async def test_repeated_cancellation_drains_sqlite_before_releasing_io_queue(
    tmp_path, monkeypatch
):
    journal = Journal(str(tmp_path))
    entered = threading.Event()
    release = threading.Event()
    checkpoint = journal.checkpoint

    def delayed_checkpoint(*args):
        entered.set()
        assert release.wait(5)
        checkpoint(*args)

    monkeypatch.setattr(journal, "checkpoint", delayed_checkpoint)
    writing = asyncio.create_task(
        journal.call("checkpoint", "workspace", {"disk": "one"}, {"clean": True})
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        writing.cancel()
        await asyncio.sleep(0)
        writing.cancel()
        reading = asyncio.create_task(
            journal.call("proof", "workspace", {"disk": "one"})
        )
        await asyncio.sleep(0)
        assert not writing.done()
        assert not reading.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await writing
    assert await reading == {"clean": True}
    journal.close()


@pytest.mark.asyncio
async def test_cancelled_delivery_during_fsync_still_executes_once_and_drains(
    tmp_path, monkeypatch
):
    journal = Journal(str(tmp_path))
    entered = threading.Event()
    release = threading.Event()
    begin = journal.begin
    calls = []

    def delayed_begin(*args):
        entered.set()
        assert release.wait(5)
        return begin(*args)

    async def handler(data):
        calls.append(data)
        await socket.emit("workspace:created", {})

    socket = Socket(handler)
    executor = OperationExecutor(socket, journal)
    monkeypatch.setattr(journal, "begin", delayed_begin)
    delivery = asyncio.create_task(
        socket.handlers["/"]["task:create_workspace"](command())
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        delivery.cancel()
        with pytest.raises(asyncio.CancelledError):
            await delivery
        # Both heartbeat and the event loop remain usable during blocked fsync.
        await executor.heartbeat()
        assert calls == []
    finally:
        release.set()
    await executor.drain()
    assert len(calls) == 1
    assert journal.pending()[0][0] == "workspace:created"
    assert not executor.running
    journal.close()


@pytest.mark.asyncio
async def test_terminal_inspection_keeps_target_fenced_but_allows_other_targets(
    tmp_path,
):
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def incarnation(workspace_id):
        if workspace_id == "workspace":
            entered.set()
            await release.wait()
        return {"domain": workspace_id}, "running"

    async def handler(data):
        calls.append(data["operation_id"])
        await socket.emit("workspace:created", {})

    socket = Socket(handler)
    journal = Journal(str(tmp_path))
    executor = OperationExecutor(
        socket, journal,
        observer=SimpleNamespace(workspace_incarnation=incarnation),
    )
    wrapped = socket.handlers["/"]["task:create_workspace"]
    first = asyncio.create_task(wrapped(command()))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert (await executor.inspect(command()))["status"] == "running"
        await wrapped(command("conflicting"))
        assert journal.lookup("conflicting") is None
        await wrapped(command("independent", "other-workspace"))
        assert calls == ["first", "independent"]
        assert "first" in executor.running
    finally:
        release.set()
        await first
    assert journal.pending("first")[0][0] == "workspace:created"
    journal.close()


@pytest.mark.asyncio
async def test_recovery_retries_transient_inspection_and_continues_other_records(
    tmp_path, monkeypatch
):
    journal = Journal(str(tmp_path), defer_recovery=True)
    for operation_id in ("first", "second"):
        journal.begin("task:create_workspace", command(operation_id, operation_id))
    socket = Socket(AsyncMock())
    reconciler = AsyncMock(
        side_effect=[OSError("runtime unavailable"), None, None]
    )
    executor = OperationExecutor(socket, journal, reconciler=reconciler)
    monkeypatch.setattr(executor, "helpers_active", lambda: False)
    await executor.replay()
    assert journal.lookup("first")[2] is None
    assert journal.lookup("second")[2]["execution_finished"] is True
    assert executor._recovered is False
    await executor.replay()
    assert journal.lookup("first")[2]["outcome_known"] is False
    assert executor._recovered is True
    assert reconciler.await_count == 3
    journal.close()


@pytest.mark.asyncio
async def test_orphan_helpers_keep_crashed_operations_unfinished_and_target_fenced(
    tmp_path, monkeypatch
):
    journal = Journal(str(tmp_path), defer_recovery=True)
    journal.begin("task:create_workspace", command())
    handler = AsyncMock()
    socket = Socket(handler)
    executor = OperationExecutor(socket, journal)
    monkeypatch.setattr(executor, "helpers_active", lambda: True)
    await executor.replay()
    assert journal.pending() == []
    assert journal.unfinished()
    assert executor._recovered is False
    await socket.handlers["/"]["task:create_workspace"](command("replacement"))
    handler.assert_not_awaited()
    assert journal.lookup("replacement") is None
    monkeypatch.setattr(executor, "helpers_active", lambda: False)
    await executor.replay()
    result = journal.pending()[0][1]
    assert result["execution_finished"] is True
    assert result["outcome_known"] is False
    journal.close()


@pytest.mark.asyncio
async def test_terminal_delivery_does_not_overwrite_lifecycle_checkpoint(tmp_path):
    journal = Journal(str(tmp_path))
    incarnation = {"domain": "workspace"}
    newer_proof = {
        "event": "workspace:credentials_injected", "credentials_present": True,
        "initialized": True, "state": "running",
    }

    async def handler(data):
        # A later lifecycle effect has already replaced the old operation proof.
        await journal.call("checkpoint", "workspace", incarnation, newer_proof)
        await socket.emit("workspace:created", {"credentials_present": False})

    observer = SimpleNamespace(
        workspace_incarnation=AsyncMock(return_value=(incarnation, "running")),
        lifecycle=SimpleNamespace(checkpoint_hook=AsyncMock()),
    )
    socket = Socket(handler)
    OperationExecutor(socket, journal, observer=observer)
    await socket.handlers["/"]["task:create_workspace"](command())
    assert journal.proof("workspace", incarnation) == newer_proof
    observer.workspace_incarnation.assert_not_awaited()
    journal.close()


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="requires Linux /proc")
def test_exited_unreaped_helper_is_quiescent_but_live_helper_is_not():
    """Exercise Linux's actual zombie state without reaping through poll()."""
    script = (
        "import ctypes,os,sys; "
        "ctypes.CDLL(None).prctl(15,b'qemu-img',0,0,0); "
        "os.write(1,b'ready\\n'); sys.stdin.buffer.read(1)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE
    )
    try:
        assert process.stdout.readline() == b"ready\n"
        comm = Path(f"/proc/{process.pid}/comm")
        assert _disk_helper_active(comm) is True
        process.stdin.write(b"x")
        process.stdin.flush()
        os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT)
        assert _disk_helper_active(comm) is False
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        process.stdin.close()
        process.stdout.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["workspace", "wrong-source"])
async def test_capture_recovery_matches_workspace_target_and_published_image_identity(
    tmp_path, source
):
    runtime = SimpleNamespace(
        inventory=AsyncMock(
            return_value=RuntimeInventory(
                complete=True,
                resources=[StorageResource(
                    resource_id="/managed/captured.qcow2", kind="image", managed=True,
                    state="ready", virtual_bytes=50 * 1024**3,
                    metadata={
                        "operation_id": "capture", "artifact_id": "captured-image",
                        "instance_id": source, "credential_clean": True,
                        "name": "captured", "created_at": "2026-01-01T00:00:00Z",
                        "size_bytes": 100,
                    },
                )],
            )
        ),
    )
    service = WorkspaceService(runtime, RunnerSettings(state_dir=str(tmp_path)))
    identity = {
        **command("capture"), "failure_event": "image_artifact:failed",
        "image_instance_id": "captured-image",
    }
    evidence = await service.publication_evidence(identity)
    if source == "wrong-source":
        assert evidence is None
    else:
        event, result = evidence
        assert event == "image_artifact:created"
        assert result["image_artifact_id"] == "captured-image"
        assert result["target"] == "workspace"
        assert result["virtual_size_bytes"] == 50 * 1024**3
        assert result["min_disk_size_gb"] == 50


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["restart", "failed_cleanup"])
async def test_orphan_guest_script_keeps_build_unfinished_until_guest_stops(
    tmp_path, monkeypatch, stage
):
    snapshot = {
        "complete": True,
        "runtimes": [{"resources": [{
            "kind": "workspace", "state": "running",
            "metadata": {"workspace_id": "image-build-orphan"},
        }]}],
    }
    observer = SimpleNamespace(inventory_for_runner=AsyncMock(return_value=snapshot))
    journal = Journal(str(tmp_path), defer_recovery=True)
    data = command("build", "image")

    async def failed_build(payload):
        await socket.emit("image:build_failed", {"error": "cleanup failed"})

    socket = Socket(failed_build)
    if stage == "restart":
        journal.begin("task:build_image", data)
    executor = OperationExecutor(socket, journal, observer=observer)
    monkeypatch.setattr(executor, "helpers_active", lambda: False)
    if stage == "failed_cleanup":
        await socket.handlers["/"]["task:build_image"](data)
    await executor.replay()
    assert journal.pending() == []
    assert journal.unfinished()
    assert executor._recovered is False
    assert "build" in executor._unresolved
    snapshot["runtimes"][0]["resources"][0]["state"] = "exited"
    await executor.replay()
    result = journal.pending()[0][1]
    assert result["outcome_known"] is False
    assert result["execution_finished"] is True
    journal.close()
