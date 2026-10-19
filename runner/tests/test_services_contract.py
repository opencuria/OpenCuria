"""Focused integration contracts for the composed runner managers."""

from __future__ import annotations

import base64
import io
import tarfile
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from src.config import RunnerSettings
from src.models import DesktopSession, WorkspaceInfo
from src.runtime.base import PtyHandle
from src.service import WorkspaceService


def _make_service(runtimes: dict | None = None) -> WorkspaceService:
    return WorkspaceService(
        runtimes={} if runtimes is None else runtimes,
        settings=RunnerSettings(),
    )


def _cache_workspace(
    service: WorkspaceService,
    workspace_id: uuid.UUID,
    *,
    instance_id: str = "instance-1",
    runtime_type: str = "docker",
) -> WorkspaceInfo:
    info = WorkspaceInfo(
        workspace_id=workspace_id,
        instance_id=instance_id,
        status="running",
        runtime_type=runtime_type,
    )
    service.registry._cache[workspace_id] = info
    return info


async def test_workspace_context_resolution_errors() -> None:
    from src.services.exec_kernel import WorkspaceContext

    service = _make_service(runtimes={"docker": object()})
    workspace_id = uuid.uuid4()

    with pytest.raises(ValueError, match="not found"):
        await WorkspaceContext.resolve(
            service.registry.get_cached, service.registry.get_runtime, workspace_id
        )

    _cache_workspace(service, workspace_id, runtime_type="qemu")
    with pytest.raises(RuntimeError, match="not available"):
        await WorkspaceContext.resolve(
            service.registry.get_cached, service.registry.get_runtime, workspace_id
        )

    missing_instance = uuid.uuid4()
    _cache_workspace(service, missing_instance, instance_id="")
    with pytest.raises(RuntimeError, match="no instance assigned"):
        await WorkspaceContext.resolve(
            service.registry.get_cached,
            service.registry.get_runtime,
            missing_instance,
        )
    with pytest.raises(RuntimeError, match="no instance assigned"):
        await service.lifecycle.stop_workspace(missing_instance)


async def test_keyed_lock_maps_retain_manager_locks() -> None:
    service = _make_service()
    workspace_id = uuid.uuid4()
    background = service.background
    background_lock = await background._background_start_lock(workspace_id, "proc")
    assert (
        await background._background_start_lock(workspace_id, "proc") is background_lock
    )
    assert (
        background._background_start_lock_map.locks[(workspace_id, "proc")]
        is background_lock
    )
    assert (
        await background._background_start_lock(workspace_id, "other")
        is not background_lock
    )

    desktop = service.desktop
    desktop_lock = await desktop._desktop_lock(workspace_id)
    assert await desktop._desktop_lock(workspace_id) is desktop_lock
    assert desktop._desktop_lock_map.locks[workspace_id] is desktop_lock
    assert await desktop._desktop_lock(uuid.uuid4()) is not desktop_lock


class _FakePtyRuntime:
    def __init__(self) -> None:
        self.exec_kwargs: dict | None = None
        self.written: list[bytes] = []
        self.resized: list[tuple[int, int]] = []
        self.closed: list[PtyHandle] = []
        self._read = False

    async def workspace_exists(self, instance_id: str) -> bool:
        return True

    async def exec_pty(self, instance_id: str, **kwargs) -> PtyHandle:
        self.exec_kwargs = {"instance_id": instance_id, **kwargs}
        return PtyHandle(instance_id=instance_id, handle=object())

    async def pty_read(self, handle: PtyHandle, size: int = 4096) -> bytes:
        if self._read:
            return b""
        self._read = True
        return b"terminal output"

    async def pty_write(self, handle: PtyHandle, data: bytes) -> None:
        self.written.append(bytes(data))

    async def pty_resize(self, handle: PtyHandle, cols: int, rows: int) -> None:
        self.resized.append((cols, rows))

    async def pty_close(self, handle: PtyHandle) -> None:
        handle.closed = True
        self.closed.append(handle)


async def test_terminal_manager_pty_lifecycle_and_unknown_id_error() -> None:
    runtime = _FakePtyRuntime()
    service = _make_service(runtimes={"docker": runtime})
    workspace_id = uuid.uuid4()
    _cache_workspace(service, workspace_id)
    terminals = service.terminal_manager

    terminal_id = await terminals.start_terminal(workspace_id, cols=100, rows=40)
    assert runtime.exec_kwargs is not None
    assert runtime.exec_kwargs["instance_id"] == "instance-1"
    assert runtime.exec_kwargs["cols"] == 100
    assert runtime.exec_kwargs["rows"] == 40
    assert runtime.exec_kwargs["command"][0] == "/bin/bash"

    await terminals.write_terminal(terminal_id, b"ls\n")
    await terminals.resize_terminal(terminal_id, 120, 50)
    assert runtime.written == [b"ls\n"]
    assert runtime.resized == [(120, 50)]
    assert [chunk async for chunk in terminals.read_terminal(terminal_id)] == [
        b"terminal output"
    ]

    with pytest.raises(ValueError, match="not found"):
        await terminals.write_terminal("missing", b"x")
    await terminals.close_terminal(terminal_id)
    assert terminal_id not in terminals._terminals
    assert len(runtime.closed) == 1 and runtime.closed[0].closed


@pytest.mark.parametrize(
    ("runtime_type", "runtimes", "error"),
    [
        ("other", {}, "Unsupported runtime_type"),
        ("qemu", {}, "Runtime 'qemu' is not enabled"),
        ("qemu", {"qemu": object()}, "does not support image builds"),
    ],
)
async def test_image_build_rejects_unsupported_and_unavailable_runtimes(
    runtime_type: str, runtimes: dict, error: str
) -> None:
    with pytest.raises(RuntimeError, match=error):
        await _make_service(runtimes).images.build_image(
            runtime_type=runtime_type,
            build_job_id="job",
            image_path="/tmp/image.qcow2",
        )


class _ComposedRuntime:
    supports_image_artifacts = True

    def __init__(self) -> None:
        self.exec_calls: list[tuple[str, list[str], str | None, dict | None]] = []
        self.clone_calls: list[tuple[str, str]] = []
        self.archives: list[tuple[str, str, bytes]] = []
        self.harness_output = (
            "OPENCURIA_STDOUT\n"
            + base64.b64encode(b"desktop output").decode()
            + "\nOPENCURIA_STDERR\n"
            + base64.b64encode(b"desktop error").decode()
            + "\nEXIT:0"
        )

    async def exec_command_wait(
        self, instance_id: str, command, workdir=None, env=None
    ) -> tuple[int, str]:
        self.exec_calls.append((instance_id, list(command), workdir, env))
        return 0, self.harness_output

    async def create_workspace_from_image_artifact(
        self, artifact_id: str, workspace_id: str, **kwargs
    ) -> str:
        self.clone_calls.append((artifact_id, workspace_id))
        return "cloned-instance"

    async def put_archive(self, instance_id: str, path: str, archive: bytes) -> None:
        self.archives.append((instance_id, path, archive))


async def test_composer_wires_desktop_harness_and_image_credentials() -> None:
    runtime = _ComposedRuntime()
    runtimes = {"docker": runtime}
    service = _make_service(runtimes=runtimes)
    source_id = uuid.uuid4()
    _cache_workspace(service, source_id)

    # These callbacks are injected by WorkspaceService's composition root;
    # exercise them through their consumers, not facade delegates.
    assert service.desktop._exec_harness_command.__self__ is service.harness
    assert service.images._credentials_injector.__self__ is service.credentials

    result = await service.desktop.desktop_action(
        source_id, "execute", {"code": "print('hello')"}
    )
    assert result == {
        "ok": True,
        "exit_code": 0,
        "stdout": "desktop output",
        "stderr": "desktop error",
    }
    harness_calls = [
        call for call in runtime.exec_calls if "OPENCURIA_STDOUT" in " ".join(call[1])
    ]
    assert len(harness_calls) == 1
    assert "python3" in " ".join(harness_calls[0][1])

    clone_id = uuid.uuid4()
    (
        cloned,
        credentials_present,
    ) = await service.images.create_workspace_from_image_artifact(
        "artifact-1", clone_id, "docker", env_vars={"TOKEN": "secret"}
    )
    assert (cloned, credentials_present) == (clone_id, True)
    assert runtime.clone_calls == [("artifact-1", str(clone_id))]
    info = service.registry.get_cached(clone_id)
    assert (info.instance_id, info.status, info.credentials_present) == (
        "cloned-instance",
        "running",
        True,
    )
    assert len(runtime.archives) == 1
    with tarfile.open(fileobj=io.BytesIO(runtime.archives[0][2])) as archive:
        env_script = archive.extractfile("env.sh")
        assert env_script is not None
        assert b"export TOKEN=secret" in env_script.read()


async def test_remove_workspace_fans_out_to_canonical_managers() -> None:
    runtime = SimpleNamespace(remove_workspace=AsyncMock())
    service = _make_service(runtimes={"docker": runtime})
    workspace_id = uuid.uuid4()
    _cache_workspace(service, workspace_id)

    service.streams.close_workspace_streams = AsyncMock(return_value=1)
    service.background._kill_all_background_processes = AsyncMock()
    service.desktop._interrupt_desktop_recordings = AsyncMock()
    service.desktop._desktop_sessions[workspace_id] = DesktopSession(
        workspace_id=workspace_id, instance_id="instance-1"
    )
    recording = (workspace_id, "run-1")
    service.desktop._desktop_recordings[recording] = (
        123,
        "/workspace/recording.mp4",
    )

    await service.remove_workspace(workspace_id)

    service.streams.close_workspace_streams.assert_awaited_once_with(
        workspace_id, reason="remove"
    )
    service.background._kill_all_background_processes.assert_awaited_once_with(
        workspace_id, reason="remove"
    )
    service.desktop._interrupt_desktop_recordings.assert_awaited_once_with(workspace_id)
    runtime.remove_workspace.assert_awaited_once_with(str(workspace_id))
    assert workspace_id not in service.registry._cache
    assert workspace_id not in service.desktop._desktop_sessions
    assert recording not in service.desktop._desktop_recordings
