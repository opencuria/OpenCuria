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
        "run_count": 1,
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

    async def restart_process(self, workspace_id, process_id, **kwargs):
        self.calls.append(("restart_process", workspace_id, process_id, kwargs))
        return self._methods["restart_process"]()

    async def delete_process(self, workspace_id, process_id):
        self.calls.append(("delete_process", workspace_id, process_id))
        return self._methods["delete_process"]()


def _service(**overrides) -> _FakeService:
    defaults = {
        "start_process": lambda: _record(),
        "list_processes": lambda: [],
        "get_process": lambda: _record(),
        "stop_process": lambda: _record(status="killed", exit_code=0),
        "restart_process": lambda: _record(run_count=2),
        "delete_process": lambda: PROCESS_ID,
    }
    defaults.update(overrides)
    return _FakeService(**defaults)


async def _emit(event: str, payload: dict) -> None:
    raise AssertionError("process_* must not use the socket transport")


def _accessor(service: _FakeService) -> RunnerWorkspaceAccessor:
    accessor = RunnerWorkspaceAccessor(WORKSPACE_ID, emit=_emit)
    accessor._runner_service = lambda: service  # type: ignore[method-assign]
    return accessor


async def test_process_start_calls_service_and_serializes() -> None:
    """process_start validates, sanitizes, and serializes the record."""
    service = _service()
    accessor = _accessor(service)
    result = await accessor.process_start(
        "python server.py", workdir="/workspace", env={"A": "b"}, name="web"
    )
    assert result["process_id"] == str(PROCESS_ID)
    assert result["status"] == "running"
    assert result["pid"] == 4242
    assert result["log_path"].endswith(".log")
    assert result["run_count"] == 1
    name, workspace_id, command, kwargs = service.calls[0]
    assert name == "start_process"
    assert workspace_id == uuid.UUID(WORKSPACE_ID)
    assert command == "python server.py"
    assert kwargs["workdir"] == "/workspace"


async def test_process_start_rejects_bad_input() -> None:
    """Empty commands and invalid workdirs raise ValueError."""
    service = _service()
    accessor = _accessor(service)
    with pytest.raises(ValueError, match="command must not be empty"):
        await accessor.process_start("   ", name="web")
    with pytest.raises(ValueError, match="Invalid workdir"):
        await accessor.process_start("sleep 1", workdir="/tmp\n", name="web")
    assert service.calls == []


async def test_process_start_allows_external_workdir() -> None:
    """workdir outside /workspace is forwarded to the service."""
    service = _service()
    accessor = _accessor(service)
    await accessor.process_start("sleep 1", workdir="/tmp", name="web")
    assert service.calls[0][3]["workdir"] == "/tmp"


async def test_process_start_empty_name_maps_to_service_value_error() -> None:
    """An empty name reaches the service, whose ValueError is mapped."""
    service = _service(
        start_process=lambda: (_ for _ in ()).throw(
            ValueError("name must not be empty")
        ),
    )
    accessor = _accessor(service)
    with pytest.raises(RunnerAccessorError, match="process_start failed"):
        await accessor.process_start("sleep 1", name="  ")


async def test_process_list_get_stop_roundtrip() -> None:
    """list/get/stop map service records to JSON dicts."""
    service = _service(list_processes=lambda: [_record()])
    accessor = _accessor(service)
    listed = await accessor.process_list()
    assert len(listed) == 1 and listed[0]["process_id"] == str(PROCESS_ID)
    gotten = await accessor.process_get(str(PROCESS_ID))
    assert gotten["command"] == "python server.py"
    stopped = await accessor.process_stop(str(PROCESS_ID))
    assert stopped["status"] == "killed"
    assert service.calls[-1][0] == "stop_process"
    assert service.calls[-1][3] == {}


async def test_process_get_stop_accept_name() -> None:
    """get/stop forward names (not UUIDs) to the service for resolution."""
    service = _service()
    accessor = _accessor(service)
    gotten = await accessor.process_get("web")
    assert gotten["command"] == "python server.py"
    assert service.calls[0][0] == "get_process"
    assert service.calls[0][2] == "web"
    stopped = await accessor.process_stop("web")
    assert stopped["status"] == "killed"
    assert service.calls[-1][0] == "stop_process"
    assert service.calls[-1][2] == "web"


async def test_process_get_stop_reject_empty() -> None:
    """Blank ids raise before any service call."""
    service = _service()
    accessor = _accessor(service)
    with pytest.raises(ValueError, match="process_id must not be empty"):
        await accessor.process_get("   ")
    with pytest.raises(ValueError, match="process_id must not be empty"):
        await accessor.process_stop("   ")
    assert service.calls == []


async def test_process_restart_delete_roundtrip() -> None:
    """restart/delete forward UUID-or-name keys and serialize results."""
    service = _service()
    accessor = _accessor(service)
    restarted = await accessor.process_restart(str(PROCESS_ID))
    assert restarted["process_id"] == str(PROCESS_ID)
    assert restarted["run_count"] == 2
    assert service.calls[0][0] == "restart_process"
    assert service.calls[0][2] == str(PROCESS_ID)

    by_name = await accessor.process_restart("web")
    assert by_name["run_count"] == 2
    assert service.calls[1][2] == "web"

    deleted = await accessor.process_delete(str(PROCESS_ID))
    assert deleted == {"process_id": str(PROCESS_ID), "deleted": True}
    assert service.calls[2][0] == "delete_process"

    deleted_by_name = await accessor.process_delete("web")
    assert deleted_by_name == {"process_id": str(PROCESS_ID), "deleted": True}
    assert service.calls[3][2] == "web"

    with pytest.raises(ValueError, match="process_id must not be empty"):
        await accessor.process_restart("  ")
    with pytest.raises(ValueError, match="process_id must not be empty"):
        await accessor.process_delete("  ")


async def test_process_restart_delete_errors_translate() -> None:
    """Service failures for restart/delete map to RunnerAccessorError."""
    from apps.runners.exceptions import RunnerOfflineError
    from common.exceptions import ConflictError

    service = _service(
        restart_process=lambda: (_ for _ in ()).throw(
            RunnerOfflineError("r1")
        ),
        delete_process=lambda: (_ for _ in ()).throw(ConflictError("busy")),
    )
    accessor = _accessor(service)
    with pytest.raises(RunnerAccessorError, match="process_restart failed"):
        await accessor.process_restart("web")
    with pytest.raises(RunnerAccessorError, match="process_delete failed"):
        await accessor.process_delete("web")


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
        restart_process=lambda: _record(),
        delete_process=lambda: PROCESS_ID,
    )
    accessor = _accessor(offline)
    with pytest.raises(RunnerAccessorError, match="process_start failed"):
        await accessor.process_start("sleep 1", name="web")
    with pytest.raises(RunnerAccessorError, match="process_list failed"):
        await accessor.process_list()
