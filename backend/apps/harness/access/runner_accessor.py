"""Runner-backed workspace access via Socket.IO RPC.

The harness never touches workspace files directly. Instead,
:class:`RunnerWorkspaceAccessor` forwards every operation to the runner
that owns the workspace using ``harness:*`` Socket.IO events and
correlates responses by ``request_id``.

Protocol (backend -> runner)::

    harness:exec_stream {request_id, workspace_id, command, workdir,
                         env, timeout}
    harness:exec_wait   {request_id, workspace_id, command, workdir,
                         env, timeout}
    harness:read_file   {request_id, workspace_id, path, max_size}
    harness:write_file  {request_id, workspace_id, path, content, mode}
    harness:list        {request_id, workspace_id, path}
    harness:stat           {request_id, workspace_id, path}
    harness:desktop_action {request_id, workspace_id, action, args}
    harness:cancel         {request_id, workspace_id}

Protocol (runner -> backend)::

    harness:exec_chunk       {request_id, workspace_id, stream, data}
    harness:exec_done        {request_id, workspace_id, exit_code}
                             or {request_id, workspace_id, error}
    harness:exec_wait_result {request_id, workspace_id, exit_code,
                              stdout, stderr} or {..., error}
    harness:read_file_result {request_id, workspace_id, content, size,
                              truncated, mime} or {..., error}
    harness:write_file_result {request_id, workspace_id, ok}
                              or {..., error}
    harness:list_result      {request_id, workspace_id, entries}
                             or {..., error}
    harness:stat_result           {request_id, workspace_id, path, is_dir,
                                   size, mime} or {..., error}
    harness:desktop_action_result {request_id, workspace_id, ok?, ...}
                                  or {..., error}

Timeouts are enforced locally with ``asyncio``; on timeout or task
cancellation a best-effort ``harness:cancel`` is sent so the runner can
stop the remote operation.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from apps.runners.exceptions import (
    RunnerOfflineError,
    WorkspaceNotFoundError,
)
from common.exceptions import ConflictError

from .base import (
    DirEntry,
    ExecChunk,
    ExecResult,
    FileContent,
    FileStat,
    WorkspaceAccessor,
    guess_mime_type,
    sanitize_harness_path,
)

log = structlog.get_logger(__name__)

EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]

DEFAULT_TIMEOUT = 60.0


class RunnerAccessorError(RuntimeError):
    """Raised when the runner reports a harness operation failure."""


# Maps in-flight request_id -> owning accessor instance. Runner reply
# handlers in ``apps.runners.sio_server`` route through this registry.
_ACCESSORS_BY_REQUEST: dict[str, RunnerWorkspaceAccessor] = {}


def route_harness_chunk(data: dict[str, Any]) -> bool:
    """Deliver a ``harness:exec_chunk`` payload to its accessor."""
    accessor = _ACCESSORS_BY_REQUEST.get(str(data.get("request_id", "")))
    if accessor is None:
        log.warning(
            "harness_chunk_unknown_request",
            request_id=data.get("request_id", ""),
        )
        return False
    accessor._deliver_chunk(data)
    return True


def route_harness_done(data: dict[str, Any]) -> bool:
    """Deliver a ``harness:exec_done`` payload to its accessor."""
    accessor = _ACCESSORS_BY_REQUEST.get(str(data.get("request_id", "")))
    if accessor is None:
        log.warning(
            "harness_done_unknown_request",
            request_id=data.get("request_id", ""),
        )
        return False
    accessor._deliver_done(data)
    return True


def route_harness_result(data: dict[str, Any]) -> bool:
    """Deliver a ``harness:*_result`` payload to its accessor."""
    accessor = _ACCESSORS_BY_REQUEST.get(str(data.get("request_id", "")))
    if accessor is None:
        log.warning(
            "harness_result_unknown_request",
            request_id=data.get("request_id", ""),
        )
        return False
    accessor._deliver_result(data)
    return True


def _normalize_command(command: list[str] | str) -> list[str] | str:
    """Validate and normalize an exec command payload."""
    if isinstance(command, str):
        if not command.strip():
            raise ValueError("command must not be empty")
        return command
    args = [str(arg) for arg in command]
    if not args:
        raise ValueError("command must not be empty")
    return args


def _serialize_process(process: Any) -> dict[str, Any]:
    """Convert a ``WorkspaceProcess`` ORM row to a JSON-safe dict."""
    ended_at = getattr(process, "ended_at", None)
    started_at = getattr(process, "started_at", None)
    updated_at = getattr(process, "updated_at", None)
    return {
        "process_id": str(getattr(process, "id", "")),
        "workspace_id": str(getattr(process, "workspace_id", "")),
        "name": getattr(process, "name", "") or "",
        "command": getattr(process, "command", "") or "",
        "workdir": getattr(process, "workdir", "") or "",
        "pid": getattr(process, "pid", None),
        "log_path": getattr(process, "log_path", "") or "",
        "status": str(getattr(process, "status", "") or ""),
        "exit_code": getattr(process, "exit_code", None),
        "started_at": started_at.isoformat() if started_at else None,
        "ended_at": ended_at.isoformat() if ended_at else None,
        "updated_at": updated_at.isoformat() if updated_at else None,
    }


def _parse_process_uuid(process_id: str) -> uuid.UUID:
    """Parse *process_id* into a UUID or raise ValueError."""
    try:
        return uuid.UUID(str(process_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"Invalid process_id: {process_id}") from exc


class RunnerWorkspaceAccessor(WorkspaceAccessor):
    """Workspace accessor that forwards operations to a runner.

    The ``emit`` callable sends ``(event, payload)`` to the runner that
    owns the workspace. Runner replies are routed back via the module
    level ``route_harness_*`` functions called from Socket.IO handlers.
    """

    def __init__(
        self,
        workspace_id: str,
        *,
        emit: EmitFn,
        default_timeout: float = DEFAULT_TIMEOUT,
        desktop_geometry: Callable[[], Awaitable[tuple[int, int]]] | None = None,
    ) -> None:
        super().__init__(workspace_id)
        self._emit = emit
        self._default_timeout = default_timeout
        self._desktop_geometry = desktop_geometry
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._streams: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        try:
            self._loop: asyncio.AbstractEventLoop | None = (
                asyncio.get_running_loop()
            )
        except RuntimeError:
            self._loop = None

    def _schedule(self, callback: Callable[..., None], *args: Any) -> None:
        """Run *callback* on this accessor's loop (thread-safe).

        Socket.IO replies arrive via ``sync_to_async`` worker threads.
        ``Future.set_result`` / ``Queue.put_nowait`` must run on the loop
        that owns the waiter.
        """
        loop = self._loop
        if loop is None or not loop.is_running():
            callback(*args)
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            callback(*args)
            return
        loop.call_soon_threadsafe(callback, *args)

    # -- reply routing (called from Socket.IO handlers) -------------------

    def _deliver_result(self, data: dict[str, Any]) -> None:
        """Resolve the pending unary request future for *data*."""
        request_id = str(data.get("request_id", ""))
        future = self._pending.get(request_id)
        if future is None or future.done():
            log.warning("harness_result_no_pending", request_id=request_id)
            return
        self._schedule(_resolve_future, future, data)

    def _deliver_chunk(self, data: dict[str, Any]) -> None:
        """Push a stream chunk into the queue for *data*."""
        request_id = str(data.get("request_id", ""))
        queue = self._streams.get(request_id)
        if queue is None:
            log.warning("harness_chunk_no_stream", request_id=request_id)
            return
        self._schedule(
            queue.put_nowait,
            {
                "type": "chunk",
                "stream": str(data.get("stream", "stdout")),
                "data": str(data.get("data", "")),
            },
        )

    def _deliver_done(self, data: dict[str, Any]) -> None:
        """Push the terminal stream message into the queue for *data*."""
        request_id = str(data.get("request_id", ""))
        queue = self._streams.get(request_id)
        if queue is None:
            log.warning("harness_done_no_stream", request_id=request_id)
            return
        self._schedule(queue.put_nowait, {"type": "done", "payload": data})

    # -- internals --------------------------------------------------------

    def _register(
        self,
        request_id: str,
        *,
        future: asyncio.Future[dict[str, Any]] | None = None,
        queue: asyncio.Queue[dict[str, Any]] | None = None,
    ) -> None:
        """Track an in-flight request for reply routing."""
        _ACCESSORS_BY_REQUEST[request_id] = self
        if future is not None:
            self._pending[request_id] = future
        if queue is not None:
            self._streams[request_id] = queue

    def _unregister(self, request_id: str) -> None:
        """Drop all routing state for *request_id*."""
        _ACCESSORS_BY_REQUEST.pop(request_id, None)
        self._pending.pop(request_id, None)
        self._streams.pop(request_id, None)

    def _resolve_timeout(self, timeout: float | None) -> float:
        """Return the effective timeout for a call."""
        resolved = self._default_timeout if timeout is None else timeout
        if resolved <= 0:
            raise ValueError("timeout must be a positive number of seconds")
        return resolved

    async def _emit_cancel(self, request_id: str) -> None:
        """Best-effort cancel notification for an abandoned request."""
        with contextlib.suppress(Exception):
            await asyncio.shield(
                self._emit(
                    "harness:cancel",
                    {
                        "request_id": request_id,
                        "workspace_id": self.workspace_id,
                    },
                )
            )

    async def _await_result(
        self,
        request_id: str,
        event: str,
        payload: dict[str, Any],
        timeout: float | None,
    ) -> dict[str, Any]:
        """Emit a unary request and wait for its correlated result."""
        timeout_s = self._resolve_timeout(timeout)
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._register(request_id, future=future)
        completed = False
        try:
            await self._emit(event, payload)
            result = await asyncio.wait_for(future, timeout_s)
            completed = True
            return result
        except TimeoutError as exc:
            raise TimeoutError(
                f"Harness request '{event}' timed out "
                f"after {timeout_s:.1f}s"
            ) from exc
        finally:
            self._unregister(request_id)
            if not completed:
                future.cancel()
                await self._emit_cancel(request_id)

    @staticmethod
    def _raise_for_error(result: dict[str, Any], operation: str) -> None:
        """Raise RunnerAccessorError when the runner reported an error."""
        error = result.get("error")
        if error:
            raise RunnerAccessorError(f"{operation} failed: {error}")

    # -- WorkspaceAccessor API --------------------------------------------

    async def exec_stream(
        self,
        command: list[str] | str,
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ):  # type: ignore[override]
        """Stream command output; final chunk carries the exit code."""
        safe_workdir = sanitize_harness_path(workdir)
        normalized = _normalize_command(command)
        timeout_s = self._resolve_timeout(timeout)
        request_id = uuid.uuid4().hex
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._register(request_id, queue=queue)
        await self._emit(
            "harness:exec_stream",
            {
                "request_id": request_id,
                "workspace_id": self.workspace_id,
                "command": normalized,
                "workdir": safe_workdir,
                "env": dict(env or {}),
                "timeout": timeout_s,
            },
        )
        deadline = asyncio.get_running_loop().time() + timeout_s
        finished = False
        try:
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError(
                        "Harness request 'harness:exec_stream' timed out "
                        f"after {timeout_s:.1f}s"
                    )
                try:
                    item = await asyncio.wait_for(queue.get(), remaining)
                except TimeoutError as exc:
                    raise TimeoutError(
                        "Harness request 'harness:exec_stream' timed out "
                        f"after {timeout_s:.1f}s"
                    ) from exc
                if item.get("type") == "chunk":
                    yield ExecChunk(
                        stream=item["stream"],
                        data=item.get("data", ""),
                    )
                else:
                    payload = item.get("payload", {})
                    self._raise_for_error(payload, "harness:exec_stream")
                    finished = True
                    yield ExecChunk(
                        stream="",
                        exit_code=int(payload.get("exit_code", 0)),
                        done=True,
                    )
                    return
        finally:
            self._unregister(request_id)
            if not finished:
                await self._emit_cancel(request_id)

    async def exec_wait(
        self,
        command: list[str] | str,
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> ExecResult:
        """Execute a command and return the buffered result."""
        safe_workdir = sanitize_harness_path(workdir)
        normalized = _normalize_command(command)
        timeout_s = self._resolve_timeout(timeout)
        request_id = uuid.uuid4().hex
        result = await self._await_result(
            request_id,
            "harness:exec_wait",
            {
                "request_id": request_id,
                "workspace_id": self.workspace_id,
                "command": normalized,
                "workdir": safe_workdir,
                "env": dict(env or {}),
                "timeout": timeout_s,
            },
            timeout,
        )
        self._raise_for_error(result, "harness:exec_wait")
        return ExecResult(
            exit_code=int(result.get("exit_code", 0)),
            stdout=str(result.get("stdout", "")),
            stderr=str(result.get("stderr", "")),
        )

    async def read_file(
        self,
        path: str,
        max_size: int | None = None,
    ) -> FileContent:
        """Read a file from the sandboxed workspace."""
        safe_path = sanitize_harness_path(path)
        request_id = uuid.uuid4().hex
        result = await self._await_result(
            request_id,
            "harness:read_file",
            {
                "request_id": request_id,
                "workspace_id": self.workspace_id,
                "path": safe_path,
                "max_size": max_size,
            },
            None,
        )
        self._raise_for_error(result, "harness:read_file")
        raw_content = str(result.get("content", ""))
        try:
            content = base64.b64decode(raw_content) if raw_content else b""
        except Exception as exc:
            raise RunnerAccessorError(
                "harness:read_file failed: invalid base64 payload"
            ) from exc
        mime = str(
            result.get("mime") or result.get("mime_type") or guess_mime_type(
                safe_path
            )
        )
        return FileContent(
            content=content,
            size=int(result.get("size", len(content))),
            truncated=bool(result.get("truncated", False)),
            mime=mime,
        )

    async def write_file(
        self,
        path: str,
        content: bytes,
        mode: int = 0o644,
    ) -> None:
        """Write a file atomically into the sandboxed workspace."""
        safe_path = sanitize_harness_path(path)
        if not isinstance(content, (bytes, bytearray)):
            raise ValueError("content must be bytes")
        request_id = uuid.uuid4().hex
        result = await self._await_result(
            request_id,
            "harness:write_file",
            {
                "request_id": request_id,
                "workspace_id": self.workspace_id,
                "path": safe_path,
                "content": base64.b64encode(bytes(content)).decode("ascii"),
                "mode": mode,
            },
            None,
        )
        self._raise_for_error(result, "harness:write_file")

    async def list_dir(self, path: str) -> list[DirEntry]:
        """List directory entries inside the sandboxed workspace."""
        safe_path = sanitize_harness_path(path)
        request_id = uuid.uuid4().hex
        result = await self._await_result(
            request_id,
            "harness:list",
            {
                "request_id": request_id,
                "workspace_id": self.workspace_id,
                "path": safe_path,
            },
            None,
        )
        self._raise_for_error(result, "harness:list")
        entries: list[DirEntry] = []
        raw_entries = result.get("entries", [])
        if not isinstance(raw_entries, list):
            raise RunnerAccessorError("harness:list failed: invalid entries")
        for entry in raw_entries:
            if not isinstance(entry, dict):
                continue
            is_dir = entry.get("is_dir", entry.get("type") == "directory")
            entries.append(
                DirEntry(
                    name=str(entry.get("name", "")),
                    path=str(entry.get("path", "")),
                    is_dir=bool(is_dir),
                    size=int(entry.get("size", 0) or 0),
                )
            )
        return entries

    async def stat(self, path: str) -> FileStat:
        """Stat a path inside the sandboxed workspace."""
        safe_path = sanitize_harness_path(path)
        request_id = uuid.uuid4().hex
        result = await self._await_result(
            request_id,
            "harness:stat",
            {
                "request_id": request_id,
                "workspace_id": self.workspace_id,
                "path": safe_path,
            },
            None,
        )
        self._raise_for_error(result, "harness:stat")
        mime = str(
            result.get("mime") or result.get("mime_type") or guess_mime_type(
                safe_path
            )
        )
        return FileStat(
            path=str(result.get("path", safe_path)),
            is_dir=bool(result.get("is_dir", False)),
            size=int(result.get("size", 0) or 0),
            mime=mime,
        )

    async def desktop_action(
        self,
        action: str,
        args: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Run a desktop automation action in the workspace."""
        payload_args = dict(args or {})
        if action in {"ensure", "hold"} and self._desktop_geometry is not None:
            width, height = await self._desktop_geometry()
            payload_args.setdefault("desktop_width", width)
            payload_args.setdefault("desktop_height", height)
        request_id = uuid.uuid4().hex
        result = await self._await_result(
            request_id,
            "harness:desktop_action",
            {
                "request_id": request_id,
                "workspace_id": self.workspace_id,
                "action": action,
                "args": payload_args,
            },
            timeout,
        )
        self._raise_for_error(result, "harness:desktop_action")
        return result

    # -- Background processes (direct RunnerService calls) ----------------

    def _runner_service(self) -> Any:
        """Return the process-wide RunnerService (lazy import, no cycle)."""
        from apps.runners.sio_server import get_runner_service

        return get_runner_service()

    def _workspace_uuid(self) -> uuid.UUID:
        """Parse this accessor's workspace id into a UUID."""
        try:
            return uuid.UUID(str(self.workspace_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError(
                f"Invalid workspace_id: {self.workspace_id}"
            ) from exc

    async def process_start(
        self,
        command: str,
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
        name: str = "",
    ) -> dict[str, Any]:
        """Start a detached background process via RunnerService."""
        if not (command or "").strip():
            raise ValueError("command must not be empty")
        safe_workdir = sanitize_harness_path(workdir or "/workspace")
        service = self._runner_service()
        try:
            process = await service.start_process(
                self._workspace_uuid(),
                command.strip(),
                workdir=safe_workdir,
                env=dict(env or {}),
                name=name or "",
            )
        except (RunnerOfflineError, ConflictError, WorkspaceNotFoundError) as exc:
            raise RunnerAccessorError(f"process_start failed: {exc}") from exc
        return _serialize_process(process)

    async def process_list(self) -> list[dict[str, Any]]:
        """List background processes via RunnerService (live-merged)."""
        service = self._runner_service()
        try:
            processes = await service.list_processes(self._workspace_uuid())
        except (RunnerOfflineError, ConflictError, WorkspaceNotFoundError) as exc:
            raise RunnerAccessorError(f"process_list failed: {exc}") from exc
        return [_serialize_process(process) for process in processes]

    async def process_get(self, process_id: str) -> dict[str, Any]:
        """Return one background process via RunnerService."""
        parsed = _parse_process_uuid(process_id)
        service = self._runner_service()
        try:
            process = await service.get_process(self._workspace_uuid(), parsed)
        except (RunnerOfflineError, ConflictError, WorkspaceNotFoundError) as exc:
            raise RunnerAccessorError(f"process_get failed: {exc}") from exc
        return _serialize_process(process)

    async def process_stop(
        self, process_id: str, force: bool = False
    ) -> dict[str, Any]:
        """Stop a background process via RunnerService."""
        parsed = _parse_process_uuid(process_id)
        service = self._runner_service()
        try:
            process = await service.stop_process(
                self._workspace_uuid(), parsed, force=bool(force)
            )
        except (RunnerOfflineError, ConflictError, WorkspaceNotFoundError) as exc:
            raise RunnerAccessorError(f"process_stop failed: {exc}") from exc
        return _serialize_process(process)


def _resolve_future(
    future: asyncio.Future[dict[str, Any]], data: dict[str, Any]
) -> None:
    """Set *future* result if it is still waiting."""
    if not future.done():
        future.set_result(data)


async def create_harness_accessor(
    service: Any,
    workspace_id: str,
    *,
    default_timeout: float = DEFAULT_TIMEOUT,
) -> RunnerWorkspaceAccessor:
    """Build a runner-backed accessor for *workspace_id*.

    Args:
        service: The runners ``RunnerService`` used to emit events.
        workspace_id: Workspace UUID string owned by an online runner.

    Raises:
        WorkspaceNotFoundError: If the workspace does not exist.
        RunnerOfflineError: If the owning runner is offline.
    """
    import uuid as _uuid

    from asgiref.sync import sync_to_async

    from apps.runners.exceptions import (
        RunnerOfflineError,
        WorkspaceNotFoundError,
    )

    workspace = await sync_to_async(service.workspaces.get_by_id)(
        _uuid.UUID(workspace_id)
    )
    if workspace is None:
        raise WorkspaceNotFoundError(workspace_id)
    runner = workspace.runner
    if not runner.is_online or not runner.sid:
        raise RunnerOfflineError(str(runner.id))

    async def _emit(event: str, payload: dict[str, Any]) -> None:
        """Emit *event* to the workspace's current runner SID."""
        current = await sync_to_async(service.workspaces.get_by_id)(
            _uuid.UUID(workspace_id)
        )
        if current is None:
            raise RunnerAccessorError(
                f"{event} failed: workspace {workspace_id} not found"
            )
        live_runner = current.runner
        if not live_runner.is_online or not live_runner.sid:
            raise RunnerAccessorError(
                f"{event} failed: runner {live_runner.id} is offline"
            )
        try:
            await service.emit_harness_event(live_runner, event, payload)
        except RunnerOfflineError as exc:
            raise RunnerAccessorError(f"{event} failed: {exc}") from exc

    from apps.runners.desktop import (
        DEFAULT_DESKTOP_HEIGHT,
        DEFAULT_DESKTOP_WIDTH,
    )

    async def _desktop_geometry() -> tuple[int, int]:
        """Return the workspace's configured desktop framebuffer size."""
        current = await sync_to_async(service.workspaces.get_by_id)(
            _uuid.UUID(workspace_id)
        )
        if current is None:
            return DEFAULT_DESKTOP_WIDTH, DEFAULT_DESKTOP_HEIGHT
        return int(current.desktop_width), int(current.desktop_height)

    return RunnerWorkspaceAccessor(
        workspace_id,
        emit=_emit,
        default_timeout=default_timeout,
        desktop_geometry=_desktop_geometry,
    )
