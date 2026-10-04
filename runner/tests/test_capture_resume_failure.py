"""Resume failure evidence must never infer stopped/clean from cached state."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from src.config import RunnerSettings
from src.interfaces.websocket import WebSocketInterface
from src.journal import COMMANDS, Journal, OperationExecutor
from src.models import WorkspaceInfo
from src.service import WorkspaceService


@pytest.fixture
def interface(tmp_path):
    ws = uuid.uuid4()
    incarnation = {"runtime": "qemu", "resource_id": "guest"}
    runtime = SimpleNamespace(
        start_workspace=AsyncMock(),
        reconfigure_workspace=AsyncMock(),
        workspace_incarnation=AsyncMock(return_value=(incarnation, "running")),
    )
    service = WorkspaceService({"qemu": runtime}, RunnerSettings())
    service._cache[ws] = WorkspaceInfo(
        workspace_id=ws,
        instance_id="guest",
        status="exited",
        runtime_type="qemu",
        credentials_present=False,
    )
    service.lifecycle._call_inject = AsyncMock(
        side_effect=RuntimeError("inject failed")
    )
    interface = WebSocketInterface(service, RunnerSettings(state_dir=str(tmp_path)))
    interface._operations.emit = AsyncMock()
    yield interface, service, runtime, ws, incarnation
    interface._operations.journal.close()


def command(ws):
    return {
        "task_id": str(uuid.uuid4()),
        "operation_id": str(uuid.uuid4()),
        "attempt": 1,
        "target": str(ws),
        "runner_id": str(uuid.uuid4()),
        "workspace_id": str(ws),
        "qemu_vcpus": 2,
        "qemu_memory_mb": 1024,
        "qemu_disk_size_gb": 10,
    }


async def test_started_guest_with_failed_injection_is_unknown(interface):
    iface, service, runtime, ws, incarnation = interface
    iface._operations.journal.checkpoint(
        str(ws),
        incarnation,
        {"state": "exited", "credentials_present": False, "event": "scrubbed"},
    )
    iface._operations.safe_retry = AsyncMock(return_value=False)
    data = command(ws)
    await iface._sio.handlers["/"]["task:resume_workspace"](data)
    result = iface._operations.journal.pending(data["operation_id"])[0][1]
    assert result["observed_status"] == "running"
    assert result["outcome_known"] is False
    assert "credentials_present" not in result
    assert "intervention" in result["error"]
    runtime.start_workspace.assert_awaited_once()
    assert service._cache[ws].credentials_present is None


@pytest.mark.parametrize(
    "state,present", [("running", True), ("exited", False), ("stopped", False)]
)
async def test_known_no_effect_failure_uses_exact_checkpoint(interface, state, present):
    iface, service, runtime, ws, incarnation = interface
    runtime.workspace_incarnation.return_value = (incarnation, state)
    service.resume_workspace = AsyncMock(side_effect=RuntimeError("preflight refused"))
    iface._operations.safe_retry = AsyncMock(return_value=False)
    iface._operations.journal.checkpoint(
        str(ws),
        incarnation,
        {"state": state, "credentials_present": present, "event": "workspace:resumed"},
    )
    iface._operations.safe_retry = AsyncMock(return_value=False)
    data = command(ws)
    await iface._sio.handlers["/"]["task:resume_workspace"](data)
    result = iface._operations.journal.pending(data["operation_id"])[0][1]
    assert result["observed_status"] == state
    assert result["credentials_present"] is present
    assert result["outcome_known"] is True
    runtime.start_workspace.assert_not_awaited()


@pytest.mark.parametrize(
    "observation",
    [
        None,
        ({"resource_id": "replacement"}, "exited"),
        ({"resource_id": "guest"}, "unknown"),
    ],
)
async def test_missing_or_changed_runtime_proof_cannot_use_cache(
    interface, observation
):
    iface, _service, runtime, ws, incarnation = interface
    iface._operations.journal.checkpoint(
        str(ws),
        incarnation,
        {"state": "exited", "credentials_present": False},
    )
    runtime.workspace_incarnation.return_value = observation
    evidence = await iface._workspace_failure_evidence(ws)
    assert evidence["outcome_known"] is False
    assert "credentials_present" not in evidence


async def test_inspection_failure_is_unknown(interface):
    iface, _service, runtime, ws, _ = interface
    runtime.workspace_incarnation.side_effect = RuntimeError("inspection unavailable")
    assert await iface._workspace_failure_evidence(ws) == {"outcome_known": False}


async def test_journal_recovery_preserves_explicit_unknown_outcome(tmp_path):
    journal = Journal(str(tmp_path), defer_recovery=True)
    data = command(uuid.uuid4())
    journal.begin("task:resume_workspace", data)
    socket = SimpleNamespace(
        handlers={"/": {event: AsyncMock() for event in COMMANDS}},
        emit=AsyncMock(),
        on=lambda *args: None,
    )
    executor = OperationExecutor(
        socket,
        journal,
        reconciler=AsyncMock(
            return_value=(
                "workspace:error",
                {"outcome_known": False, "error": "uncertain"},
            )
        ),
    )
    await executor.replay()
    result = journal.pending(data["operation_id"])[0][1]
    assert result["outcome_known"] is False
    assert result["execution_finished"] is True
    journal.close()


@pytest.mark.parametrize("old_running_proof", [False, True])
async def test_partial_stop_without_proof_emits_unknown_wrapped_result(
    interface, old_running_proof
):
    iface, service, runtime, ws, incarnation = interface
    if old_running_proof:
        iface._operations.journal.checkpoint(
            str(ws),
            incarnation,
            {"state": "running", "credentials_present": True, "initialized": True},
        )
    runtime.get_workspace_status = AsyncMock(
        return_value=SimpleNamespace(status="running")
    )
    service.lifecycle._call_remove = AsyncMock(
        side_effect=RuntimeError("partial scrub failed")
    )
    iface._operations.safe_retry = AsyncMock(return_value=False)
    data = command(ws)
    await iface._sio.handlers["/"]["task:stop_workspace"](data)
    result = iface._operations.journal.pending(data["operation_id"])[0][1]
    assert result["observed_status"] == "running"
    assert result["outcome_known"] is False
    assert "credentials_present" not in result
    assert "intervention" in result["error"]
    assert result["execution_finished"] is True
    service.lifecycle._call_remove.assert_awaited_once()
    assert service._cache[ws].credentials_present is None
    assert iface._operations.emit.call_args.args[1]["data"] == result


@pytest.mark.parametrize(
    "state,present", [("running", True), ("running", False), ("exited", False)]
)
async def test_known_stop_failure_uses_live_checkpoint(interface, state, present):
    iface, service, runtime, ws, incarnation = interface
    runtime.workspace_incarnation.return_value = (incarnation, state)
    service.stop_workspace = AsyncMock(side_effect=RuntimeError("stop refused"))
    iface._operations.safe_retry = AsyncMock(return_value=False)
    iface._operations.journal.checkpoint(
        str(ws),
        incarnation,
        {"state": state, "credentials_present": present, "event": "scrubbed"},
    )
    data = command(ws)
    await iface._sio.handlers["/"]["task:stop_workspace"](data)
    result = iface._operations.journal.pending(data["operation_id"])[0][1]
    assert result["observed_status"] == state
    assert result["credentials_present"] is present
    assert result["outcome_known"] is True
