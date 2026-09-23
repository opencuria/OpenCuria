"""Step 0 contract tests: pin preamble + lock-map shapes before extraction.

These tests document invariants in ``src.service.WorkspaceService`` that
the ``src.services`` extraction (Steps 1-8) must preserve:

- workspace preamble errors: ``_get_cached`` raises ``ValueError`` with
  "not found", ``_get_runtime`` raises ``RuntimeError`` for an unknown
  runtime, and lifecycle entry points raise ``RuntimeError`` when the
  cached workspace has no ``instance_id`` assigned.
- keyed lock maps (``_background_start_locks`` / ``_git_locks`` /
  ``_desktop_locks``) use never-evict semantics: repeated lookups for
  the same key return the identical lock object.
- Step 1 parity: pure/stateless helpers extracted into ``src.services``
  (``exec_kernel``, ``files``, ``sessions.xdotool``,
  ``sessions.streams``) agree exactly with the ``src.service``
  delegates/re-exports (same values, same results, same error messages).
- Step 2 parity: leaf managers (``sessions.terminals.TerminalManager``,
  ``images.ImageManager``) own terminal + image state; the
  ``WorkspaceService`` facade delegates (same names/signatures/messages)
  and exposes them as ``terminal_manager`` / ``images``; websocket
  terminal + image handlers call the managers directly.
"""

from __future__ import annotations

import io
import tarfile
import uuid

import pytest

from src.config import RunnerSettings
from src.models import WorkspaceInfo
from src.service import WorkspaceService


def _make_service(runtimes: dict | None = None) -> WorkspaceService:
    if runtimes is None:
        runtimes = {}
    return WorkspaceService(runtimes=runtimes, settings=RunnerSettings())


def test_workspace_context_preamble_error_shapes() -> None:
    svc = _make_service()
    unknown_id = uuid.uuid4()

    with pytest.raises(ValueError, match="not found"):
        svc._get_cached(unknown_id)

    with pytest.raises(ValueError, match="not found"):
        svc._get_runtime(unknown_id)

    svc._cache[unknown_id] = WorkspaceInfo(
        workspace_id=unknown_id,
        instance_id="instance-1",
        status="running",
        runtime_type="docker",
    )
    with pytest.raises(RuntimeError, match="not available"):
        svc._get_runtime(unknown_id)


async def test_workspace_missing_instance_id_runtime_invariant() -> None:
    svc = _make_service(runtimes={"docker": object()})
    workspace_id = uuid.uuid4()
    svc._cache[workspace_id] = WorkspaceInfo(
        workspace_id=workspace_id,
        instance_id="",
        status="running",
        runtime_type="docker",
    )
    with pytest.raises(RuntimeError, match="no instance assigned"):
        await svc.stop_workspace(workspace_id)


async def test_keyed_lock_map_never_evict_semantics() -> None:
    svc = _make_service()
    workspace_id = uuid.uuid4()

    first = await svc._background_start_lock(workspace_id, "proc")
    second = await svc._background_start_lock(workspace_id, "proc")
    assert first is second

    first = await svc._git_lock(workspace_id, "/workspace/repo")
    second = await svc._git_lock(workspace_id, "/workspace/repo")
    assert first is second

    first = await svc._desktop_lock(workspace_id)
    second = await svc._desktop_lock(workspace_id)
    assert first is second


# -- Step 1 parity: service delegates vs canonical services modules -------


def test_step1_exec_kernel_parity() -> None:
    """``_normalise_command_args``/sanitizers delegate to exec_kernel."""
    import src.service as service_module
    from src.services import exec_kernel

    svc = _make_service()
    cases: list = [
        ["ls", "-la"],
        ["echo", "hi", "|", "grep", "h"],
        "echo hi",
        ["bash", "-lc", "echo hi"],
        [
            "ssh-keyscan",
            "-t",
            "ed25519,rsa",
            "github.com",
            ">>",
            "/root/.ssh/known_hosts",
            "2>/dev/null",
        ],
        ["echo", "a", "&&", "echo", "b"],
    ]
    for raw in cases:
        assert svc._normalise_command_args(raw) == exec_kernel.normalise_command_args(
            raw
        )
    # module-level sentinels are identical objects (re-export, not copy).
    assert service_module._SHELL_OPERATOR_TOKENS is exec_kernel._SHELL_OPERATOR_TOKENS
    assert service_module._REDIRECTION_RE is exec_kernel._REDIRECTION_RE

    for path in ("/workspace", "/workspace/a.txt", "/workspace/d/../e"):
        assert svc._sanitize_path(path) == exec_kernel.sanitize_path(path)
    with pytest.raises(ValueError) as exc_service:
        svc._sanitize_path("/etc/passwd")
    with pytest.raises(ValueError) as exc_kernel:
        exec_kernel.sanitize_path("/etc/passwd")
    assert str(exc_service.value) == str(exc_kernel.value)

    for workdir in ("/tmp", "", "src", "/workspace"):
        assert svc._sanitize_exec_workdir(workdir) == (
            exec_kernel.sanitize_exec_workdir(workdir)
        )
    with pytest.raises(ValueError) as exc_service:
        svc._sanitize_exec_workdir("/tmp\n")
    with pytest.raises(ValueError) as exc_kernel:
        exec_kernel.sanitize_exec_workdir("/tmp\n")
    assert str(exc_service.value) == str(exc_kernel.value)

    assert svc._sanitize_filename("a.txt") == exec_kernel.sanitize_filename("a.txt")
    with pytest.raises(ValueError) as exc_service:
        svc._sanitize_filename("a/b.txt")
    with pytest.raises(ValueError) as exc_kernel:
        exec_kernel.sanitize_filename("a/b.txt")
    assert str(exc_service.value) == str(exc_kernel.value)


def test_step1_files_parity() -> None:
    """find/tar helpers delegate to ``src.services.files``."""
    import src.service as service_module
    from src.services import files as files_module

    svc = _make_service()
    assert (
        service_module.FIND_FILES_DEFAULT_LIMIT
        == files_module.FIND_FILES_DEFAULT_LIMIT
        == 50
    )
    assert service_module.FIND_FILES_PRUNE_NAMES == files_module.FIND_FILES_PRUNE_NAMES
    assert (
        service_module._FIND_FILES_QUERY_RE is files_module._FIND_FILES_QUERY_RE
    )
    assert (
        service_module._FIND_FILES_SUCCESS_EXIT_CODES
        == files_module._FIND_FILES_SUCCESS_EXIT_CODES
    )

    assert svc.sanitize_find_query("src/a.ts") == files_module.sanitize_find_query(
        "src/a.ts"
    )
    assert svc.sanitize_find_query("") == files_module.sanitize_find_query("") == ""
    with pytest.raises(ValueError) as exc_service:
        svc.sanitize_find_query("../etc/passwd")
    with pytest.raises(ValueError) as exc_kernel:
        files_module.sanitize_find_query("../etc/passwd")
    assert str(exc_service.value) == str(exc_kernel.value)

    assert svc.build_find_files_command(
        "src/a.ts", 50
    ) == files_module.build_find_files_command("src/a.ts", 50)
    assert svc.build_find_files_command(
        "", 8
    ) == files_module.build_find_files_command("", 8)

    assert svc._build_single_file_tar(
        "a.txt", b"hi"
    ) == files_module.build_single_file_tar("a.txt", b"hi")
    assert svc._build_tar_entries(
        [("a.txt", b"hi", 0o644)]
    ) == files_module.build_tar_entries([("a.txt", b"hi", 0o644)])

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo(name="a.txt")
        info.size = 2
        tar.addfile(info, io.BytesIO(b"hi"))
    payload = buffer.getvalue()
    assert svc._convert_archive_to_tar(
        payload
    ) == files_module.convert_archive_to_tar(payload)


def test_step1_xdotool_parity() -> None:
    """xdotool helpers delegate to ``src.services.sessions.xdotool``."""
    import src.service as service_module
    from src.services.sessions import xdotool as xdotool_module

    assert service_module._XDOTOOL_KEY_ALIASES is xdotool_module._XDOTOOL_KEY_ALIASES
    assert (
        service_module._XDOTOOL_MODIFIER_ALIASES
        is xdotool_module._XDOTOOL_MODIFIER_ALIASES
    )
    assert (
        service_module._XDOTOOL_KEY_FAILURE_MARKERS
        is xdotool_module._XDOTOOL_KEY_FAILURE_MARKERS
    )
    assert (
        service_module._XDOTOOL_FUNCTION_KEY_RE
        is xdotool_module._XDOTOOL_FUNCTION_KEY_RE
    )

    assert service_module._collapse_xdotool_token(
        " Page-Up "
    ) == xdotool_module._collapse_xdotool_token(" Page-Up ")
    assert service_module._normalize_xdotool_token(
        "enter"
    ) == xdotool_module._normalize_xdotool_token("enter") == "Return"
    assert service_module._normalize_xdotool_key_combo(
        "c", ["ctrl"]
    ) == xdotool_module._normalize_xdotool_key_combo("c", ["ctrl"])
    assert service_module._xdotool_type_command(
        "hi"
    ) == xdotool_module._xdotool_type_command("hi")
    assert service_module._xdotool_key_failed(
        1, ""
    ) == xdotool_module._xdotool_key_failed(1, "") is True
    with pytest.raises(ValueError) as exc_service:
        service_module._normalize_xdotool_token("  ")
    with pytest.raises(ValueError) as exc_canonical:
        xdotool_module._normalize_xdotool_token("  ")
    assert str(exc_service.value) == str(exc_canonical.value)


def test_step1_streams_parity() -> None:
    """stream constants/validators delegate to sessions.streams."""
    import src.service as service_module
    from src.services.sessions import streams as streams_module

    assert (
        service_module.STREAM_MAX_PER_WORKSPACE
        == streams_module.STREAM_MAX_PER_WORKSPACE
        == 8
    )
    assert (
        service_module.STREAM_CHUNK_SIZE
        == streams_module.STREAM_CHUNK_SIZE
        == 64 * 1024
    )
    assert (
        service_module.STREAM_BLOCKED_ENV_PREFIXES
        == streams_module.STREAM_BLOCKED_ENV_PREFIXES
    )
    assert (
        service_module.STREAM_BLOCKED_ENV_EXACT
        == streams_module.STREAM_BLOCKED_ENV_EXACT
    )
    assert service_module.TCP_RELAY_CODE == streams_module.TCP_RELAY_CODE
    assert service_module._TCP_HOST_RE is streams_module._TCP_HOST_RE

    assert service_module._validate_stream_host(
        "example.com"
    ) == streams_module._validate_stream_host("example.com")
    assert service_module._validate_stream_port(
        8080
    ) == streams_module._validate_stream_port(8080) == 8080
    with pytest.raises(ValueError) as exc_service:
        service_module._validate_stream_host("bad host!")
    with pytest.raises(ValueError) as exc_canonical:
        streams_module._validate_stream_host("bad host!")
    assert str(exc_service.value) == str(exc_canonical.value)
    with pytest.raises(ValueError) as exc_service:
        service_module._validate_stream_port(0)
    with pytest.raises(ValueError) as exc_canonical:
        streams_module._validate_stream_port(0)
    assert str(exc_service.value) == str(exc_canonical.value)


# -- Step 2: terminal + image managers ---------------------------------------


class _FakePtyRuntime:
    """Minimal PTY runtime for terminal lifecycle tests."""

    def __init__(self) -> None:
        from src.runtime.base import PtyHandle

        self._pty_handle_cls = PtyHandle
        self.written: list[bytes] = []
        self.resizes: list[tuple[int, int]] = []
        self.closed: list[object] = []
        self.exec_kwargs: dict | None = None

    async def workspace_exists(self, instance_id: str) -> bool:  # noqa: ANN001
        return True

    async def exec_pty(self, instance_id, cols=80, rows=24, workdir=None, env=None, command=None):  # noqa: ANN001
        self.exec_kwargs = {
            "instance_id": instance_id,
            "cols": cols,
            "rows": rows,
            "workdir": workdir,
            "env": env,
            "command": command,
        }
        return self._pty_handle_cls(instance_id=instance_id, handle=object())

    async def pty_read(self, handle, size: int = 4096) -> bytes:  # noqa: ANN001
        if getattr(handle, "_served", False):
            return b""
        handle._served = True
        return b"hello"

    async def pty_write(self, handle, data: bytes) -> None:  # noqa: ANN001
        self.written.append(bytes(data))

    async def pty_resize(self, handle, cols: int, rows: int) -> None:  # noqa: ANN001
        self.resizes.append((cols, rows))

    async def pty_close(self, handle) -> None:  # noqa: ANN001
        handle.closed = True
        self.closed.append(handle)


async def test_step2_terminal_lifecycle_via_facade_and_manager() -> None:
    """Terminal start/write/resize/read/close works through both facade and manager.

    Uses a fake PTY runtime (no docker needed). Also pins:
    ``service._terminals`` is a live alias onto the manager dict, and the
    websocket terminal handlers call the manager (facade delegates agree).
    """
    import src.service as service_module
    from src.services.sessions import terminals as terminals_module

    assert service_module.TerminalSession is terminals_module.TerminalSession

    runtime = _FakePtyRuntime()
    svc = _make_service(runtimes={"docker": runtime})
    workspace_id = uuid.uuid4()
    svc._cache[workspace_id] = WorkspaceInfo(
        workspace_id=workspace_id,
        instance_id="instance-1",
        status="running",
        runtime_type="docker",
    )

    assert svc.terminal_manager is svc._terminals_manager
    assert svc._terminals is svc._terminals_manager._terminals

    terminal_id = await svc.start_terminal(workspace_id, cols=100, rows=40)
    # Facade delegate wrote through to the manager-owned dict.
    assert terminal_id in svc._terminals
    assert terminal_id in svc.terminal_manager._terminals
    assert isinstance(
        svc._terminals[terminal_id], terminals_module.TerminalSession
    )
    assert runtime.exec_kwargs is not None
    assert runtime.exec_kwargs["cols"] == 100
    assert runtime.exec_kwargs["rows"] == 40
    assert runtime.exec_kwargs["command"][0] == "/bin/bash"

    await svc.write_terminal(terminal_id, b"ls\n")
    assert runtime.written == [b"ls\n"]

    await svc.resize_terminal(terminal_id, 120, 50)
    assert runtime.resizes == [(120, 50)]

    chunks = [chunk async for chunk in svc.read_terminal(terminal_id)]
    assert chunks == [b"hello"]

    await svc.close_terminal(terminal_id)
    assert terminal_id not in svc._terminals
    assert terminal_id not in svc.terminal_manager._terminals
    assert len(runtime.closed) == 1

    # Unknown terminal ids keep the same error shape via facade + manager.
    # Note: read_terminal is an async generator, so the error surfaces on
    # iteration, not on the call itself.
    with pytest.raises(ValueError, match="not found"):
        [chunk async for chunk in svc.read_terminal("missing")]
    with pytest.raises(ValueError, match="not found"):
        await svc.terminal_manager.write_terminal("missing", b"x")
    with pytest.raises(ValueError, match="not found"):
        await svc.resize_terminal("missing", 80, 24)
    # Closing an unknown terminal is a no-op (both layers).
    await svc.close_terminal("missing")
    await svc.terminal_manager.close_terminal("missing")


async def test_step2_image_build_error_paths(monkeypatch) -> None:
    """Image build error paths agree via facade and manager (no docker needed)."""
    import sys

    # Simulate a missing docker SDK: ``import docker`` then raises
    # ImportError, which the manager converts to RuntimeError. (The test
    # venv has the SDK installed but no daemon, so without this the
    # docker path would fail with a daemon-connection DockerException.)
    monkeypatch.setitem(sys.modules, "docker", None)
    monkeypatch.setitem(sys.modules, "docker.errors", None)

    svc = _make_service()

    # docker SDK missing -> RuntimeError via manager and facade delegate.
    for caller in (svc.images.build_image, svc.build_image):
        with pytest.raises(RuntimeError, match="docker SDK is not available"):
            await caller(
                runtime_type="docker",
                build_job_id="job-1",
                dockerfile_content="FROM scratch",
                image_tag="example/img:test",
            )

    # Unsupported runtime_type -> RuntimeError via manager and facade.
    for caller in (svc.images.build_image, svc.build_image):
        with pytest.raises(RuntimeError, match="Unsupported runtime_type"):
            await caller(runtime_type="nope", build_job_id="job-1")

    # qemu without a qemu runtime -> "not enabled" via manager and facade.
    for caller in (svc.images.build_image, svc.build_image):
        with pytest.raises(RuntimeError, match="not enabled"):
            await caller(
                runtime_type="qemu",
                build_job_id="job-1",
                image_path="/tmp/img.qcow2",
            )


def test_step2_websocket_handlers_use_managers() -> None:
    """Websocket terminal + image handlers bypass the facade delegates."""
    import inspect

    from src.interfaces import websocket as websocket_module

    source = inspect.getsource(websocket_module.WebSocketInterface)
    for handler_snippet in (
        "self._service.terminal_manager.start_terminal",
        "self._service.terminal_manager.read_terminal",
        "self._service.terminal_manager.write_terminal",
        "self._service.terminal_manager.resize_terminal",
        "self._service.terminal_manager.close_terminal",
        "self._service.images.build_image",
        "self._service.images.create_image_artifact",
        "self._service.images.list_image_artifacts",
        "self._service.images.delete_image_reference",
        "self._service.images.delete_image_artifact",
        "self._service.images.create_workspace_from_image_artifact",
    ):
        assert handler_snippet in source, handler_snippet


# -- Step 3: stream + background managers -------------------------------------


class _FakeStreamRuntime:
    """Minimal process-stream runtime for stream cap/cleanup tests."""

    def __init__(self) -> None:
        from src.runtime.base import ProcessHandle

        self._handle_cls = ProcessHandle
        self.spawned: list = []
        self.closed: list = []

    async def spawn_process(self, instance_id, command, workdir=None, env=None):  # noqa: ANN001
        self.spawned.append(list(command))
        return self._handle_cls(instance_id=instance_id, handle=object())

    async def process_read(self, handle, stream="stdout", size=65536) -> bytes:  # noqa: ANN001
        return b""

    async def process_write(self, handle, data: bytes) -> None:  # noqa: ANN001
        return None

    async def process_write_eof(self, handle) -> None:  # noqa: ANN001
        return None

    async def process_wait(self, handle) -> int:  # noqa: ANN001
        return 0

    async def process_close(self, handle) -> None:  # noqa: ANN001
        handle.closed = True
        self.closed.append(handle)


async def test_step3_stream_cap8_and_workspace_cleanup() -> None:
    """Stream cap-8 enforced; close_workspace_streams cleans up (facade + manager).

    Fills 8 streams via a fake runtime, asserts the 9th raises, then
    closes per-workspace and asserts cleanup on both the facade alias
    and the manager dict.
    """
    import src.service as service_module
    from src.services.sessions import streams as streams_module

    assert service_module.StreamSession is streams_module.StreamSession

    runtime = _FakeStreamRuntime()
    svc = _make_service(runtimes={"docker": runtime})
    workspace_id = uuid.uuid4()
    svc._cache[workspace_id] = WorkspaceInfo(
        workspace_id=workspace_id,
        instance_id="instance-1",
        status="running",
        runtime_type="docker",
    )

    assert svc.streams is svc._streams_manager
    assert svc._streams is svc._streams_manager._streams

    for i in range(streams_module.STREAM_MAX_PER_WORKSPACE):
        await svc.stream_start_process(workspace_id, f"c{i}", ["sleep", "1"])
    assert len(svc._streams) == streams_module.STREAM_MAX_PER_WORKSPACE
    with pytest.raises(ValueError, match="too many streams"):
        await svc.stream_start_process(workspace_id, "overflow", ["sleep", "1"])
    with pytest.raises(ValueError, match="too many streams"):
        await svc.streams.stream_start_process(workspace_id, "overflow", ["x"])

    closed = await svc.close_workspace_streams(workspace_id, reason="test")
    assert closed == streams_module.STREAM_MAX_PER_WORKSPACE
    assert svc._streams == {}
    assert svc.streams._streams == {}
    assert svc._stream_count_for_workspace(workspace_id) == 0


async def test_step3_background_alias_and_status_parity() -> None:
    """Background alias identity + facade/manager status-shape parity.

    Uses a fake exec runtime (no docker needed): starts one process via
    the facade, asserts ``service._background_processes`` is the manager
    dict, then asserts facade and manager status dicts agree exactly.
    """
    import src.service as service_module
    from src.services.sessions import background as background_module
    from unittest.mock import AsyncMock

    assert service_module.BackgroundProcess is background_module.BackgroundProcess
    assert (
        service_module.BACKGROUND_PROCESS_DIR
        is background_module.BACKGROUND_PROCESS_DIR
    )

    class _FakeBgRuntime:
        def __init__(self) -> None:
            self.exec_command_wait = AsyncMock(side_effect=self._dispatch)
            self.alive: dict[int, bool] = {}
            self.next_pid = 100

        async def _dispatch(self, instance_id, command=None, workdir=None, env=None):  # noqa: ANN001
            argv = list(command or [])
            shell = argv[-1] if argv else ""
            if "setsid bash -c" in shell and "echo $!" in shell:
                pid = self.next_pid
                self.next_pid += 1
                self.alive[pid] = True
                return (0, f"{pid}\n")
            if shell.startswith("kill -0 "):
                pid = int(shell.split()[2])
                return (0, "") if self.alive.get(pid, False) else (1, "")
            if argv[:1] == ["cat"]:
                return (1, "No such file")
            return (0, "")

    runtime = _FakeBgRuntime()
    svc = _make_service(runtimes={"docker": runtime})
    workspace_id = uuid.uuid4()
    svc._cache[workspace_id] = WorkspaceInfo(
        workspace_id=workspace_id,
        instance_id="instance-1",
        status="running",
        runtime_type="docker",
    )

    assert svc.background is svc._background
    assert svc._background_processes is svc._background._background_processes

    started = await svc.start_background_process(workspace_id, "proc-1", "sleep 60")
    assert started["process_id"] == "proc-1"
    assert "proc-1" in svc._background_processes[workspace_id]
    assert "proc-1" in svc.background._background_processes[workspace_id]

    via_facade = await svc.get_background_status(workspace_id, "proc-1")
    via_manager = await svc.background.get_background_status(workspace_id, "proc-1")
    assert via_facade == via_manager == {
        "process_id": "proc-1",
        "status": "running",
        "exit_code": None,
        "pid": started["pid"],
    }

    listed_facade = await svc.list_background_processes(workspace_id)
    listed_manager = await svc.background.list_background_processes(workspace_id)
    assert listed_facade == listed_manager
    assert listed_facade[0]["process_id"] == "proc-1"
    assert listed_facade[0]["status"] == "running"


def test_step3_websocket_handlers_use_managers() -> None:
    """Websocket stream + background handlers bypass the facade delegates."""
    import inspect

    from src.interfaces import websocket as websocket_module

    source = inspect.getsource(websocket_module.WebSocketInterface)
    for handler_snippet in (
        "self._service.streams.stream_start_process",
        "self._service.streams.stream_start_tcp",
        "self._service.streams.stream_read_once",
        "self._service.streams.stream_wait",
        "self._service.streams.stream_close",
        "self._service.streams.get_stream",
        "self._service.streams.stream_write",
        "self._service.streams.stream_write_eof",
        "self._service.streams.close_all_streams",
        "self._service.background.start_background_process",
        "self._service.background.list_background_processes",
        "self._service.background.get_background_status",
        "self._service.background.stop_background_process",
        "self._service.background.verify_and_reattach_background_processes",
    ):
        assert handler_snippet in source, handler_snippet


# -- Step 4: files + harness_exec managers -------------------------------------


def test_step4_constants_canonical() -> None:
    """Size caps + credential paths are canonical in services modules."""
    import src.service as service_module
    from src.services import credentials as credentials_module
    from src.services import files as files_module

    assert (
        service_module.FILE_READ_DEFAULT_MAX_SIZE
        == files_module.FILE_READ_DEFAULT_MAX_SIZE
        == 5 * 1024 * 1024
    )
    assert (
        service_module.FILE_READ_ABSOLUTE_MAX_SIZE
        == files_module.FILE_READ_ABSOLUTE_MAX_SIZE
        == 100 * 1024 * 1024
    )
    assert (
        service_module.FILE_UPLOAD_MAX_SIZE
        == files_module.FILE_UPLOAD_MAX_SIZE
        == 10 * 1024 * 1024
    )
    assert (
        service_module.FILE_DOWNLOAD_MAX_SIZE
        == files_module.FILE_DOWNLOAD_MAX_SIZE
        == 100 * 1024 * 1024
    )
    assert (
        service_module.WORKSPACE_CREDENTIAL_ENV_FILE
        is credentials_module.WORKSPACE_CREDENTIAL_ENV_FILE
    )
    assert (
        service_module.WORKSPACE_CREDENTIAL_DIR
        is credentials_module.WORKSPACE_CREDENTIAL_DIR
    )


async def test_step4_files_list_delegation_parity() -> None:
    """list_files via facade == via FileManager (fake runtime)."""
    from src.services import files as files_module

    class _FakeListRuntime:
        def __init__(self) -> None:
            self.calls: list = []

        async def exec_command_wait(self, instance_id, command=None, workdir=None, env=None):  # noqa: ANN001
            self.calls.append(list(command or []))
            if list(command or [])[:2] == ["realpath", "-m"]:
                return 0, command[2] + "\n"
            if list(command or [])[:1] == ["find"]:
                return 0, "f\t2\t/workspace/a.txt\nd\t0\t/workspace/sub\n"
            raise AssertionError(f"unexpected command: {command!r}")

    runtime = _FakeListRuntime()
    svc = _make_service(runtimes={"docker": runtime})
    workspace_id = uuid.uuid4()
    svc._cache[workspace_id] = WorkspaceInfo(
        workspace_id=workspace_id,
        instance_id="instance-1",
        status="running",
        runtime_type="docker",
    )

    assert svc.files is svc._files_manager
    assert svc._file_read_semaphores is svc._files_manager._file_read_semaphores
    assert isinstance(svc.files, files_module.FileManager)

    via_facade = await svc.list_files(workspace_id, "/workspace")
    via_manager = await svc.files.list_files(workspace_id, "/workspace")
    assert via_facade == via_manager
    assert via_facade[0] == {
        "name": "sub",
        "path": "/workspace/sub",
        "type": "directory",
        "size": 0,
    }


def test_step4_harness_parse_parity() -> None:
    """_parse_harness_exec_output facade == HarnessExecService."""
    import base64

    import src.service as service_module
    from src.services import harness_exec as harness_module

    assert service_module.HarnessExecService is harness_module.HarnessExecService

    svc = _make_service()
    assert svc.harness is svc._harness

    stdout_b64 = base64.b64encode(b"out").decode()
    stderr_b64 = base64.b64encode(b"err").decode()
    output = (
        f"OPENCURIA_STDOUT\n{stdout_b64}\n"
        f"OPENCURIA_STDERR\n{stderr_b64}\nEXIT:0"
    )
    assert svc._parse_harness_exec_output(
        output
    ) == harness_module.HarnessExecService._parse_harness_exec_output(
        output
    ) == ("out", "err")


def test_step4_websocket_handlers_use_managers() -> None:
    """Websocket files/harness handlers bypass the facade delegates."""
    import inspect

    from src.interfaces import websocket as websocket_module

    source = inspect.getsource(websocket_module.WebSocketInterface)
    for handler_snippet in (
        "self._service.files.list_files",
        "self._service.files.find_files",
        "self._service.files.read_file",
        "self._service.files.upload_file",
        "self._service.files.download_file",
        "self._service.files.stat_path",
        "self._service.files.write_file_content",
        "self._service.harness.exec_harness_command_stream",
        "self._service.harness.exec_harness_command",
    ):
        assert handler_snippet in source, handler_snippet
    for legacy_snippet in (
        "self._service.list_files(",
        "self._service.find_files(",
        "self._service.read_file(",
        "self._service.upload_file(",
        "self._service.download_file(",
        "self._service.stat_path(",
        "self._service.write_file_content(",
        "self._service.exec_harness_command(",
        "self._service.exec_harness_command_stream(",
    ):
        assert legacy_snippet not in source, legacy_snippet


# -- Step 5: credentials service + exec kernel cutover ------------------------
#
# Decision note: ``WorkspaceContext`` is introduced and used in the NEW
# code paths only (credentials manager + exec kernel ``exec_command``
# helpers). The ~30 existing workspace preambles in the desktop / git /
# lifecycle clusters are NOT rewritten here; each cluster adopts
# ``WorkspaceContext`` when it moves (Steps 6-8, lifecycle owns the
# final cutover). This keeps Step 5 behavior-preserving with no caller
# rewiring beyond delegation.


async def test_step5_keyed_lock_map_identity() -> None:
    """``KeyedLockMap``: same key -> same lock, different keys -> different.

    Pins the never-evict get-or-create semantics for the canonical map
    plus every factory now delegating to it (background manager map,
    service git/desktop maps, and the service background factory).
    """
    from src.services.exec_kernel import KeyedLockMap

    keyed: KeyedLockMap = KeyedLockMap()
    first = await keyed.get("a")
    second = await keyed.get("a")
    assert first is second
    assert await keyed.get("b") is not first
    # Entries are never evicted: the dict keeps the identical objects.
    assert keyed.as_dict()["a"] is first
    assert keyed.locks["a"] is first

    svc = _make_service()
    workspace_id = uuid.uuid4()

    via_service_bg_first = await svc._background_start_lock(
        workspace_id, "proc"
    )
    assert await svc._background_start_lock(workspace_id, "proc") is (
        via_service_bg_first
    )
    assert (
        svc._background_start_locks[(workspace_id, "proc")]
        is via_service_bg_first
    )
    assert await svc._background_start_lock(
        workspace_id, "other"
    ) is not via_service_bg_first

    via_service_git_first = await svc._git_lock(workspace_id, "/workspace/repo")
    assert await svc._git_lock(workspace_id, "/workspace/repo") is (
        via_service_git_first
    )
    assert svc._git_locks[(workspace_id, "/workspace/repo")] is (
        via_service_git_first
    )
    assert await svc._git_lock(workspace_id, "/other") is not (
        via_service_git_first
    )

    via_service_desk_first = await svc._desktop_lock(workspace_id)
    assert await svc._desktop_lock(workspace_id) is via_service_desk_first
    assert svc._desktop_locks.get(workspace_id) is via_service_desk_first


async def test_step5_workspace_context_resolve_error_parity() -> None:
    """``WorkspaceContext.resolve`` matches the facade preamble error shape.

    Unknown workspace -> ``ValueError("... not found")`` (same message as
    ``_get_cached``), unknown runtime -> ``RuntimeError`` (same message as
    ``_get_runtime``), missing instance -> ``RuntimeError("Workspace has
    no instance assigned")`` (same message as every facade entry point).
    A resolvable workspace yields identical info/runtime objects.
    """
    from src.services.exec_kernel import WorkspaceContext

    svc = _make_service()
    unknown_id = uuid.uuid4()

    with pytest.raises(ValueError, match="not found") as exc_ctx:
        await WorkspaceContext.resolve(
            svc._get_cached, svc._get_runtime, unknown_id
        )
    with pytest.raises(ValueError, match="not found") as exc_facade:
        svc._get_cached(unknown_id)
    assert str(exc_ctx.value) == str(exc_facade.value)

    svc._cache[unknown_id] = WorkspaceInfo(
        workspace_id=unknown_id,
        instance_id="instance-1",
        status="running",
        runtime_type="docker",
    )
    with pytest.raises(RuntimeError, match="not available") as exc_ctx:
        await WorkspaceContext.resolve(
            svc._get_cached, svc._get_runtime, unknown_id
        )
    with pytest.raises(RuntimeError, match="not available") as exc_facade:
        svc._get_runtime(unknown_id)
    assert str(exc_ctx.value) == str(exc_facade.value)

    svc2 = _make_service(runtimes={"docker": object()})
    empty_id = uuid.uuid4()
    svc2._cache[empty_id] = WorkspaceInfo(
        workspace_id=empty_id,
        instance_id="",
        status="running",
        runtime_type="docker",
    )
    with pytest.raises(RuntimeError, match="no instance assigned"):
        await WorkspaceContext.resolve(
            svc2._get_cached, svc2._get_runtime, empty_id
        )

    ok_id = uuid.uuid4()
    svc2._cache[ok_id] = WorkspaceInfo(
        workspace_id=ok_id,
        instance_id="instance-1",
        status="running",
        runtime_type="docker",
    )
    ctx = await WorkspaceContext.resolve(
        svc2._get_cached, svc2._get_runtime, ok_id
    )
    assert ctx.workspace_id == ok_id
    assert ctx.info is svc2._get_cached(ok_id)
    assert ctx.runtime is svc2._get_runtime(ok_id)


async def test_step5_credentials_and_exec_kernel_delegation_parity() -> None:
    """Facade credential/exec entry points delegate to the canonical owners.

    ``credentials`` property exposes the manager; helpers, wrap, exec,
    and inject/remove agree exactly between facade, manager, and kernel.
    """
    from unittest.mock import AsyncMock, Mock

    from src.services import credentials as credentials_module
    from src.services import exec_kernel as exec_kernel_module

    svc = _make_service()
    assert svc.credentials is svc._credentials_manager
    assert isinstance(
        svc.credentials, credentials_module.CredentialManager
    )
    assert isinstance(svc.exec_kernel, exec_kernel_module.ExecKernel)

    assert (
        svc._credential_path_helpers()
        == svc.credentials._credential_path_helpers()
        == exec_kernel_module.credential_path_helpers()
    )
    command = {"args": ["git", "clone", "repo"], "env": {"EXTRA": "1"}}
    assert svc._wrap_command_with_persistent_env(
        command
    ) == svc.credentials._wrap_command_with_persistent_env(
        command
    ) == exec_kernel_module.wrap_command_with_persistent_env(command)

    class _DummyExecRuntime:
        def __init__(self) -> None:
            self.calls: list = []

        async def exec_command_wait(self, instance_id, command=None, workdir=None, env=None):  # noqa: ANN001
            self.calls.append(
                (instance_id, list(command or []), workdir, env)
            )
            return 0, "ok"

    runtime = _DummyExecRuntime()
    via_facade = await svc._exec_command(runtime, "i-1", dict(command))
    via_kernel = await svc.exec_kernel.exec_command(runtime, "i-1", dict(command))
    via_module = await exec_kernel_module.exec_command(
        runtime, "i-1", dict(command)
    )
    assert via_facade == via_kernel == via_module == (0, "ok")
    assert [call[1] for call in runtime.calls] == [
        list(
            exec_kernel_module.wrap_command_with_persistent_env(
                {"args": ["git", "clone", "repo"], "env": {"EXTRA": "1"}}
            )["args"]
        )
    ] * 3

    # Empty credential set returns False via facade and manager alike
    # (remove hook mocked on both layers, like the security tests do).
    svc.remove_workspace_credentials = AsyncMock()
    svc.credentials.remove_workspace_credentials = AsyncMock()
    assert (
        await svc.inject_workspace_credentials(
            runtime, "i-1", {}, [], [], log=Mock()
        )
        is False
    )
    assert (
        await svc.credentials.inject_workspace_credentials(
            runtime, "i-1", {}, [], [], log=Mock()
        )
        is False
    )


# -- Step 6a: desktop manager (verbatim move, PART 1) -------------------------


def test_step6a_desktop_constants_and_pure_helper_parity() -> None:
    """Desktop constants + pure helpers agree via facade and manager.

    ``src.service`` re-exports the canonical
    ``src.services.sessions.desktop`` constants (identical objects) and
    keeps thin delegates with identical signatures; the manager owns
    the session/recording dicts plus the keyed lock map, exposed via
    facade property aliases (live identity, never-evict locks).
    """
    import src.service as service_module
    from src.models import DesktopReleaseResult, DesktopSession
    from src.services.sessions import desktop as desktop_module

    svc = _make_service()

    # Constants are identical objects (re-export, not copies).
    for name in (
        "DESKTOP_DISPLAY",
        "DESKTOP_HOME",
        "DESKTOP_XAUTHORITY_PATH",
        "DEFAULT_DESKTOP_WIDTH",
        "DEFAULT_DESKTOP_HEIGHT",
        "MIN_DESKTOP_WIDTH",
        "MAX_DESKTOP_WIDTH",
        "MIN_DESKTOP_HEIGHT",
        "MAX_DESKTOP_HEIGHT",
        "COMPUTER_USE_RECORD_DIR",
        "DESKTOP_EXECUTE_MAX_CHARS",
        "DESKTOP_EXECUTE_TIMEOUT_S",
        "_RUN_ID_RE",
        "DESKTOP_HOLDER_VIEWER",
        "DESKTOP_HOLDER_COMPUTERUSE",
        "_SCROLL_BUTTONS",
        "_CLICK_BUTTONS",
    ):
        assert getattr(service_module, name) is getattr(desktop_module, name), name
    assert service_module.DESKTOP_DISPLAY == ":1"
    assert service_module.DesktopManager is desktop_module.DesktopManager

    # ``DesktopSession`` / ``DesktopReleaseResult`` still come from
    # ``src.models`` (imported, never redefined) on both layers.
    assert service_module.DesktopSession is DesktopSession
    assert service_module.DesktopReleaseResult is DesktopReleaseResult

    # Manager wiring: ``desktop`` property exposes the manager; the
    # facade aliases are live views onto the manager-owned stores.
    assert svc.desktop is svc._desktop
    assert isinstance(svc.desktop, desktop_module.DesktopManager)
    assert svc._desktop_sessions is svc.desktop._desktop_sessions
    assert svc._desktop_recordings is svc.desktop._desktop_recordings
    assert svc._desktop_lock_map is svc.desktop._desktop_lock_map
    assert svc._desktop_locks is svc.desktop._desktop_locks
    assert svc._desktop_locks_guard is svc.desktop._desktop_locks_guard

    # Pure helper parity: valid/invalid holders, geometry defaults and
    # clamping, run-id validation, desktop env, and start-command shape.
    assert svc._parse_desktop_holder("viewer") == "viewer"
    assert svc._parse_desktop_holder("  COMPUTERUSE ") == "computeruse"
    assert desktop_module.DesktopManager._parse_desktop_holder(
        "viewer"
    ) == svc._parse_desktop_holder("viewer")
    for bad in ("", "admin", " viewer2 "):
        with pytest.raises(ValueError, match="Invalid desktop holder"):
            svc._parse_desktop_holder(bad)
        with pytest.raises(ValueError, match="Invalid desktop holder"):
            desktop_module.DesktopManager._parse_desktop_holder(bad)

    assert svc._resolve_desktop_geometry() == (1920, 1080)
    assert desktop_module.DesktopManager._resolve_desktop_geometry() == (
        svc._resolve_desktop_geometry()
    )
    assert svc._resolve_desktop_geometry(1281, 721) == (1280, 720)
    assert desktop_module.DesktopManager._resolve_desktop_geometry(
        1281, 721
    ) == svc._resolve_desktop_geometry(1281, 721)
    assert svc._resolve_desktop_geometry(1, 99999) == (
        desktop_module.DesktopManager._resolve_desktop_geometry(1, 99999)
    ) == (800, 2160)

    assert svc._sanitize_run_id("run-1") == "run-1"
    assert desktop_module.DesktopManager._sanitize_run_id(
        "run-1"
    ) == svc._sanitize_run_id("run-1")
    with pytest.raises(ValueError, match="Invalid run_id"):
        svc._sanitize_run_id("bad id!")
    with pytest.raises(ValueError, match="Invalid run_id"):
        desktop_module.DesktopManager._sanitize_run_id("bad id!")

    assert svc._desktop_env() == desktop_module.DesktopManager._desktop_env() == {
        "HOME": "/root",
        "DISPLAY": ":1",
        "XAUTHORITY": "/root/.Xauthority",
    }

    command = svc._desktop_start_command(1280, 720)
    assert command == desktop_module.DesktopManager._desktop_start_command(
        1280, 720
    )
    assert "-geometry 1280x720" in command
    assert "-AcceptSetDesktopSize=0" in command

    empty_facade = svc._empty_desktop_release_result()
    empty_manager = svc.desktop._empty_desktop_release_result()
    assert empty_facade == empty_manager == DesktopReleaseResult(
        stopped=False,
        process_alive=False,
        viewer_held=False,
        computer_use_active=False,
    )


async def test_step6a_desktop_lock_never_evict_via_facade_and_manager() -> None:
    """Desktop per-workspace locks keep never-evict identity on both layers."""
    svc = _make_service()
    workspace_id = uuid.uuid4()

    via_facade = await svc._desktop_lock(workspace_id)
    assert await svc._desktop_lock(workspace_id) is via_facade
    assert await svc.desktop._desktop_lock(workspace_id) is via_facade
    assert svc._desktop_locks.get(workspace_id) is via_facade
    assert svc.desktop._desktop_locks.get(workspace_id) is via_facade
    assert await svc._desktop_lock(uuid.uuid4()) is not via_facade


# -- Step 6b: desktop_action dispatcher split + manager-direct handlers -----
#
# Step 6b splits ``DesktopManager.desktop_action`` (verbatim since Step 6a)
# into a thin dispatcher plus one private ``_desktop_action_<action>``
# helper per branch (same order/checks/messages), and rewires the
# websocket desktop handlers to ``self._service.desktop.*``. The facade
# ``desktop_action`` delegate keeps its signature.


class _FakeDesktopRuntime:
    """Minimal runtime for desktop lease/dispatcher tests (no docker)."""

    def __init__(self) -> None:
        self.live = True

    async def exec_command_wait(self, instance_id, command=None, workdir=None, env=None):  # noqa: ANN001
        argv = list(command or [])
        shell = argv[-1] if len(argv) > 2 and argv[0] == "bash" else ""
        if "connect_ex" in shell or "pgrep" in shell:
            return (0, "alive") if self.live else (1, "dead")
        if "opencuria-desktop-stop" in shell or argv == [
            "/usr/local/bin/opencuria-desktop-stop",
        ]:
            self.live = False
            return (0, "")
        if "/usr/bin/Xvnc :1" in shell:
            self.live = True
            return (0, "started")
        return (0, "")

    def get_container_ip(self, instance_id: str, workspace_id: str) -> str:
        return "172.22.0.2"

    def get_workspace_network_name(self, workspace_id: str) -> str:
        return f"opencuria-ws-{workspace_id}"


def _make_desktop_service(runtime=None):  # noqa: ANN001
    from unittest.mock import AsyncMock

    from src.models import WorkspaceInfo

    svc = _make_service(runtimes={"docker": runtime or _FakeDesktopRuntime()})
    workspace_id = uuid.uuid4()
    svc._cache[workspace_id] = WorkspaceInfo(
        workspace_id=workspace_id,
        instance_id="instance-1",
        status="running",
        runtime_type="docker",
    )
    # ``desktop_action("execute")`` goes through the harness facade; stub
    # it so dispatcher tests never touch docker.
    svc.exec_harness_command = AsyncMock(return_value=(0, "out", "err"))
    svc.desktop._exec_harness_command = svc.exec_harness_command
    return svc, workspace_id


async def test_step6b_hold_lease_generation_viewer_then_computeruse() -> None:
    """Viewer + computer-use leases share one session; releases drain in order.

    acquire(holder=viewer) then acquire(holder=computeruse, run_id) land on
    the same session object/generation; releasing computeruse keeps the
    process live (viewer still held); releasing viewer stops it.
    """
    svc, workspace_id = _make_desktop_service()

    viewer_session = await svc.acquire_desktop(
        workspace_id, holder="viewer"
    )
    computeruse_session = await svc.acquire_desktop(
        workspace_id, holder="computeruse", run_id="run-1"
    )
    assert viewer_session is computeruse_session
    assert viewer_session.viewer_held is True
    assert "run-1" in viewer_session.computeruse_run_ids
    generation = viewer_session.generation

    kept = await svc.release_desktop(
        workspace_id, holder="computeruse", run_id="run-1"
    )
    assert kept.stopped is False
    assert kept.process_alive is True
    assert kept.viewer_held is True
    assert workspace_id in svc._desktop_sessions
    assert svc._desktop_sessions[workspace_id].generation == generation

    stopped = await svc.release_desktop(workspace_id, holder="viewer")
    assert stopped.stopped is True
    assert stopped.process_alive is False
    assert workspace_id not in svc._desktop_sessions


async def test_step6b_dispatcher_split_facade_and_manager_agree() -> None:
    """``desktop_action("ensure")`` works identically via facade + manager.

    Pins the Step 6b dispatcher split: the dispatcher routes ``ensure``
    to ``_desktop_action_ensure`` on both layers with the same result,
    and every ``if action ==`` branch has its own private helper.
    """
    from src.services.sessions import desktop as desktop_module

    svc, workspace_id = _make_desktop_service()

    via_facade = await svc.desktop_action(workspace_id, "ensure")
    assert via_facade == {"ok": True, "display": ":1", "port": 6901}

    other_id = uuid.uuid4()
    from src.models import WorkspaceInfo

    svc._cache[other_id] = WorkspaceInfo(
        workspace_id=other_id,
        instance_id="instance-1",
        status="running",
        runtime_type="docker",
    )
    via_manager = await svc.desktop.desktop_action(other_id, "ensure")
    assert via_manager == {"ok": True, "display": ":1", "port": 6901}

    for action in (
        "ensure",
        "hold",
        "release",
        "display_info",
        "screenshot",
        "move",
        "click",
        "drag",
        "scroll",
        "type",
        "key",
        "open_url",
        "record_start",
        "record_stop",
        "execute",
    ):
        assert callable(
            getattr(svc.desktop, f"_desktop_action_{action}")
        ), action
    assert svc.desktop._validate_desktop_execute_code("screenshot", {}) == ""

    import pytest as _pytest

    with _pytest.raises(ValueError, match="code must not be empty"):
        svc.desktop._validate_desktop_execute_code("execute", {})
    with _pytest.raises(ValueError, match="Unknown desktop action"):
        await svc.desktop_action(workspace_id, "nope")


def test_step6b_websocket_desktop_handlers_use_manager() -> None:
    """Websocket desktop handlers bypass the facade delegates."""
    import inspect

    from src.interfaces import websocket as websocket_module

    source = inspect.getsource(websocket_module.WebSocketInterface)
    for handler_snippet in (
        "self._service.desktop.get_desktop_container_ip",
        "self._service.desktop.get_desktop_state_payload",
        "self._service.desktop.get_desktop_session",
        "self._service.desktop.start_desktop",
        "self._service.desktop.get_desktop_network_name",
        "self._service.desktop.stop_desktop",
        "self._service.desktop.write_desktop_clipboard",
        "self._service.desktop.read_desktop_clipboard",
        "self._service.desktop.desktop_action",
    ):
        assert handler_snippet in source, handler_snippet
    for legacy_snippet in (
        "self._service.start_desktop(",
        "self._service.stop_desktop(",
        "self._service.desktop_action(",
        "self._service.get_desktop_session(",
        "self._service.get_desktop_state_payload(",
        "self._service.get_desktop_container_ip(",
        "self._service.get_desktop_network_name(",
        "self._service.write_desktop_clipboard(",
        "self._service.read_desktop_clipboard(",
    ):
        assert legacy_snippet not in source, legacy_snippet


# -- Step 7: git manager (verbatim move) ---------------------------------------


async def test_step7_git_lock_never_evict_via_facade_and_manager() -> None:
    """``_git_lock`` never-evict semantics agree via facade + manager.

    Pins the Step 5 ``KeyedLockMap`` guarantee through the Step 7 move:
    repeated lookups for the same workspace/repo return the identical
    lock object, different repos get different locks, and
    ``service._git_locks`` is a live alias onto the manager-owned map.
    """
    svc = _make_service()
    workspace_id = uuid.uuid4()

    assert svc.git is svc._git
    assert svc._git_lock_map is svc.git._git_lock_map
    assert svc._git_locks is svc.git._git_locks
    assert svc._git_locks_guard is svc.git._git_locks_guard

    via_facade = await svc._git_lock(workspace_id, "/workspace/repo")
    assert await svc._git_lock(workspace_id, "/workspace/repo") is via_facade
    assert await svc.git._git_lock(workspace_id, "/workspace/repo") is via_facade
    assert svc._git_locks[(workspace_id, "/workspace/repo")] is via_facade
    assert svc.git._git_locks[(workspace_id, "/workspace/repo")] is via_facade
    assert await svc._git_lock(workspace_id, "/other") is not via_facade
    assert await svc.git._git_lock(workspace_id, "/other") is not via_facade


async def test_step7_git_unknown_operation_parity_facade_and_manager() -> None:
    """Facade-vs-manager parity for a structured error (unknown_operation)."""
    svc = _make_service()
    workspace_id = uuid.uuid4()

    via_facade = await svc.execute_git_operation(
        workspace_id, "nope", None, {}
    )
    via_manager = await svc.git.execute_git_operation(
        workspace_id, "nope", None, {}
    )
    expected = {
        "ok": False,
        "code": "unknown_operation",
        "message": "Unknown git operation: nope",
        "stderr": "",
        "exit_code": None,
    }
    assert via_facade == via_manager == expected


def test_step7_git_helpers_delegate_to_manager() -> None:
    """Pure git helpers agree via facade and manager (same results/errors)."""
    import src.service as service_module
    from src.services import git_service as git_module

    assert service_module.GitService is git_module.GitService

    svc = _make_service()
    assert svc.git is svc._git
    assert isinstance(svc.git, git_module.GitService)

    assert svc._git_join_diff_parts({}, ({}, {})) == (
        svc.git._git_join_diff_parts({}, ({}, {}))
    ) == []
    assert svc._git_mutation_result({"a": 1}, repo_path="/r") == (
        svc.git._git_mutation_result({"a": 1}, repo_path="/r")
    ) == {"ok": True, "snapshot": {"a": 1}, "repo_path": "/r"}
    assert svc._git_require_paths({"paths": ["a.txt"]}, "stage") == (
        git_module.GitService._git_require_paths({"paths": ["a.txt"]}, "stage")
    ) == ["a.txt"]
    with pytest.raises(ValueError) as exc_facade:
        svc._git_require_paths({}, "stage")
    with pytest.raises(ValueError) as exc_manager:
        git_module.GitService._git_require_paths({}, "stage")
    assert str(exc_facade.value) == str(exc_manager.value)
    assert svc._git_remote_arg({"remote": "origin"}) == (
        svc.git._git_remote_arg({"remote": "origin"})
    ) == "origin"
    assert svc._git_remote_arg({}) == svc.git._git_remote_arg({}) is None
    assert svc._git_merge_msg({}, "dflt") == svc.git._git_merge_msg(
        {}, "dflt"
    ) == []
    assert svc._git_merge_msg({"message": "hi"}, "dflt") == (
        svc.git._git_merge_msg({"message": "hi"}, "dflt")
    ) == ["-m", "hi"]


def test_step7_websocket_git_handler_uses_manager() -> None:
    """Websocket git:operation handler calls the git manager directly."""
    import inspect

    from src.interfaces import websocket as websocket_module

    source = inspect.getsource(websocket_module.WebSocketInterface)
    assert "self._service.git.execute_git_operation" in source
    assert "self._service.execute_git_operation(" not in source


# -- Step 8: registry + lifecycle (final extraction) ---------------------------

@pytest.mark.asyncio
async def test_step8_creating_entry_survives_sync() -> None:
    """Registry preserves 'creating' cache entries invisible to runtimes."""
    from src.services.workspace_registry import WorkspaceRegistry

    svc = _make_service()

    creating_id = uuid.uuid4()
    svc._cache[creating_id] = WorkspaceInfo(
        workspace_id=creating_id,
        instance_id="",
        status="creating",
        runtime_type="docker",
    )

    # Composer wiring: registry owns the cache; facade alias is live.
    assert svc.registry._cache is svc._cache
    assert creating_id in svc.registry._cache

    await svc.sync_from_runtime()
    assert creating_id in svc._cache
    assert svc._cache[creating_id].status == "creating"

    # Same guarantee through the manager directly.
    registry = WorkspaceRegistry(runtimes={}, settings=RunnerSettings())
    registry._cache[creating_id] = WorkspaceInfo(
        workspace_id=creating_id,
        instance_id="",
        status="creating",
        runtime_type="docker",
    )
    await registry.sync_from_runtime()
    assert registry._cache[creating_id].status == "creating"


@pytest.mark.asyncio
async def test_step8_remove_workspace_teardown_kills_all() -> None:
    """remove_workspace pops the cache and fans out to all teardowns."""
    from unittest.mock import AsyncMock

    svc = _make_service()
    workspace_id = uuid.uuid4()
    svc._cache[workspace_id] = WorkspaceInfo(
        workspace_id=workspace_id,
        instance_id="instance-1",
        status="running",
        runtime_type="docker",
    )

    svc.close_workspace_streams = AsyncMock(return_value=2)  # type: ignore[method-assign]
    svc._kill_all_background_processes = AsyncMock(return_value=None)  # type: ignore[method-assign]
    svc._interrupt_desktop_recordings = AsyncMock(return_value=None)  # type: ignore[method-assign]

    removed: list[str] = []

    class TeardownRuntime:
        async def remove_workspace(self, instance_id: str) -> None:
            removed.append(instance_id)

    svc._runtimes = {"docker": TeardownRuntime()}
    await svc.remove_workspace(workspace_id)

    assert workspace_id not in svc._cache
    svc.close_workspace_streams.assert_awaited_once_with(
        workspace_id, reason="remove"
    )
    svc._kill_all_background_processes.assert_awaited_once_with(
        workspace_id, reason="remove"
    )
    svc._interrupt_desktop_recordings.assert_awaited_once_with(workspace_id)
    assert removed == ["instance-1"]


def test_step8_workspace_exists_replaces_cache_poke() -> None:
    """Public workspace_exists() replaces the private _cache poke."""
    import inspect

    from src.interfaces import websocket as websocket_module

    svc = _make_service()
    workspace_id = uuid.uuid4()
    assert svc.workspace_exists(workspace_id) is False
    svc._cache[workspace_id] = WorkspaceInfo(
        workspace_id=workspace_id,
        instance_id="",
        status="creating",
        runtime_type="docker",
    )
    assert svc.workspace_exists(workspace_id) is True
    # Composer exposes both new entry points.
    assert svc.registry is svc._registry
    assert svc.lifecycle is svc._lifecycle
    assert svc._unreachable_since is svc._lifecycle._unreachable_since

    source = inspect.getsource(websocket_module.WebSocketInterface)
    assert "self._service.workspace_exists(workspace_id)" in source
    assert "self._service._cache" not in source
