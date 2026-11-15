"""Refusals before any effect are known failures with stable diagnostic codes."""

import asyncio
import errno
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.config import RunnerSettings
from src.journal import Journal
from src.models import WorkspaceInfo
from src.runtime.diagnostics import (
    DISK_SHRINK_UNSUPPORTED,
    IMAGE_UNAVAILABLE,
    OperationError,
    OperationRejectedError,
    failure_code,
)
from src.service import WorkspaceService
from tests.command_support import make_interface

INCARNATION = {"runtime": "qemu", "resource_id": "guest"}
SECRET_PATH = "/var/lib/opencuria/secret-token"


@pytest.fixture(autouse=True)
def isolated_helper_observation(monkeypatch):
    """Keep journal inspection independent of unrelated host disk helpers."""
    monkeypatch.setattr("src.operation_executor.disk_helpers_active", lambda: False)


@pytest.fixture
def stopped(tmp_path):
    """A stopped, scrubbed guest behind the production command graph."""
    ws = uuid.uuid4()
    runtime = SimpleNamespace(
        validate_reconfigure=AsyncMock(),
        reconfigure_workspace=AsyncMock(),
        start_workspace=AsyncMock(),
        get_workspace_status=AsyncMock(return_value=SimpleNamespace(status="exited")),
        workspace_incarnation=AsyncMock(return_value=(INCARNATION, "exited")),
    )
    service = WorkspaceService(runtime, RunnerSettings())
    service.registry._cache[ws] = WorkspaceInfo(
        workspace_id=ws, instance_id="guest", status="exited", credentials_present=False
    )
    interface = make_interface(service, RunnerSettings(state_dir=str(tmp_path)))
    interface._operations.emit = AsyncMock()
    # An automatic retry would be permitted: refusals must not use it.
    interface._operations.safe_retry = AsyncMock(return_value=True)
    yield interface, service, runtime, ws
    interface._operations.journal.close()


def command(ws, **fields):
    operation = str(uuid.uuid4())
    return {
        "task_id": operation,
        "operation_id": operation,
        "attempt": 1,
        "target": str(ws),
        "runner_id": str(uuid.uuid4()),
        "workspace_id": str(ws),
        "qemu_vcpus": 2,
        "qemu_memory_mb": 1024,
        "qemu_disk_size_gb": 10,
        **fields,
    }


async def run(interface, event, data):
    await interface._sio.handlers["/"][event](data)
    return interface._operations.journal.pending(data["operation_id"])[0]


def scrubbed(interface, ws):
    interface._operations.journal.checkpoint(
        str(ws),
        INCARNATION,
        {"state": "exited", "credentials_present": False, "event": "scrubbed"},
    )


async def test_rejected_resume_is_known_and_preserves_scrub_proof(stopped):
    interface, service, runtime, ws = stopped
    scrubbed(interface, ws)
    runtime.validate_reconfigure.side_effect = OperationRejectedError(
        DISK_SHRINK_UNSUPPORTED, f"Reducing disk below {SECRET_PATH}"
    )

    event, result = await run(interface, "task:resume_workspace", command(ws))

    assert event == "workspace:error"
    assert result["outcome_known"] is True
    assert result["diagnostic_code"] == "disk_shrink_unsupported"
    assert result["error"] == "Operation failed; execution finished"
    assert SECRET_PATH not in str(result)
    assert result["observed_status"] == "exited"
    assert result["credentials_present"] is False
    runtime.validate_reconfigure.assert_awaited_once()
    runtime.reconfigure_workspace.assert_not_awaited()
    runtime.start_workspace.assert_not_awaited()
    interface._operations.safe_retry.assert_not_awaited()
    # Nothing was revoked: the guest can still be stopped or captured as clean.
    assert service.registry.get_cached(ws).credentials_present is False
    assert interface._operations.journal.proof(str(ws), INCARNATION) is not None


async def test_missing_resume_resources_are_rejected_before_effects(stopped):
    interface, _service, runtime, ws = stopped
    data = command(ws)
    del data["qemu_disk_size_gb"]

    _event, result = await run(interface, "task:resume_workspace", data)

    assert result["outcome_known"] is True
    assert result["diagnostic_code"] == "invalid_resources"
    runtime.validate_reconfigure.assert_not_awaited()
    runtime.start_workspace.assert_not_awaited()


async def test_failure_after_effects_stays_unknown_and_is_retried(stopped, monkeypatch):
    interface, service, runtime, ws = stopped
    scrubbed(interface, ws)
    runtime.start_workspace.side_effect = RuntimeError(f"boot failed {SECRET_PATH}")
    monkeypatch.setattr("src.operation_executor.asyncio.sleep", AsyncMock())

    event, result = await run(interface, "task:resume_workspace", command(ws))

    assert event == "workspace:error"
    assert result["outcome_known"] is False
    assert result["diagnostic_code"] == "handler_failed"
    assert result["error"] == "Operation interrupted; resources preserved for retry"
    assert SECRET_PATH not in str(result)
    assert runtime.start_workspace.await_count == 3
    assert service.registry.get_cached(ws).credentials_present is None


async def test_unproven_stop_of_stopped_guest_is_a_known_refusal(stopped):
    interface, _service, runtime, ws = stopped

    event, result = await run(interface, "task:stop_workspace", command(ws))

    assert event == "workspace:error"
    assert result["outcome_known"] is True
    assert result["diagnostic_code"] == "credential_scrub_unverified"
    assert result["observed_status"] == "exited"
    assert "credentials_present" not in result
    runtime.get_workspace_status.assert_awaited_once()
    interface._operations.safe_retry.assert_not_awaited()


@pytest.mark.parametrize(
    "payload,code",
    [({"qemu_vcpus": None}, "invalid_resources"), ({}, "disk_shrink_unsupported")],
)
async def test_rejected_update_keeps_streams_open(stopped, payload, code):
    interface, service, runtime, ws = stopped
    service.registry.get_cached(ws).status = "running"
    service.streams.close_workspace_streams = AsyncMock()
    runtime.validate_reconfigure.side_effect = OperationRejectedError(
        DISK_SHRINK_UNSUPPORTED, "Reducing QEMU disk size is not supported"
    )

    event, result = await run(
        interface, "task:update_workspace", command(ws, **payload)
    )

    assert event == "workspace:error"
    assert result["outcome_known"] is True
    assert result["diagnostic_code"] == code
    service.streams.close_workspace_streams.assert_not_awaited()
    runtime.reconfigure_workspace.assert_not_awaited()


@pytest.mark.parametrize("source", ["base", "artifact"])
async def test_rejected_create_leaves_no_reservation_or_allocation_lineage(
    tmp_path, source
):
    ws = uuid.uuid4()
    refusal = OperationRejectedError(IMAGE_UNAVAILABLE, f"missing {SECRET_PATH}")
    runtime = SimpleNamespace(
        create_workspace=AsyncMock(side_effect=refusal),
        create_workspace_from_image_artifact=AsyncMock(side_effect=refusal),
    )
    service = WorkspaceService(runtime, RunnerSettings())
    interface = make_interface(service, RunnerSettings(state_dir=str(tmp_path)))
    interface._operations.emit = AsyncMock()
    try:
        if source == "base":
            event = "task:create_workspace"
            data = command(ws, base_image_path="/images/base.qcow2")
        else:
            event = "task:create_workspace_from_image_artifact"
            data = command(ws, image_artifact_id="artifact")

        terminal, result = await run(interface, event, data)

        assert terminal == "workspace:error"
        assert result["outcome_known"] is True
        assert result["diagnostic_code"] == "image_unavailable"
        assert SECRET_PATH not in str(result)
        assert not service.registry.workspace_exists(ws)
        journal = interface._operations.journal
        assert not journal.owns_workspace_allocation(str(ws))
    finally:
        interface._operations.journal.close()


def test_refused_create_does_not_revoke_older_allocation_lineage(tmp_path):
    journal = Journal(str(tmp_path))
    workspace = str(uuid.uuid4())

    def create(operation, result):
        data = {
            "task_id": operation,
            "operation_id": operation,
            "attempt": 1,
            "target": workspace,
            "runner_id": "runner",
            "workspace_id": workspace,
        }
        journal.begin("task:create_workspace", data)
        journal.finish(operation, "workspace:error", {**data, **result})

    create("partial", {"error": "boot lost", "outcome_known": False})
    create(
        "refused",
        {
            "error": "Workspace domain already exists",
            "diagnostic_code": "workspace_exists",
            "outcome_known": True,
        },
    )
    assert journal.owns_workspace_allocation(workspace)
    journal.close()


def test_journal_keeps_known_codes_and_sanitizes_messages(tmp_path):
    journal = Journal(str(tmp_path))
    base = {"task_id": "task", "attempt": 1, "target": "t", "runner_id": "runner"}
    cases = {
        "coded": {"diagnostic_code": "build_queue_timeout", "error": SECRET_PATH},
        "forged": {"diagnostic_code": SECRET_PATH, "error": "boom"},
        "storage": {"error": f"No space left on device: {SECRET_PATH}"},
    }
    for operation, result in cases.items():
        data = {**base, "operation_id": operation}
        journal.begin("task:build_image", data)
        journal.finish(operation, "image:build_failed", {**data, **result})
    codes = {
        operation: journal.pending(operation)[0][1]["diagnostic_code"]
        for operation in cases
    }
    assert codes == {
        "coded": "build_queue_timeout",
        "forged": "handler_failed",
        "storage": "insufficient_storage",
    }
    assert SECRET_PATH not in str(journal.pending())
    journal.close()


def test_failure_code_classifies_without_messages():
    assert failure_code(OperationError("build_script_failed", "exit 1")) == (
        "build_script_failed"
    )
    try:
        try:
            raise OSError(errno.ENOSPC, "No space left on device")
        except OSError as exc:
            raise RuntimeError("Image conversion failed") from exc
    except RuntimeError as wrapped:
        assert failure_code(wrapped) == "insufficient_storage"
    assert failure_code(asyncio.TimeoutError()) == "handler_failed"
    with pytest.raises(ValueError, match="Unknown diagnostic code"):
        OperationRejectedError("not_a_code", "refused")


async def test_inject_reply_carries_code_not_exception_text(stopped):
    interface, service, _runtime, ws = stopped
    service.credentials.inject_workspace_credentials = AsyncMock(
        side_effect=OSError(errno.ENOSPC, f"No space left on device: {SECRET_PATH}")
    )

    reply = await interface._sio.handlers["/"]["task:inject_credentials"](command(ws))

    assert reply["ok"] is False
    assert reply["diagnostic_code"] == "insufficient_storage"
    assert reply["outcome_known"] is False
    assert "error" not in reply
    assert SECRET_PATH not in str(reply)
