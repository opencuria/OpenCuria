"""Tests for RunnerWorkspaceAccessor.process_* against RunnerService."""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest

from apps.harness.access.runner_accessor import (
    RunnerAccessorError,
    RunnerWorkspaceAccessor,
)

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")

WORKSPACE_ID = str(uuid.uuid4())
PROCESS_ID = uuid.uuid4()


def _record(**overrides):
    base = {
        "id": PROCESS_ID,
        "workspace_id": uuid.UUID(WORKSPACE_ID),
        "name": "web",
        "command": "python server.py",
        "workdir": "/workspace",
        "pid": 4242,
        "log_path": "/workspace/.opencuria/processes/x.log",
        "status": "running",
        "exit_code": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _FakeService:
    """Minimal RunnerService double recording calls."""

    def __init__(self, **methods):
        self.calls: list[tuple] = []
        self._methods = methods

    async def start_process(self, workspace_id, command, **kwargs):
        self.calls.append(("start_process", workspace_id, command, kwargs))
        return self._methods["start_process"]()

    async def list_processes(self, workspace_id):
        self.calls.append(("list_processes", workspace_id))
        return self._methods["list_processes"]()

    async def get_process(self, workspace_id, process_id):
        self.calls.append(("get_process", workspace_id, process_id))
        return self._methods["get_process"]()

    async def stop_process(self, workspace_id, process_id, **kwargs):
        self.calls.append(("stop_process", workspace_id, process_id, kwargs))
        return self._methods["stop_process"]()


async def _emit(event: str, payload: dict) -> None:
    raise AssertionError("process_* must not use the socket transport")


def _accessor(service: _FakeService) -> RunnerWorkspaceAccessor:
    accessor = RunnerWorkspaceAccessor(WORKSPACE_ID, emit=_emit)
    accessor._runner_service = lambda: service  # type: ignore[method-assign]
    return accessor


async def test_process_start_calls_service_and_serializes() -> None:
    """process_start validates, sanitizes, and serializes the record."""
    service = _FakeService(
        start_process=lambda: _record(),
        list_processes=lambda: [],
        get_process=lambda: _record(),
        stop_process=lambda: _record(status="killed", exit_code=0),
    )
    accessor = _accessor(service)
    result = await accessor.process_start(
        "python server.py", workdir="/workspace", env={"A": "b"}, name="web"
    )
    assert result["process_id"] == str(PROCESS_ID)
    assert result["status"] == "running"
    assert result["pid"] == 4242
    assert result["log_path"].endswith(".log")
    name, workspace_id, command, kwargs = service.calls[0]
    assert name == "start_process"
    assert workspace_id == uuid.UUID(WORKSPACE_ID)
    assert command == "python server.py"
    assert kwargs["workdir"] == "/workspace"


async def test_process_start_rejects_bad_input() -> None:
    """Empty commands and escaping workdirs raise ValueError."""
    service = _FakeService(
        start_process=lambda: _record(),
        list_processes=lambda: [],
        get_process=lambda: _record(),
        stop_process=lambda: _record(),
    )
    accessor = _accessor(service)
    with pytest.raises(ValueError, match="command must not be empty"):
        await accessor.process_start("   ")
    with pytest.raises(ValueError, match="under /workspace"):
        await accessor.process_start("sleep 1", workdir="/etc")
    assert service.calls == []


async def test_process_list_get_stop_roundtrip() -> None:
    """list/get/stop map service records to JSON dicts."""
    service = _FakeService(
        start_process=lambda: _record(),
        list_processes=lambda: [_record()],
        get_process=lambda: _record(),
        stop_process=lambda: _record(status="killed", exit_code=0),
    )
    accessor = _accessor(service)
    listed = await accessor.process_list()
    assert len(listed) == 1 and listed[0]["process_id"] == str(PROCESS_ID)
    gotten = await accessor.process_get(str(PROCESS_ID))
    assert gotten["command"] == "python server.py"
    stopped = await accessor.process_stop(str(PROCESS_ID), force=True)
    assert stopped["status"] == "killed"
    assert service.calls[-1][0] == "stop_process"
    assert service.calls[-1][3] == {"force": True}


async def test_process_get_rejects_invalid_uuid() -> None:
    """Malformed process ids raise ValueError before any service call."""
    service = _FakeService(
        start_process=lambda: _record(),
        list_processes=lambda: [],
        get_process=lambda: _record(),
        stop_process=lambda: _record(),
    )
    accessor = _accessor(service)
    with pytest.raises(ValueError, match="Invalid process_id"):
        await accessor.process_get("not-a-uuid")
    with pytest.raises(ValueError, match="Invalid process_id"):
        await accessor.process_stop("not-a-uuid")
    assert service.calls == []


async def test_process_errors_translate_to_accessor_error() -> None:
    """Service conflicts/offline map to RunnerAccessorError."""
    from apps.runners.exceptions import RunnerOfflineError
    from common.exceptions import ConflictError

    offline = _FakeService(
        start_process=lambda: (_ for _ in ()).throw(
            RunnerOfflineError("r1")
        ),
        list_processes=lambda: (_ for _ in ()).throw(ConflictError("busy")),
        get_process=lambda: _record(),
        stop_process=lambda: _record(),
    )
    accessor = _accessor(offline)
    with pytest.raises(RunnerAccessorError, match="process_start failed"):
        await accessor.process_start("sleep 1")
    with pytest.raises(RunnerAccessorError, match="process_list failed"):
        await accessor.process_list()
