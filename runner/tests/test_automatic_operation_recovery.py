"""Automatic interruption recovery without recipe replay or operator approval."""

import asyncio
import threading
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from src.config import RunnerSettings
from src.journal import COMMANDS, Journal, OperationExecutor
from src.service import WorkspaceService

WORKSPACE = str(uuid.UUID(int=101))
OPERATION = str(uuid.UUID(int=102))
OTHER = str(uuid.UUID(int=103))


def command(operation_id=OPERATION, target=WORKSPACE):
    return {
        "operation_id": operation_id,
        "task_id": operation_id,
        "target": target,
        "runner_id": "runner",
        "attempt": 1,
        "workspace_id": target,
    }


def snapshot(state=None, *, workspace_id=WORKSPACE, complete=True):
    resources = []
    if state is not None:
        resources.append(
            {
                "kind": "workspace",
                "state": state,
                "metadata": {"workspace_id": workspace_id},
            }
        )
    return {"complete": complete, "runtimes": [{"resources": resources}]}


class Socket:
    def __init__(self, handler=None):
        self.handlers = {"/": {event: handler or AsyncMock() for event in COMMANDS}}
        self.emit = AsyncMock()

    def on(self, event, handler):
        self.handlers["/"][event] = handler


@pytest.fixture(autouse=True)
def no_foreign_helpers(monkeypatch):
    monkeypatch.setattr(
        OperationExecutor, "helpers_active", staticmethod(lambda: False)
    )


def executor_for(tmp_path, observation=None, handler=None):
    observer = SimpleNamespace(
        inventory_for_runner=AsyncMock(return_value=observation or snapshot())
    )
    socket = Socket(handler)
    journal = Journal(str(tmp_path), defer_recovery=True)
    return OperationExecutor(socket, journal, observer=observer), socket, observer


@pytest.mark.asyncio
async def test_inspect_request_seals_quiescent_missing_identity(tmp_path):
    executor, socket, _ = executor_for(tmp_path)
    await executor.inspect_request(command())
    event, evidence = executor.emit.call_args.args
    assert event == "operation:inspection"
    assert evidence["status"] == "terminal"
    assert evidence["execution_finished"] and evidence["quiescent"]
    assert evidence["outcome_known"] is False
    assert executor.journal.lookup(OPERATION)[2]
    with pytest.raises(ValueError, match="collision"):
        await socket.handlers["/"]["task:create_workspace"](command())
    executor.journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["incomplete", "helper", "build"])
async def test_inspect_request_does_not_seal_unverified_missing_identity(
    tmp_path, monkeypatch, failure
):
    observed = snapshot(complete=failure != "incomplete")
    if failure == "build":
        observed = snapshot("running", workspace_id="image-build-orphan")
    executor, _, _ = executor_for(tmp_path, observed)
    monkeypatch.setattr(executor, "helpers_active", lambda: failure == "helper")
    await executor.inspect_request(command())
    assert executor.journal.lookup(OPERATION) is None
    assert executor.emit.call_args.args[1]["quiescent"] is False
    executor.journal.close()


@pytest.mark.asyncio
async def test_inspect_request_seal_rechecks_admission_epoch(tmp_path, monkeypatch):
    executor, _, _ = executor_for(tmp_path)
    inspect = executor.inspect
    count = 0

    async def raced_inspection(data):
        nonlocal count
        count += 1
        evidence = await inspect(data)
        if count == 2:
            executor._execution_epoch += 1
        return evidence

    monkeypatch.setattr(executor, "inspect", raced_inspection)
    await executor.inspect_request(command())
    assert executor.journal.lookup(OPERATION) is None
    assert executor.emit.call_args.args[1]["quiescent"] is False
    executor.journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state,removed,expected",
    [
        ("running", False, "running"),
        ("stopped", False, "stopped"),
        ("exited", False, "stopped"),
        (None, True, "absent"),
        (None, False, "unknown"),
        ("paused", False, "unknown"),
    ],
)
async def test_inspection_includes_exact_workspace_state(
    tmp_path, state, removed, expected
):
    runtime = SimpleNamespace(workspace_removed=AsyncMock(return_value=removed))
    service = WorkspaceService(runtime, RunnerSettings(state_dir=str(tmp_path)))
    evidence = await service.operation_workspace_evidence(command(), snapshot(state))
    assert evidence == {"workspace_status": expected}


@pytest.mark.asyncio
@pytest.mark.parametrize("proof_kind", ["exact", "wrong_stat", "wrong_state", "none"])
async def test_inspection_credential_presence_requires_exact_proof(
    tmp_path, proof_kind
):
    incarnation = {"resource_id": "exact", "metadata": {"disks": [{"inode": 4}]}}
    runtime = SimpleNamespace(
        workspace_incarnation=AsyncMock(return_value=(incarnation, "stopped"))
    )
    service = WorkspaceService(runtime, RunnerSettings(state_dir=str(tmp_path)))
    journal = Journal(str(tmp_path), defer_recovery=True)
    service._journal = journal
    if proof_kind != "none":
        saved = incarnation if proof_kind != "wrong_stat" else {"resource_id": "other"}
        journal.checkpoint(
            WORKSPACE,
            saved,
            {
                "state": "running" if proof_kind == "wrong_state" else "stopped",
                "credentials_present": False,
            },
        )
    evidence = await service.operation_workspace_evidence(
        command(), snapshot("stopped")
    )
    assert ("credentials_present" in evidence) == (proof_kind == "exact")
    journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["other", "incomplete", "duplicate"])
async def test_inspection_never_uses_wrong_workspace_inventory(tmp_path, mode):
    runtime = SimpleNamespace(workspace_removed=AsyncMock(return_value=False))
    service = WorkspaceService(runtime, RunnerSettings(state_dir=str(tmp_path)))
    observed = snapshot(
        "running",
        workspace_id=OTHER if mode == "other" else WORKSPACE,
        complete=mode != "incomplete",
    )
    if mode == "duplicate":
        observed["runtimes"][0]["resources"] *= 2
    evidence = await service.operation_workspace_evidence(command(), observed)
    assert evidence == {"workspace_status": "unknown"}


@pytest.mark.asyncio
async def test_recovery_cleans_only_exact_interrupted_build(tmp_path):
    runtime = SimpleNamespace(recover_interrupted_build=AsyncMock())
    service = WorkspaceService(runtime, RunnerSettings(state_dir=str(tmp_path)))
    await service.recover_interrupted_operation(
        {**command(), "failure_event": "image:build_failed"}
    )
    runtime.recover_interrupted_build.assert_awaited_once_with(OPERATION)
    await service.recover_interrupted_operation(
        {**command(OTHER), "failure_event": "workspace:error"}
    )
    runtime.recover_interrupted_build.assert_awaited_once()


@pytest.mark.asyncio
async def test_recovery_retries_failed_build_cleanup(tmp_path):
    executor, _, observer = executor_for(tmp_path)
    executor.journal.begin("task:build_image", command())
    executor._unresolved[OPERATION] = command()
    observer.recover_interrupted_operation = AsyncMock(
        side_effect=[OSError("busy"), None]
    )
    await executor.replay()
    assert executor.journal.lookup(OPERATION)[2] is None
    assert not executor._recovered
    await executor.replay()
    assert executor.journal.lookup(OPERATION)[2]["execution_finished"]
    assert observer.recover_interrupted_operation.await_count == 2
    executor.journal.close()


@pytest.mark.asyncio
async def test_recovery_successful_publication_wins_after_build_cleanup(tmp_path):
    executor, _, observer = executor_for(tmp_path)
    executor.journal.begin("task:build_image", command())
    observer.recover_interrupted_operation = AsyncMock()
    executor.reconciler = AsyncMock(
        return_value=("image:built", {"image_path": "published"})
    )
    await executor.replay()
    row = executor.journal.lookup(OPERATION)
    assert row[1] == "image:built"
    assert row[2]["image_path"] == "published"
    assert row[2]["outcome_known"] is True
    observer.recover_interrupted_operation.assert_awaited_once()
    executor.journal.close()


@pytest.mark.asyncio
async def test_late_old_observation_does_not_claim_quiescence(tmp_path):
    executor, _, observer = executor_for(tmp_path)

    async def observation(identity, scan):
        executor._execution_epoch += 1
        return {"workspace_status": "running", "credentials_present": True}

    observer.operation_workspace_evidence = observation
    result = await executor.inspect(command())
    assert result["quiescent"] is False
    assert result["workspace_status"] == "unknown"
    assert "credentials_present" not in result
    executor.journal.close()


@pytest.mark.asyncio
async def test_sealed_missing_identity_uses_retryable_diagnostic(tmp_path):
    executor, _, _ = executor_for(tmp_path)
    result = await executor.seal(command())
    assert result["result"]["diagnostic_code"] == "interrupted"
    assert "manual" not in result["result"]["error"].lower()
    assert "preserved" in result["result"]["error"].lower()
    executor.journal.close()


@pytest.mark.asyncio
async def test_unfinished_recovery_keeps_target_bound_until_commit(
    tmp_path, monkeypatch
):
    executor, socket, _ = executor_for(tmp_path)
    executor.journal.begin("task:create_workspace", command())
    executor._unresolved[OPERATION] = command()
    entered, release = threading.Event(), threading.Event()
    finish = executor.journal.finish

    def slow_finish(*args):
        entered.set()
        assert release.wait(5)
        finish(*args)

    monkeypatch.setattr(executor.journal, "finish", slow_finish)
    recovering = asyncio.create_task(executor.replay())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        await socket.handlers["/"]["task:create_workspace"](command(OTHER))
        assert OTHER not in executor.running
        assert OPERATION in executor._unresolved
    finally:
        release.set()
        await recovering
    assert OPERATION not in executor._unresolved
    assert executor.journal.lookup(OTHER) is None
    executor.journal.close()


@pytest.mark.asyncio
async def test_live_same_process_build_is_not_cleaned_by_recovery(tmp_path):
    executor, _, observer = executor_for(tmp_path)
    executor.journal.begin("task:build_image", command())
    observer.recover_interrupted_operation = AsyncMock()
    executor.running[OPERATION] = asyncio.current_task()
    await executor.replay()
    observer.recover_interrupted_operation.assert_not_awaited()
    assert executor.journal.lookup(OPERATION)[2] is None
    assert not executor._live_build_guests(
        snapshot("running", workspace_id=f"image-build-{OPERATION}")
    )
    executor.running.clear()
    executor.journal.close()


@pytest.mark.asyncio
async def test_recovery_waits_for_exact_runtime_absence(tmp_path):
    executor, _, observer = executor_for(tmp_path)
    executor.journal.begin("task:build_image", command())
    observer.recover_interrupted_operation = AsyncMock(
        side_effect=RuntimeError("temporary build resources still present")
    )
    await executor.replay()
    assert executor.journal.lookup(OPERATION)[2] is None
    assert not executor._recovered
    executor.journal.close()


@pytest.mark.asyncio
async def test_restart_recovers_stop_checkpoint_without_rerun(tmp_path):
    incarnation = {"resource_id": WORKSPACE, "metadata": {"disk": "same"}}
    journal = Journal(str(tmp_path), defer_recovery=True)
    journal.begin("task:stop_workspace", command())
    journal.checkpoint(
        WORKSPACE,
        incarnation,
        {
            "event": "scrubbed",
            "operation_id": OPERATION,
            "state": "stopped",
            "credentials_present": False,
        },
    )
    journal.close()
    journal = Journal(str(tmp_path), defer_recovery=True)
    runtime = SimpleNamespace(
        workspace_incarnation=AsyncMock(return_value=(incarnation, "stopped"))
    )
    service = WorkspaceService(runtime, RunnerSettings(state_dir=str(tmp_path)))
    service._journal = journal
    handler = AsyncMock()
    socket = Socket(handler)
    executor = OperationExecutor(
        socket, journal, reconciler=service.publication_evidence
    )
    await executor.replay()
    assert journal.lookup(OPERATION)[1] == "workspace:stopped"
    assert journal.lookup(OPERATION)[2]["credentials_present"] is False
    handler.assert_not_awaited()
    journal.close()


@pytest.mark.asyncio
async def test_restart_preserves_create_ambiguity_without_init_replay(tmp_path):
    journal = Journal(str(tmp_path), defer_recovery=True)
    journal.begin("task:create_workspace", command())
    journal.close()
    journal = Journal(str(tmp_path), defer_recovery=True)
    handler = AsyncMock()
    socket = Socket(handler)
    observer = SimpleNamespace(
        inventory_for_runner=AsyncMock(return_value=snapshot("running"))
    )
    executor = OperationExecutor(socket, journal, observer=observer)
    await executor.replay()
    event, result = journal.pending()[0]
    assert event == "workspace:error"
    assert result["outcome_known"] is False
    assert result["execution_finished"] is True
    assert (await executor.inspect(command()))["quiescent"] is True
    handler.assert_not_awaited()
    journal.close()


@pytest.mark.asyncio
async def test_other_active_build_does_not_block_finished_target_inspection(tmp_path):
    observation = snapshot("running", workspace_id=f"image-build-{OTHER}")
    executor, _, observer = executor_for(tmp_path, observation)
    observer.recover_interrupted_operation = AsyncMock()
    executor.running[OTHER] = asyncio.current_task()
    executor._bindings[OTHER] = {**command(OTHER, OTHER), "build_job_id": "build"}
    result = await executor.seal(command())
    assert result["status"] == "terminal"
    assert result["quiescent"] is True
    assert result["execution_finished"] is True
    observer.recover_interrupted_operation.assert_not_awaited()
    assert OTHER in executor.running
    executor.running.clear()
    executor._bindings.clear()
    executor.journal.close()


@pytest.mark.asyncio
async def test_unknown_terminal_inspection_quiesces_remote_effects(tmp_path):
    executor, _, observer = executor_for(tmp_path, snapshot("running"))
    executor.journal.begin("task:create_workspace", command())
    executor.journal.finish(
        OPERATION,
        "workspace:error",
        {
            **command(),
            "outcome_known": False,
            "execution_finished": True,
            "diagnostic_code": "interrupted",
            "error": "partial init",
        },
    )

    async def stop(identity):
        assert identity["workspace_id"] == WORKSPACE
        observer.inventory_for_runner.return_value = snapshot("exited")

    observer.recover_interrupted_operation = AsyncMock(side_effect=stop)
    result = await executor.inspect(command())
    observer.recover_interrupted_operation.assert_awaited_once()
    assert result["quiescent"] and result["execution_finished"]
    assert result["outcome_known"] is False
    assert result["inventory"]["runtimes"][0]["resources"][0]["state"] == "exited"
    assert "credentials_present" not in result
    executor.journal.close()


@pytest.mark.asyncio
async def test_quiescence_failure_retains_unknown_execution_fence(tmp_path):
    executor, _, observer = executor_for(tmp_path, snapshot("running"))
    executor.journal.begin("task:create_workspace", command())
    observer.recover_interrupted_operation = AsyncMock(
        side_effect=[RuntimeError("shutdown unverified"), None]
    )
    await executor.replay()
    assert executor.journal.lookup(OPERATION)[2] is None
    assert not executor._recovered
    await executor.replay()
    assert executor.journal.lookup(OPERATION)[2]["execution_finished"] is True
    assert observer.recover_interrupted_operation.await_count == 2
    executor.journal.close()


@pytest.mark.asyncio
async def test_confirmed_workspace_checkpoint_prevents_recovery_shutdown(tmp_path):
    executor, _, observer = executor_for(tmp_path, snapshot("running"))
    executor.journal.begin("task:create_workspace", command())
    observer.recover_interrupted_operation = AsyncMock()
    executor.reconciler = AsyncMock(
        return_value=(
            "workspace:created",
            {
                **command(),
                "credentials_present": True,
                "outcome_known": True,
            },
        )
    )
    await executor.replay()
    assert executor.journal.lookup(OPERATION)[1] == "workspace:created"
    observer.recover_interrupted_operation.assert_not_awaited()
    executor.journal.close()


@pytest.mark.parametrize("event,valid", [
    ("task:create_workspace", True),
    ("task:create_workspace_from_image_artifact", True),
    ("task:stop_workspace", False),
    ("task:remove_workspace", False),
])
def test_workspace_allocation_ownership_survives_ack_restart(tmp_path, event, valid):
    journal = Journal(str(tmp_path), defer_recovery=True)
    data = command()
    journal.begin(event, data)
    journal.finish(OPERATION, "workspace:error", {
        **data, "error": "interrupted", "outcome_known": False,
    })
    journal.acknowledge(data)
    journal.close()
    journal = Journal(str(tmp_path), defer_recovery=True)
    try:
        assert journal.owns_workspace_allocation(WORKSPACE) is valid
        assert not journal.owns_workspace_allocation(OTHER)
        journal.begin(event, command(OTHER, "wrong-target") | {"workspace_id": OTHER})
        assert not journal.owns_workspace_allocation(OTHER)
    finally:
        journal.close()


def test_removed_workspace_revokes_historic_allocation_ownership(tmp_path):
    journal = Journal(str(tmp_path), defer_recovery=True)
    try:
        journal.begin("task:create_workspace", command())
        journal.finish(OPERATION, "workspace:created", command())
        removal = command(OTHER)
        journal.begin("task:remove_workspace", removal)
        journal.finish(OTHER, "workspace:removed", removal)
        journal.acknowledge(removal)
        assert not journal.owns_workspace_allocation(WORKSPACE)
    finally:
        journal.close()
    journal = Journal(str(tmp_path), defer_recovery=True)
    try:
        assert not journal.owns_workspace_allocation(WORKSPACE)
    finally:
        journal.close()


def test_new_create_after_removal_establishes_fresh_allocation_ownership(tmp_path):
    journal = Journal(str(tmp_path), defer_recovery=True)
    try:
        journal.begin("task:create_workspace", command())
        removal = command(OTHER)
        journal.begin("task:remove_workspace", removal)
        journal.finish(OTHER, "workspace:removed", removal)
        assert not journal.owns_workspace_allocation(WORKSPACE)
        journal.begin("task:create_workspace", command(str(uuid.UUID(int=104))))
        assert journal.owns_workspace_allocation(WORKSPACE)
    finally:
        journal.close()


def test_known_failed_create_does_not_grant_allocation_ownership(tmp_path):
    journal = Journal(str(tmp_path), defer_recovery=True)
    try:
        journal.begin("task:create_workspace", command())
        journal.finish(OPERATION, "workspace:error", {
            **command(), "error": "Rejected before allocation", "outcome_known": True,
        })
        assert not journal.owns_workspace_allocation(WORKSPACE)
    finally:
        journal.close()


@pytest.mark.parametrize("state", [
    "running", "paused", "suspended", "stopping", "failed", "unknown", "stopped",
    "exited",
])
def test_nonstopped_domain_observation_revokes_old_scrub_proof(tmp_path, state):
    journal = Journal(str(tmp_path), defer_recovery=True)
    incarnation = {"domain_uuid": "exact", "disk": {"inode": 1}}
    proof = {"event": "scrubbed", "state": "exited", "credentials_present": False}
    journal.checkpoint(WORKSPACE, incarnation, proof)
    journal.observe_workspace(WORKSPACE, state)
    assert journal.proof(WORKSPACE, incarnation) == (
        proof if state in {"stopped", "exited"} else None
    )
    journal.close()
