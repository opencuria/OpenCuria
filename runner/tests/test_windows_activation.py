"""Windows license activation: secret-safe delivery and outcome reporting."""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.commands import LifecycleCommands
from src.journal import SECRET_FIELDS, Journal
from src.services.windows_activation import (
    WindowsActivationService,
    _parse_result,
    validate_activation,
)

KEY = "ABCDE-FGHIJ-KLMNO-PQRST-UVWXY"


def test_requests_are_validated() -> None:
    assert validate_activation({"mode": "key", "product_key": KEY}) == {
        "mode": "key",
        "product_key": KEY,
    }
    assert validate_activation({"mode": "kms", "kms_host": "kms.example:1688"}) == {
        "mode": "kms",
        "kms_host": "kms.example:1688",
    }
    assert validate_activation({"mode": "kms"}) == {"mode": "kms", "kms_host": ""}
    assert validate_activation({"mode": "none", "product_key": KEY}) == {"mode": "none"}
    for invalid in (
        None,
        {"mode": "key", "product_key": "abc"},
        {"mode": "key", "product_key": KEY + "; rm -rf /"},
        {"mode": "kms", "kms_host": "host; reboot"},
        {"mode": "other"},
    ):
        with pytest.raises(ValueError):
            validate_activation(invalid)


def test_results_come_only_from_the_scripts_json_line() -> None:
    assert _parse_result(0, 'noise\n{"status":"activated","code":""}\n') == {
        "status": "activated",
        "code": "",
    }
    assert _parse_result(0, '{"status":"failed","code":"0xC004F074"}') == {
        "status": "failed",
        "code": "0xC004F074",
    }
    assert _parse_result(1, "boom") == {"status": "failed", "code": "exit_1"}
    assert _parse_result(0, '{"status":"maybe"}') == {"status": "failed", "code": "exit_0"}


class _Runtime:
    def __init__(self, stdout: str = '{"status":"activated","code":""}') -> None:
        self.archives: list[tuple[str, bytes]] = []
        self.calls: list[tuple[list[str], bytes]] = []
        self.stdout = stdout

    async def put_archive(self, instance_id: str, path: str, data: bytes) -> None:
        self.archives.append((path, data))

    async def exec_with_input(self, instance_id, command, data, workdir=None):
        self.calls.append((command, data))
        return 0, self.stdout, ""


def _service(runtime) -> WindowsActivationService:
    info = SimpleNamespace(instance_id="guest")
    return WindowsActivationService(lambda _: info, lambda _: runtime)


async def test_product_key_travels_on_stdin_only() -> None:
    runtime = _Runtime()
    result = await _service(runtime).activate(
        uuid.uuid4(), {"mode": "key", "product_key": KEY}
    )
    assert result == {"status": "activated", "code": ""}
    (command, stdin), = runtime.calls
    assert KEY not in " ".join(command)
    assert json.loads(stdin) == {"mode": "key", "product_key": KEY}
    path, archive = runtime.archives[0]
    assert path == "/tmp"
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        (member,) = tar.getmembers()
        script = tar.extractfile(member).read().decode()
    assert member.name.endswith(".ps1") and member.mode == 0o600
    assert KEY not in script and "[Console]::In.ReadToEnd()" in script
    assert "rm -f" in command[-1]


async def test_no_request_means_no_guest_work() -> None:
    runtime = _Runtime()
    assert await _service(runtime).activate(uuid.uuid4(), None) is None
    assert runtime.calls == []


async def test_failures_are_reported_never_raised(monkeypatch) -> None:
    runtime = _Runtime()
    assert await _service(runtime).activate(uuid.uuid4(), {"mode": "bogus"}) == {
        "status": "failed",
        "code": "invalid_request",
    }
    runtime.exec_with_input = AsyncMock(side_effect=OSError("ssh lost"))
    assert await _service(runtime).activate(uuid.uuid4(), {"mode": "none"}) == {
        "status": "failed",
        "code": "runner_error",
    }

    async def slow(*args, **kwargs):
        await asyncio.sleep(10)

    runtime.exec_with_input = slow
    monkeypatch.setattr("src.services.windows_activation.ACTIVATION_TIMEOUT", 0.01)
    assert await _service(runtime).activate(uuid.uuid4(), {"mode": "none"}) == {
        "status": "failed",
        "code": "timeout",
    }


def test_activation_requests_are_never_journaled() -> None:
    assert "windows_activation" in SECRET_FIELDS
    assert "windows_activation_result" not in SECRET_FIELDS


def test_journal_keeps_the_outcome_but_not_the_request(tmp_path) -> None:
    journal = Journal(str(tmp_path), defer_recovery=True)
    operation = str(uuid.uuid4())
    request = {
        "operation_id": operation,
        "workspace_id": str(uuid.uuid4()),
        "windows_activation": {"mode": "key", "product_key": KEY},
    }
    try:
        assert journal.begin("task:resume_workspace", request)
        journal.finish(
            operation,
            "workspace:resumed",
            {
                **request,
                "windows_activation_result": {"status": "failed", "code": "0x803F7001"},
            },
        )
        identity, event, result = journal.lookup(operation)
        assert event == "workspace:resumed"
        assert result["windows_activation_result"] == {
            "status": "failed",
            "code": "0x803F7001",
        }
        assert "windows_activation" not in result
        journal.db.commit()
        for stored in tmp_path.glob("operations.sqlite3*"):
            assert KEY.encode() not in stored.read_bytes()
        assert KEY not in json.dumps(identity)
    finally:
        journal.close()


async def test_lifecycle_results_carry_the_activation_outcome() -> None:
    workspace_id = uuid.uuid4()
    activation = SimpleNamespace(
        activate=AsyncMock(return_value={"status": "failed", "code": "0xC004F074"})
    )
    lifecycle = SimpleNamespace(
        create_workspace=AsyncMock(return_value=(workspace_id, False)),
        resume_workspace=AsyncMock(return_value=False),
    )
    service = SimpleNamespace(lifecycle=lifecycle, windows_activation=activation)
    commands = LifecycleCommands.__new__(LifecycleCommands)
    commands._service = service

    created = await commands.on_create_workspace(
        {
            "task_id": "t",
            "workspace_id": str(workspace_id),
            "base_image_path": "/images/windows.qcow2",
            "guest_os": "windows",
            "windows_activation": {"mode": "kms", "kms_host": ""},
        }
    )
    assert created.data["windows_activation_result"] == {
        "status": "failed",
        "code": "0xC004F074",
    }
    assert lifecycle.create_workspace.await_args.kwargs["guest_os"] == "windows"

    resumed = await commands.on_resume_workspace(
        {"task_id": "t", "workspace_id": str(workspace_id)}
    )
    assert "windows_activation_result" not in resumed.data
    activation.activate.assert_awaited_once()
