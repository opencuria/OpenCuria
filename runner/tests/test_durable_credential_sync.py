"""Credential writes retain journal replay and reject unbound delayed packets."""

import uuid
from unittest.mock import AsyncMock

import pytest
from src.config import RunnerSettings
from src.interfaces.websocket import WebSocketInterface

from tests.test_automatic_operation_recovery import command, executor_for, snapshot
from tests.test_websocket_interface import DummyService


async def test_credential_replay_returns_ack_without_repeating_secret_write(tmp_path):
    service = DummyService()
    interface = WebSocketInterface(service, RunnerSettings(state_dir=str(tmp_path)))
    interface._operations.emit = AsyncMock()
    workspace_id, operation_id = str(uuid.uuid4()), str(uuid.uuid4())
    payload = {
        **command(operation_id, workspace_id),
        "env_vars": {"TOKEN": "NEVER_PERSIST_SECRET"},
    }
    handler = interface._sio.handlers["/"]["task:inject_credentials"]
    assert await handler(payload) == {"ok": True, "credentials_present": True}
    repeated = await handler(payload)
    assert repeated["ok"] and repeated["credentials_present"]
    service.lifecycle.inject_credentials.assert_awaited_once()
    for file in tmp_path.glob("operations.sqlite3*"):
        assert b"NEVER_PERSIST_SECRET" not in file.read_bytes()
    # Late unbound legacy delivery cannot write after the upgrade adapter seals.
    assert await handler({"task_id": "legacy", "workspace_id": workspace_id}) is None
    service.lifecycle.inject_credentials.assert_awaited_once()
    interface._operations.journal.close()


async def test_legacy_inspection_seals_then_quiesces_before_releasing(tmp_path):
    executor, _, observer = executor_for(tmp_path, snapshot("running"))
    observer.recover_interrupted_operation = AsyncMock()

    async def stop(identity):
        assert executor.journal.lookup(identity["operation_id"]) is not None
        observer.inventory_for_runner.return_value = snapshot("stopped")
        return "stopped"

    observer.recover_interrupted_operation.side_effect = stop
    data = {**command(), "legacy_credential_sync": True}
    await executor.inspect_request(data)
    event, evidence = executor.emit.await_args.args
    assert event == "operation:inspection"
    assert evidence["status"] == "terminal"
    assert evidence["durable_credentials"] is True
    assert evidence["legacy_credential_quiesced"] is True
    assert evidence["quiescent"] is True
    observer.recover_interrupted_operation.assert_awaited_once()
    executor.journal.close()


async def test_noop_recovery_cannot_attest_legacy_credential_quiescence(tmp_path):
    executor, _, observer = executor_for(tmp_path, snapshot("running"))
    observer.recover_interrupted_operation = AsyncMock(return_value=None)
    await executor.inspect_request({**command(), "legacy_credential_sync": True})
    evidence = executor.emit.await_args.args[1]
    assert evidence["status"] == "terminal"
    assert evidence["legacy_credential_quiesced"] is False
    executor.journal.close()


async def test_inspection_does_not_recover_while_disk_helpers_are_active(
    tmp_path, monkeypatch
):
    from src.journal import OperationExecutor

    executor, _, observer = executor_for(tmp_path, snapshot("running"))
    payload = command()
    executor.journal.begin("task:inject_credentials", payload)
    executor.journal.finish(
        payload["operation_id"],
        "workspace:error",
        {
            **payload,
            "outcome_known": False,
            "execution_finished": True,
        },
    )
    observer.recover_interrupted_operation = AsyncMock(return_value="stopped")
    monkeypatch.setattr(OperationExecutor, "helpers_active", staticmethod(lambda: True))
    evidence = await executor.inspect(payload)
    observer.recover_interrupted_operation.assert_not_awaited()
    assert not evidence["execution_finished"]
    assert evidence["diagnostic_code"] == "disk_helpers_active"
    executor.journal.close()


@pytest.mark.parametrize("finished", [False, True])
async def test_inspection_reports_only_typed_publication_ownership_conflicts(
    tmp_path, monkeypatch, finished
):
    from src.journal import OperationExecutor
    from src.runtime.recovery import RecoveryConflict

    executor, _, observer = executor_for(tmp_path, snapshot("stopped"))
    payload = command()
    executor.journal.begin("task:create_image_artifact", payload)
    if finished:
        executor.journal.finish(
            payload["operation_id"],
            "image_artifact:failed",
            {
                **payload,
                "outcome_known": False,
                "execution_finished": True,
            },
        )
    observer.recover_interrupted_operation = AsyncMock(
        side_effect=RecoveryConflict("Publication staging file ownership changed")
    )
    monkeypatch.setattr(
        OperationExecutor, "helpers_active", staticmethod(lambda: False)
    )
    evidence = await executor.inspect(payload)
    assert evidence["identity"]["operation_id"] == payload["operation_id"]
    assert evidence["instance_id"] == executor.journal.instance_id
    assert evidence["status"] == "unknown" and not evidence["execution_finished"]
    assert not evidence["quiescent"]
    assert evidence["recovery_blocked_code"] == "publication_ownership_conflict"
    assert evidence["diagnostic"] == "Publication staging file ownership changed"
    observer.recover_interrupted_operation.side_effect = OSError(
        "temporary scan failure"
    )
    with pytest.raises(OSError):
        await executor.inspect(payload)
    executor.journal.close()
