from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from src.models import WorkspaceInfo
from src.runtime.base import (
    PtyHandle,
    RuntimeBackend,
    RuntimeStatus,
    RuntimeWorkspaceInfo,
    WorkspaceConfig,
)
from src.services.workspace_lifecycle import WorkspaceLifecycle
from src.services.workspace_registry import WorkspaceRegistry


class _RuntimeStub(RuntimeBackend):
    """Concrete test runtime with the base reachability implementation."""

    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.exec_calls: list[tuple[str, list[str]]] = []
        self.destructive_calls: list[str] = []

    @property
    def runtime_type(self) -> str:
        return "test"

    async def create_workspace(self, config: WorkspaceConfig) -> str:
        raise NotImplementedError

    async def stop_workspace(self, instance_id: str) -> None:
        self.destructive_calls.append("stop")

    async def start_workspace(self, instance_id: str) -> None:
        self.destructive_calls.append("start")

    async def remove_workspace(self, instance_id: str) -> None:
        self.destructive_calls.append("remove")

    async def workspace_exists(self, instance_id: str) -> bool:
        raise NotImplementedError

    async def get_workspace_status(self, instance_id: str) -> RuntimeStatus:
        raise NotImplementedError

    async def list_workspaces(self) -> list[RuntimeWorkspaceInfo]:
        raise NotImplementedError

    async def exec_command_wait(
        self,
        instance_id: str,
        command: list[str],
        workdir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        self.exec_calls.append((instance_id, command))
        return self.exit_code, ""

    async def put_archive(self, instance_id: str, path: str, data: bytes) -> None:
        raise NotImplementedError

    async def exec_pty(
        self,
        instance_id: str,
        cols: int = 80,
        rows: int = 24,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
        command: list[str] | None = None,
    ) -> PtyHandle:
        raise NotImplementedError

    async def pty_read(self, handle: PtyHandle, size: int = 4096) -> bytes:
        raise NotImplementedError

    async def pty_write(self, handle: PtyHandle, data: bytes) -> None:
        raise NotImplementedError

    async def pty_resize(self, handle: PtyHandle, cols: int, rows: int) -> None:
        raise NotImplementedError

    async def pty_close(self, handle: PtyHandle) -> None:
        raise NotImplementedError


class _ReachabilityOverrideRuntime(_RuntimeStub):
    def __init__(self, reachable: bool = True) -> None:
        super().__init__()
        self.reachable = reachable
        self.reachability_calls: list[str] = []
        self.reachability_error: BaseException | None = None

    async def check_reachable(self, instance_id: str) -> bool:
        self.reachability_calls.append(instance_id)
        if self.reachability_error is not None:
            raise self.reachability_error
        return self.reachable


def _lifecycle(
    runtime: RuntimeBackend | None,
    *,
    instance_id: str = "instance-1",
) -> tuple[WorkspaceLifecycle, WorkspaceInfo, uuid.UUID]:
    workspace_id = uuid.uuid4()
    info = WorkspaceInfo(
        workspace_id=workspace_id,
        instance_id=instance_id,
        status="running",
    )
    registry = WorkspaceRegistry(runtime=runtime)
    registry._cache[workspace_id] = info
    settings = SimpleNamespace(ssh_health_check_interval=0, ssh_unreachable_timeout=0)
    return WorkspaceLifecycle(registry, settings, runtime), info, workspace_id


@pytest.mark.parametrize(
    ("exit_code", "expected"), [(0, True), (1, False), (127, False)]
)
async def test_default_check_reachable_uses_echo_exit_status(
    exit_code: int, expected: bool
) -> None:
    runtime = _RuntimeStub(exit_code=exit_code)

    assert await runtime.check_reachable("instance-1") is expected
    assert runtime.exec_calls == [("instance-1", ["echo", "ok"])]


async def test_lifecycle_uses_runtime_reachability_override_not_business_exec() -> None:
    runtime = _ReachabilityOverrideRuntime(reachable=False)
    lifecycle, info, workspace_id = _lifecycle(runtime)

    assert await lifecycle._check_workspace_reachable(workspace_id, info) is False
    assert runtime.reachability_calls == ["instance-1"]
    assert runtime.exec_calls == []


async def test_lifecycle_treats_probe_exception_as_unreachable() -> None:
    runtime = _ReachabilityOverrideRuntime()
    runtime.reachability_error = OSError("probe failed")
    lifecycle, info, workspace_id = _lifecycle(runtime)

    assert await lifecycle._check_workspace_reachable(workspace_id, info) is False


async def test_lifecycle_propagates_probe_cancellation() -> None:
    runtime = _ReachabilityOverrideRuntime()
    runtime.reachability_error = asyncio.CancelledError()
    lifecycle, info, workspace_id = _lifecycle(runtime)

    with pytest.raises(asyncio.CancelledError):
        await lifecycle._check_workspace_reachable(workspace_id, info)


@pytest.mark.parametrize(
    ("runtime_available", "instance_id"),
    [(False, "instance-1"), (True, "")],
)
async def test_lifecycle_assumes_reachable_without_runtime_or_instance(
    runtime_available: bool, instance_id: str
) -> None:
    runtime = _ReachabilityOverrideRuntime()
    lifecycle, info, workspace_id = _lifecycle(runtime, instance_id=instance_id)
    if not runtime_available:
        lifecycle._runtime = None

    assert await lifecycle._check_workspace_reachable(workspace_id, info) is True
    assert runtime.reachability_calls == []


async def test_health_loop_reports_unreachable_without_destructive_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _ReachabilityOverrideRuntime(reachable=False)
    lifecycle, _, workspace_id = _lifecycle(runtime)
    slept = False

    async def stop_after_one_iteration(delay: float) -> None:
        nonlocal slept
        if slept:
            raise asyncio.CancelledError
        slept = True

    monkeypatch.setattr(
        "src.services.workspace_lifecycle.asyncio.sleep", stop_after_one_iteration
    )

    await lifecycle.run_health_check_loop()

    assert workspace_id in lifecycle._unreachable_since
    assert runtime.reachability_calls == ["instance-1"]
    assert runtime.destructive_calls == []
    assert runtime.exec_calls == []
