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
                        (small payloads only; large writes use
                        harness:write_file_start/chunk/finish below)
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
    harness:read_file_chunk  {request_id, workspace_id, path, index,
                              total_chunks, content} (256 KiB base64 slices,
                              followed by a metadata-only
                              harness:read_file_result with
                              ``chunked=True`` and empty content)
    harness:write_file_start  {request_id, workspace_id, path, mode,
                               total_chunks}
    harness:write_file_chunk  {request_id, workspace_id, path, index,
                               total_chunks, content}
    harness:write_file_finish {request_id, workspace_id, path}
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
    StreamClosedError,
    WorkspaceAccessor,
    WorkspaceByteStream,
    guess_mime_type,
    sanitize_exec_workdir,
    sanitize_harness_path,
)

log = structlog.get_logger(__name__)

EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]

DEFAULT_TIMEOUT = 60.0

#: Base64 characters per chunk event on the harness file channel. Mirrors the
#: runner constant (``runner/src/chunking.py`` ``CHUNK_B64_SIZE``) and the
#: webapp copy (``webapp/src/lib/fileChunks.ts`` ``FILE_CHUNK_B64_SIZE``):
#: 256 KiB base64 per event stays far below Daphne's default 1 MiB
#: inbound message/frame cap (which dropped oversize frames as
#: disconnects). Engine.IO keeps its separate 200 MiB HTTP buffer for
#: pre-existing monolithic non-chunk events.
HARNESS_CHUNK_B64_SIZE = 256 * 1024

#: Max buffered base64 chars for one chunked harness read. 100 MiB of raw
#: bytes encode to ~139.81 M base64 chars; cap slightly above so valid
#: reads up to the runner's absolute read cap fit, but unbounded streams
#: fail closed. Requests pass their negotiated ``max_size`` (runner
#: default 5 MiB when omitted); explicit values may go up to 100 MiB.
HARNESS_READ_MAX_CHARS = 141 * 1024 * 1024

#: Max chunks for one chunked harness read. 100 MiB of raw bytes encode
#: to ~534 chunks at 256 KiB base64; the cap leaves headroom for padding
#: while rejecting unbounded ``total_chunks`` announcements.
HARNESS_READ_MAX_CHUNKS = 560

#: Max chunks for one chunked harness write. 10 MiB of raw bytes encode
#: to ~52 chunks; 64 leaves headroom and matches the runner upload cap.
HARNESS_WRITE_MAX_CHUNKS = 64

#: Max decoded bytes for a chunked harness write. Matches the runner's
#: ``FILE_UPLOAD_MAX_SIZE`` (10 MiB) so the backend never buffers more.
HARNESS_WRITE_MAX_BYTES = 10 * 1024 * 1024

#: Max raw bytes for one chunked harness read. Matches the runner's
#: absolute read cap so the backend never buffers more than the runner
#: can legitimately send.
HARNESS_READ_MAX_BYTES = 100 * 1024 * 1024

#: Default read budget (raw bytes) when the caller passes max_size=None.
#: Mirrors the runner's FILE_READ_DEFAULT_MAX_SIZE (5 MiB) — NOT the
#: absolute 100 MiB cap. Explicit max_size may still go up to 100 MiB.
HARNESS_READ_DEFAULT_BYTES = 5 * 1024 * 1024


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
    return accessor._deliver_chunk(data)


def route_harness_done(data: dict[str, Any]) -> bool:
    """Deliver a ``harness:exec_done`` payload to its accessor."""
    accessor = _ACCESSORS_BY_REQUEST.get(str(data.get("request_id", "")))
    if accessor is None:
        log.warning(
            "harness_done_unknown_request",
            request_id=data.get("request_id", ""),
        )
        return False
    return accessor._deliver_done(data)


def route_harness_result(data: dict[str, Any]) -> bool:
    """Deliver a ``harness:*_result`` payload to its accessor."""
    accessor = _ACCESSORS_BY_REQUEST.get(str(data.get("request_id", "")))
    if accessor is None:
        log.warning(
            "harness_result_unknown_request",
            request_id=data.get("request_id", ""),
        )
        return False
    return accessor._deliver_result(data)


def route_harness_file_chunk(data: dict[str, Any]) -> bool:
    """Deliver a ``harness:read_file_chunk`` payload to its accessor."""
    accessor = _ACCESSORS_BY_REQUEST.get(str(data.get("request_id", "")))
    if accessor is None:
        log.warning(
            "harness_file_chunk_unknown_request",
            request_id=data.get("request_id", ""),
        )
        return False
    return accessor._deliver_file_chunk(data)


#: Runner -> backend stream events route by connection_id instead of
#: request_id.  The owning accessor is resolved through the byte-stream
#: registry (connection ids are globally unique per backend process).
_ACCESSORS_BY_STREAM: dict[str, RunnerWorkspaceAccessor] = {}


def route_stream_output(data: dict[str, Any]) -> bool:
    """Deliver a ``workspace:stream_output`` chunk to its byte stream."""
    accessor = _ACCESSORS_BY_STREAM.get(str(data.get("connection_id", "")))
    if accessor is None:
        log.warning(
            "stream_output_unknown_connection",
            connection_id=data.get("connection_id", ""),
        )
        return False
    return accessor._deliver_stream_output(data)


def route_stream_closed(data: dict[str, Any]) -> bool:
    """Deliver a ``workspace:stream_closed`` notice to its byte stream."""
    accessor = _ACCESSORS_BY_STREAM.get(str(data.get("connection_id", "")))
    if accessor is None:
        log.warning(
            "stream_closed_unknown_connection",
            connection_id=data.get("connection_id", ""),
        )
        return False
    return accessor._deliver_stream_closed(data)


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
    run_count = getattr(process, "run_count", None)
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
        "run_count": int(run_count) if run_count is not None else None,
        "started_at": started_at.isoformat() if started_at else None,
        "ended_at": ended_at.isoformat() if ended_at else None,
        "updated_at": updated_at.isoformat() if updated_at else None,
    }


#: Max raw bytes per stream chunk in either direction.
STREAM_CHUNK_SIZE = 64 * 1024

#: Bounded per-stream queue depth (chunks of at most 64KiB each).
STREAM_QUEUE_DEPTH = 64

#: ACK timeout for stream control events (start/input/close).
STREAM_CALL_TIMEOUT = 15.0

#: Bounded stderr capture per byte stream (newest bytes win). The newest
#: bytes carry the crash reason; the sanitized excerpt (not verbatim
#: content) is what failure logs and skip notes attach.
MCP_STDERR_BUFFER_BYTES = 64 * 1024

_TCP_HOST_RE = None  # compiled lazily in _validate_tcp_host


def _validate_tcp_host(host: str) -> str:
    """Validate a workspace-local TCP host (fail-closed, no shell use)."""
    import ipaddress as _ip
    import re as _re

    cleaned = (host or "").strip()
    if not cleaned or len(cleaned) > 255 or "\x00" in cleaned or "\n" in cleaned:
        raise ValueError(f"Invalid host: {host!r}")
    if cleaned in ("localhost", "127.0.0.1", "::1"):
        return cleaned
    try:
        _ip.ip_address(cleaned)
        return cleaned
    except ValueError:
        pass
    if not _re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,253}[A-Za-z0-9])?", cleaned):
        raise ValueError(f"Invalid host: {host!r}")
    return cleaned


def _validate_tcp_port(port: object) -> int:
    """Validate a workspace-local TCP port (1..65535)."""
    try:
        number = int(port)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid port: {port!r}") from exc
    if isinstance(port, bool) or not 1 <= number <= 65535:
        raise ValueError(f"Invalid port: {port!r}")
    return number


class _StreamState:
    """Bookkeeping for one open :class:`RunnerByteStream`."""

    __slots__ = (
        "connection_id",
        "queue",
        "closed",
        "close_error",
        "close_event",
        "exit_code",
        "stderr_buf",
        "stderr_total",
        "opened_at",
        "stdout_bytes",
    )

    def __init__(self, connection_id: str, queue: asyncio.Queue) -> None:
        self.connection_id = connection_id
        self.queue = queue
        self.closed = False
        self.close_error: str | None = None
        self.close_event = asyncio.Event()
        self.exit_code: int | None = None
        # Bounded stderr capture (newest bytes win) for failure
        # diagnosis. Only the sanitized excerpt ever reaches logs —
        # see ``get_stream_stderr_excerpt``.
        self.stderr_buf = bytearray()
        self.stderr_total = 0
        self.opened_at = 0.0
        self.stdout_bytes = 0

    def append_stderr(self, data: bytes) -> None:
        """Buffer bounded stderr bytes (newest win; unbounded total kept)."""
        raw = bytes(data or b"")
        if not raw:
            return
        self.stderr_total += len(raw)
        self.stderr_buf.extend(raw)
        overflow = len(self.stderr_buf) - MCP_STDERR_BUFFER_BYTES
        if overflow > 0:
            del self.stderr_buf[:overflow]

    def stderr_excerpt(self) -> str:
        """Return the sanitized stderr excerpt (secret-free, bounded)."""
        from apps.harness.mcp_client.stdio import sanitize_stderr_excerpt

        return sanitize_stderr_excerpt(bytes(self.stderr_buf))


class RunnerByteStream(WorkspaceByteStream):
    """Runner-backed byte stream (one ``workspace:stream_*`` connection)."""

    def __init__(
        self,
        accessor: RunnerWorkspaceAccessor,
        workspace_id: str,
        connection_id: str,
    ) -> None:
        super().__init__(workspace_id, connection_id)
        self._accessor = accessor
        self._closed_locally = False

    async def receive(self) -> bytes:
        """Return the next stdout chunk; ``b""`` marks clean EOF."""
        return await self._accessor._stream_receive(self.connection_id)

    async def send(self, data: bytes) -> None:
        """Write raw bytes to the stream stdin (bounded chunks)."""
        await self._accessor._stream_send(self.connection_id, data)

    async def send_eof(self) -> None:
        """Half-close the stream stdin (graceful EOF)."""
        await self._accessor._stream_send_eof(self.connection_id)

    async def aclose(self) -> None:
        """Close the stream (idempotent; remote close follows)."""
        if self._closed_locally:
            return
        self._closed_locally = True
        await self._accessor._stream_close(self.connection_id)

    async def wait_closed(self) -> int | None:
        """Wait for the remote close; return exit code when known."""
        return await self._accessor._stream_wait_closed(self.connection_id)


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
        call: Callable[[str, dict[str, Any], float | None], Awaitable[dict[str, Any]]]
        | None = None,
        on_stderr: Callable[[str, bytes], None] | None = None,
    ) -> None:
        super().__init__(workspace_id)
        self._emit = emit
        self._default_timeout = default_timeout
        self._desktop_geometry = desktop_geometry
        # Optional request/response transport for stream control events
        # (``workspace:stream_start/input/close`` need Socket.IO ACKs).
        # Falls back to :meth:`_emit_no_ack` when the service only offers
        # fire-and-forget emit — stream opens then fail closed on ACK wait.
        self._call = call
        self._on_stderr = on_stderr
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._streams: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        # Byte-stream state: connection_id -> _StreamState (bounded queues).
        self._byte_streams: dict[str, _StreamState] = {}
        # Bounded reassembly for chunked harness reads (request_id -> state).
        # Entries expire via the read timeout and are dropped on
        # timeout/error/cancel so partial transfers never linger.
        self._file_chunks: dict[str, dict[str, Any]] = {}
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

    def _deliver_result(self, data: dict[str, Any]) -> bool:
        """Resolve the pending unary request future for *data*."""
        request_id = str(data.get("request_id", ""))
        future = self._pending.get(request_id)
        if future is None or future.done():
            log.warning("harness_result_no_pending", request_id=request_id)
            return False
        self._schedule(_resolve_future, future, data)
        return True

    def _deliver_file_chunk(self, data: dict[str, Any]) -> bool:
        """Buffer one ``harness:read_file_chunk`` slice for *data*.

        The first chunk pins a strictly validated ``total_chunks``
        (1..``HARNESS_READ_MAX_CHUNKS``); every chunk must repeat the
        same total, workspace, and path. Per-chunk and total size caps
        are fail-closed: any violation drops the whole transfer and
        fails the pending read future with a small error instead of
        buffering unbounded data.
        """
        request_id = str(data.get("request_id", ""))
        future = self._pending.get(request_id)
        state = self._file_chunks.get(request_id)
        if future is None or future.done() or state is None:
            log.warning("harness_file_chunk_no_pending", request_id=request_id)
            return False

        def _store() -> None:
            try:
                try:
                    index = int(data.get("index", -1))
                    total = int(data.get("total_chunks", -1))
                except (TypeError, ValueError) as exc:
                    raise RunnerAccessorError(
                        "harness:read_file failed: invalid chunk index"
                    ) from exc
                if total <= 0 or total > HARNESS_READ_MAX_CHUNKS:
                    raise RunnerAccessorError(
                        "harness:read_file failed: invalid total_chunks"
                    )
                pinned = state.get("total_chunks") or 0
                if not pinned:
                    # First chunk pins the announced total.
                    state["total_chunks"] = total
                    pinned = total
                elif total != pinned:
                    raise RunnerAccessorError(
                        "harness:read_file failed: chunk total mismatch"
                    )
                if str(data.get("workspace_id", "")) != str(
                    state.get("workspace_id", "")
                ):
                    raise RunnerAccessorError(
                        "harness:read_file failed: workspace mismatch"
                    )
                if str(data.get("path", "")) != str(state.get("path", "")):
                    raise RunnerAccessorError(
                        "harness:read_file failed: path mismatch"
                    )
                if index < 0 or index >= pinned:
                    raise RunnerAccessorError(
                        "harness:read_file failed: chunk index out of range"
                    )
                if index in state["chunks"]:
                    raise RunnerAccessorError(
                        "harness:read_file failed: duplicate chunk"
                    )
                clean = "".join(str(data.get("content", "")).split())
                if not clean:
                    raise RunnerAccessorError(
                        "harness:read_file failed: empty chunk"
                    )
                if len(clean) > HARNESS_CHUNK_B64_SIZE:
                    raise RunnerAccessorError(
                        "harness:read_file failed: chunk too large"
                    )
                max_chars = int(
                    state.get("max_chars", HARNESS_READ_MAX_CHARS)
                )
                if state["buffered"] + len(clean) > max_chars:
                    raise RunnerAccessorError(
                        "harness:read_file failed: transfer too large"
                    )
                state["chunks"][index] = clean
                state["buffered"] += len(clean)
            except Exception as exc:
                self._file_chunks.pop(request_id, None)
                _resolve_future_exception(future, exc)

        self._schedule(_store)
        return True

    def _deliver_chunk(self, data: dict[str, Any]) -> bool:
        """Push a stream chunk into the queue for *data*."""
        request_id = str(data.get("request_id", ""))
        queue = self._streams.get(request_id)
        if queue is None:
            log.warning("harness_chunk_no_stream", request_id=request_id)
            return False
        self._schedule(
            queue.put_nowait,
            {
                "type": "chunk",
                "stream": str(data.get("stream", "stdout")),
                "data": str(data.get("data", "")),
            },
        )
        return True

    def _deliver_done(self, data: dict[str, Any]) -> bool:
        """Push the terminal stream message into the queue for *data*."""
        request_id = str(data.get("request_id", ""))
        queue = self._streams.get(request_id)
        if queue is None:
            log.warning("harness_done_no_stream", request_id=request_id)
            return False
        self._schedule(queue.put_nowait, {"type": "done", "payload": data})
        return True

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
        self._file_chunks.pop(request_id, None)

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
        safe_workdir = sanitize_exec_workdir(workdir)
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
        safe_workdir = sanitize_exec_workdir(workdir)
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
        """Read a file from the sandboxed workspace.

        The public contract is unchanged: returns complete bytes up to
        *max_size*. The runner sends large payloads as ordered
        ``harness:read_file_chunk`` slices plus a metadata-only final
        result; small payloads still arrive inline for backward
        compatibility. Incomplete or invalid chunk streams fail closed
        and clean up routing state.
        """
        safe_path = sanitize_harness_path(path)
        request_id = uuid.uuid4().hex
        result = await self._await_chunked_read_result(
            request_id,
            {
                "request_id": request_id,
                "workspace_id": self.workspace_id,
                "path": safe_path,
                "max_size": max_size,
            },
            None,
        )
        self._raise_for_error(result, "harness:read_file")
        raw_content = "".join(str(result.get("content", "")).split())
        try:
            content = (
                base64.b64decode(raw_content, validate=True)
                if raw_content
                else b""
            )
        except Exception as exc:
            raise RunnerAccessorError(
                "harness:read_file failed: invalid base64 payload"
            ) from exc
        # Plausibility against runner metadata: size must be a finite
        # non-negative int within the effective cap; decoded bytes must
        # equal size for complete reads, or be a non-empty prefix of size
        # for truncated reads (empty files are only valid non-truncated).
        try:
            effective_limit = (
                int(max_size)
                if max_size is not None
                else HARNESS_READ_DEFAULT_BYTES
            )
        except (TypeError, ValueError):
            effective_limit = HARNESS_READ_DEFAULT_BYTES
        if (
            effective_limit <= 0
            or effective_limit > HARNESS_READ_MAX_BYTES
        ):
            effective_limit = HARNESS_READ_DEFAULT_BYTES
        try:
            reported_size = int(result.get("size", len(content)))
        except (TypeError, ValueError) as exc:
            raise RunnerAccessorError(
                "harness:read_file failed: invalid size metadata"
            ) from exc
        if isinstance(result.get("size"), bool):
            raise RunnerAccessorError(
                "harness:read_file failed: invalid size metadata"
            )
        if reported_size < 0 or reported_size > HARNESS_READ_MAX_BYTES:
            raise RunnerAccessorError(
                "harness:read_file failed: size out of range"
            )
        if len(content) > effective_limit:
            raise RunnerAccessorError(
                "harness:read_file failed: payload exceeds requested max_size"
            )
        truncated = bool(result.get("truncated", False))
        if truncated:
            if reported_size == 0 or len(content) == 0:
                raise RunnerAccessorError(
                    "harness:read_file failed: truncated payload must be non-empty"
                )
            if len(content) > reported_size:
                raise RunnerAccessorError(
                    "harness:read_file failed: size mismatch"
                )
        elif len(content) != reported_size:
            raise RunnerAccessorError(
                "harness:read_file failed: size mismatch"
            )
        mime = str(
            result.get("mime") or result.get("mime_type") or guess_mime_type(
                safe_path
            )
        )
        return FileContent(
            content=content,
            size=reported_size,
            truncated=truncated,
            mime=mime,
        )

    async def _await_chunked_read_result(
        self,
        request_id: str,
        payload: dict[str, Any],
        timeout: float | None,
    ) -> dict[str, Any]:
        """Emit ``harness:read_file`` and reassemble chunked replies.

        Registers a bounded chunk buffer for *request_id*, waits for the
        final ``harness:read_file_result``, then joins ``total_chunks``
        slices in order (or uses the inline ``content`` when the runner
        sent a small backward-compatible payload).
        """
        timeout_s = self._resolve_timeout(timeout)
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._register(request_id, future=future)
        # Pin transfer identity up front; the first chunk pins the
        # announced total. max_chars follows the negotiated read cap:
        # the runner default of 5 MiB applies when max_size is omitted;
        # an explicit max_size may go up to the absolute 100 MiB cap.
        try:
            read_limit = (
                int(payload.get("max_size"))
                if payload.get("max_size") is not None
                else HARNESS_READ_DEFAULT_BYTES
            )
        except (TypeError, ValueError):
            read_limit = HARNESS_READ_DEFAULT_BYTES
        if read_limit <= 0 or read_limit > HARNESS_READ_MAX_BYTES:
            read_limit = HARNESS_READ_DEFAULT_BYTES
        max_chars = (read_limit + 2) // 3 * 4 + 4
        self._file_chunks[request_id] = {
            "total_chunks": 0,
            "chunks": {},
            "buffered": 0,
            "meta": {},
            "workspace_id": str(payload.get("workspace_id", "")),
            "path": str(payload.get("path", "")),
            "max_chars": max_chars,
        }
        completed = False
        try:
            await self._emit("harness:read_file", payload)
            result = await asyncio.wait_for(future, timeout_s)
            completed = True
            state = self._file_chunks.get(request_id, {})
            chunks: dict[int, str] = state.get("chunks", {})
            if result.get("error"):
                return result
            if result.get("chunked"):
                try:
                    total = int(result.get("total_chunks", 0))
                except (TypeError, ValueError) as exc:
                    raise RunnerAccessorError(
                        "harness:read_file failed: invalid total_chunks"
                    ) from exc
                if total <= 0 or total > HARNESS_READ_MAX_CHUNKS:
                    raise RunnerAccessorError(
                        "harness:read_file failed: invalid total_chunks"
                    )
                # The final metadata must match the pinned chunk stream.
                pinned = int(state.get("total_chunks", 0) or 0)
                if pinned and pinned != total:
                    raise RunnerAccessorError(
                        "harness:read_file failed: chunk total mismatch"
                    )
                if not pinned:
                    state["total_chunks"] = total
                if str(result.get("workspace_id", "")) != str(
                    state.get("workspace_id", "")
                ):
                    raise RunnerAccessorError(
                        "harness:read_file failed: workspace mismatch"
                    )
                if str(result.get("path", "")) != str(state.get("path", "")):
                    raise RunnerAccessorError(
                        "harness:read_file failed: path mismatch"
                    )
                missing = [i for i in range(total) if i not in chunks]
                if missing:
                    raise RunnerAccessorError(
                        "harness:read_file failed: incomplete transfer "
                        f"(missing {len(missing)} of {total} chunks)"
                    )
                joined = "".join(chunks[i] for i in range(total))
                merged = dict(result)
                merged["content"] = joined
                # The runner announces total_chunks with metadata; keep it.
                return merged
            # Backward-compatible small inline payload.
            return result
        except TimeoutError as exc:
            raise TimeoutError(
                "Harness request 'harness:read_file' timed out "
                f"after {timeout_s:.1f}s"
            ) from exc
        finally:
            self._unregister(request_id)
            if not completed:
                future.cancel()
                await self._emit_cancel(request_id)

    @staticmethod
    def _chunk_b64_slices(payload_b64: str) -> list[str]:
        """Split whitespace-free base64 into 256 KiB slices (4-char aligned)."""
        clean = "".join(payload_b64.split())
        size = HARNESS_CHUNK_B64_SIZE - (HARNESS_CHUNK_B64_SIZE % 4)
        if not clean:
            return []
        return [clean[i : i + size] for i in range(0, len(clean), size)]

    async def write_file(
        self,
        path: str,
        content: bytes,
        mode: int = 0o644,
    ) -> None:
        """Write a file atomically into the sandboxed workspace.

        Small payloads use the legacy single ``harness:write_file`` event;
        payloads above 256 KiB base64 ride as
        ``harness:write_file_start`` + ordered ``harness:write_file_chunk``
        slices + ``harness:write_file_finish`` (all small events), so large
        writes stay far below Daphne's default 1 MiB inbound cap.
        """
        safe_path = sanitize_harness_path(path)
        if not isinstance(content, (bytes, bytearray)):
            raise ValueError("content must be bytes")
        # Exact raw cap: len(content) decides, no base64 slack involved.
        # Exactly 10 MiB is allowed, 10 MiB + 1 byte is rejected.
        if len(content) > HARNESS_WRITE_MAX_BYTES:
            raise ValueError(
                "content exceeds maximum size of "
                f"{HARNESS_WRITE_MAX_BYTES} bytes"
            )
        payload_b64 = base64.b64encode(bytes(content)).decode("ascii")
        request_id = uuid.uuid4().hex
        if len(payload_b64) <= HARNESS_CHUNK_B64_SIZE:
            result = await self._await_result(
                request_id,
                "harness:write_file",
                {
                    "request_id": request_id,
                    "workspace_id": self.workspace_id,
                    "path": safe_path,
                    "content": payload_b64,
                    "mode": mode,
                },
                None,
            )
            self._raise_for_error(result, "harness:write_file")
            return
        slices = self._chunk_b64_slices(payload_b64)
        total = len(slices)
        if total > HARNESS_WRITE_MAX_CHUNKS:
            raise ValueError(
                "content exceeds maximum size of "
                f"{HARNESS_WRITE_MAX_BYTES} bytes"
            )
        completion: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._register(request_id, future=completion)
        completed = False
        try:
            await self._emit(
                "harness:write_file_start",
                {
                    "request_id": request_id,
                    "workspace_id": self.workspace_id,
                    "path": safe_path,
                    "mode": mode,
                    "total_chunks": total,
                },
            )
            for index, piece in enumerate(slices):
                await self._emit(
                    "harness:write_file_chunk",
                    {
                        "request_id": request_id,
                        "workspace_id": self.workspace_id,
                        "path": safe_path,
                        "index": index,
                        "total_chunks": total,
                        "content": piece,
                    },
                )
            await self._emit(
                "harness:write_file_finish",
                {
                    "request_id": request_id,
                    "workspace_id": self.workspace_id,
                    "path": safe_path,
                },
            )
            timeout_s = self._resolve_timeout(None)
            result = await asyncio.wait_for(completion, timeout_s)
            completed = True
            self._raise_for_error(result, "harness:write_file")
        except TimeoutError as exc:
            raise TimeoutError(
                "Harness request 'harness:write_file' timed out "
                f"after {self._resolve_timeout(None):.1f}s"
            ) from exc
        finally:
            self._unregister(request_id)
            if not completed:
                completion.cancel()
                await self._emit_cancel(request_id)

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
        safe_workdir = sanitize_exec_workdir(workdir or "/workspace")
        service = self._runner_service()
        try:
            process = await service.start_process(
                self._workspace_uuid(),
                command.strip(),
                workdir=safe_workdir,
                env=dict(env or {}),
                name=name or "",
            )
        except (
            RunnerOfflineError,
            ConflictError,
            WorkspaceNotFoundError,
            ValueError,
        ) as exc:
            raise RunnerAccessorError(f"process_start failed: {exc}") from exc
        return _serialize_process(process)

    async def process_list(self) -> list[dict[str, Any]]:
        """List background processes via RunnerService (live-merged)."""
        service = self._runner_service()
        try:
            processes = await service.list_processes(self._workspace_uuid())
        except (
            RunnerOfflineError,
            ConflictError,
            WorkspaceNotFoundError,
            ValueError,
        ) as exc:
            raise RunnerAccessorError(f"process_list failed: {exc}") from exc
        return [_serialize_process(process) for process in processes]

    async def process_get(self, process_id: str) -> dict[str, Any]:
        """Return one background process via RunnerService (UUID or name)."""
        key = (process_id or "").strip()
        if not key:
            raise ValueError("process_id must not be empty")
        service = self._runner_service()
        try:
            process = await service.get_process(self._workspace_uuid(), key)
        except (
            RunnerOfflineError,
            ConflictError,
            WorkspaceNotFoundError,
            ValueError,
        ) as exc:
            raise RunnerAccessorError(f"process_get failed: {exc}") from exc
        return _serialize_process(process)

    async def process_stop(self, process_id: str) -> dict[str, Any]:
        """Stop a background process via RunnerService (UUID or name)."""
        key = (process_id or "").strip()
        if not key:
            raise ValueError("process_id must not be empty")
        service = self._runner_service()
        try:
            process = await service.stop_process(
                self._workspace_uuid(), key
            )
        except (
            RunnerOfflineError,
            ConflictError,
            WorkspaceNotFoundError,
            ValueError,
        ) as exc:
            raise RunnerAccessorError(f"process_stop failed: {exc}") from exc
        return _serialize_process(process)

    async def process_restart(self, process_id: str) -> dict[str, Any]:
        """Restart a background process via RunnerService (UUID or name)."""
        key = (process_id or "").strip()
        if not key:
            raise ValueError("process_id must not be empty")
        service = self._runner_service()
        try:
            process = await service.restart_process(
                self._workspace_uuid(), key
            )
        except (
            RunnerOfflineError,
            ConflictError,
            WorkspaceNotFoundError,
            ValueError,
        ) as exc:
            raise RunnerAccessorError(
                f"process_restart failed: {exc}"
            ) from exc
        return _serialize_process(process)

    async def process_delete(self, process_id: str) -> dict[str, Any]:
        """Delete a background process via RunnerService (UUID or name)."""
        key = (process_id or "").strip()
        if not key:
            raise ValueError("process_id must not be empty")
        service = self._runner_service()
        try:
            deleted_id = await service.delete_process(
                self._workspace_uuid(), key
            )
        except (
            RunnerOfflineError,
            ConflictError,
            WorkspaceNotFoundError,
            ValueError,
        ) as exc:
            raise RunnerAccessorError(f"process_delete failed: {exc}") from exc
        return {"process_id": str(deleted_id), "deleted": True}

    # -- Generic byte streams (workspace:stream_* transport) ---------------

    async def _emit_no_ack(self, event: str, payload: dict[str, Any]) -> None:
        """Fire-and-forget emit used when no ACK transport is wired."""
        await self._emit(event, payload)

    async def _stream_call(
        self,
        event: str,
        payload: dict[str, Any],
        timeout: float | None,
    ) -> dict[str, Any]:
        """Send a stream control event and wait for the runner ACK."""
        timeout_s = self._resolve_timeout(timeout)
        if self._call is not None:
            try:
                result = await asyncio.wait_for(
                    self._call(event, payload, timeout_s), timeout_s
                )
            except TimeoutError as exc:
                raise TimeoutError(
                    f"Harness request '{event}' timed out "
                    f"after {timeout_s:.1f}s"
                ) from exc
            if isinstance(result, dict):
                return result
            return {"ok": True, "result": result}
        # No ACK transport: emit without ACK and fail closed (the runner
        # cannot confirm the open; callers must not assume success).
        await self._emit_no_ack(event, payload)
        raise RunnerAccessorError(
            f"{event} failed: no acknowledgement transport configured"
        )

    def _register_byte_stream(self, connection_id: str) -> _StreamState:
        """Register bounded queue state for one open byte stream."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=STREAM_QUEUE_DEPTH
        )
        state = _StreamState(connection_id, queue)
        state.opened_at = asyncio.get_running_loop().time()
        self._byte_streams[connection_id] = state
        _ACCESSORS_BY_STREAM[connection_id] = self
        return state

    def get_stream_stderr_excerpt(self, connection_id: str = "") -> str:
        """Return the sanitized stderr excerpt for *connection_id* (or last).

        Secret-free and bounded (see
        :func:`apps.harness.mcp_client.stdio.sanitize_stderr_excerpt`):
        safe to attach to logs and skip notes. With no id, the most
        recently registered stream wins; unknown ids yield ``""``.
        """
        state: _StreamState | None = None
        if connection_id:
            state = self._byte_streams.get(str(connection_id))
        elif self._byte_streams:
            state = next(reversed(list(self._byte_streams.values())))
        if state is None:
            return ""
        try:
            return state.stderr_excerpt()
        except Exception:  # pragma: no cover - logs must never break
            return ""

    def get_stream_diagnostics(self, connection_id: str = "") -> dict[str, Any]:
        """Return secret-free lifecycle counters for one stream (or last).

        ``stderr_excerpt`` is the sanitized tail (bounded); counters
        (bytes, age, exit) tell "spawned but silent" apart from "died
        loudly" without touching payload content.
        """
        state: _StreamState | None = None
        if connection_id:
            state = self._byte_streams.get(str(connection_id))
        elif self._byte_streams:
            state = next(reversed(list(self._byte_streams.values())))
        if state is None:
            return {}
        try:
            now = asyncio.get_running_loop().time()
        except RuntimeError:  # pragma: no cover - no running loop
            now = state.opened_at
        return {
            "connection_id": state.connection_id,
            "closed": state.closed,
            "close_error": state.close_error,
            "exit_code": state.exit_code,
            "stdout_bytes": state.stdout_bytes,
            "stderr_bytes": state.stderr_total,
            "stderr_buffered": len(state.stderr_buf),
            "age_s": round(max(0.0, now - (state.opened_at or now)), 2),
            "stderr_excerpt": self.get_stream_stderr_excerpt(
                state.connection_id
            ),
        }

    def _unregister_byte_stream(self, connection_id: str) -> None:
        """Drop all routing state for one byte stream."""
        self._byte_streams.pop(connection_id, None)
        if _ACCESSORS_BY_STREAM.get(connection_id) is self:
            _ACCESSORS_BY_STREAM.pop(connection_id, None)

    def _deliver_stream_output(self, data: dict[str, Any]) -> bool:
        """Enqueue one ``workspace:stream_output`` chunk (bounded).

        Returns ``True`` (accepted) when the chunk was validated and
        queued; ``False`` for unknown/closed/mismatched/invalid streams —
        the runner treats a negative ACK as a signal to close its side.
        """
        connection_id = str(data.get("connection_id", ""))
        state = self._byte_streams.get(connection_id)
        if state is None or state.closed:
            log.warning(
                "stream_output_no_stream", connection_id=connection_id
            )
            return False
        if str(data.get("workspace_id", "")) != self.workspace_id:
            log.warning(
                "stream_output_workspace_mismatch",
                connection_id=connection_id,
            )
            return False
        stream = str(data.get("stream", "stdout"))
        if stream not in ("stdout", "stderr"):
            log.warning(
                "stream_output_bad_stream", connection_id=connection_id
            )
            return False
        raw = "".join(str(data.get("data", "")).split())
        if not raw:
            # ``started`` open-ACK marker: no bytes, but a valid control
            # signal — accept it (ACK True) without queueing payload.
            if bool(data.get("started", False)):
                return True
            log.warning(
                "stream_output_empty", connection_id=connection_id
            )
            return False
        try:
            decoded_len = len(base64.b64decode(raw, validate=True))
        except Exception:
            log.warning(
                "stream_output_bad_base64", connection_id=connection_id
            )
            return False
        if decoded_len > STREAM_CHUNK_SIZE:
            log.warning(
                "stream_output_oversize", connection_id=connection_id
            )
            return False

        def _put() -> None:
            if state.closed:
                return
            try:
                # Count stdout bytes at enqueue time (secret-free size
                # only; content never leaves the queue into logs).
                if stream == "stdout":
                    state.stdout_bytes += decoded_len
                else:
                    # Stderr is captured per stream (bounded, newest
                    # win): it stays out of the MCP stdout framing and
                    # is only ever surfaced as a sanitized excerpt.
                    try:
                        state.append_stderr(
                            base64.b64decode(raw, validate=True)
                        )
                    except Exception:  # pragma: no cover - validated above
                        pass
                state.queue.put_nowait(
                    {"type": "chunk", "stream": stream, "data": raw}
                )
            except asyncio.QueueFull:
                # Overflow: mark the failure AND best-effort remote close
                # so no process keeps running without a consumer — but
                # keep the error visible: the waiting consumer still gets
                # StreamClosedError (close_error is never cleared).
                state.closed = True
                state.close_error = "stream queue overflow"
                state.close_event.set()
                # Drain one slot for the terminal marker so a
                # blocked receiver wakes up instead of hanging.
                with contextlib.suppress(Exception):
                    try:
                        state.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    state.queue.put_nowait({"type": "closed"})
                log.warning(
                    "stream_output_queue_full",
                    connection_id=connection_id,
                )
                self._request_remote_close(connection_id)

        self._schedule(_put)
        return True

    def _request_remote_close(self, connection_id: str) -> None:
        """Best-effort remote close without touching consumer state.

        Fire-and-forget ``workspace:stream_close`` (shielded, no ACK
        wait): the local ``close_error`` is preserved so the waiting
        consumer still observes the original failure instead of a
        silent EOF.
        """

        async def _close() -> None:
            with contextlib.suppress(Exception):
                await asyncio.shield(
                    self._emit(
                        "workspace:stream_close",
                        {
                            "connection_id": connection_id,
                            "workspace_id": self.workspace_id,
                        },
                    )
                )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.create_task(_close())
        elif self._loop is not None and self._loop.is_running():
            with contextlib.suppress(RuntimeError):
                asyncio.run_coroutine_threadsafe(
                    _close(), self._loop
                )

    def _deliver_stream_closed(self, data: dict[str, Any]) -> bool:
        """Resolve one byte stream with the runner close notice."""
        connection_id = str(data.get("connection_id", ""))
        state = self._byte_streams.get(connection_id)
        if state is None:
            log.warning(
                "stream_closed_no_stream", connection_id=connection_id
            )
            return False
        if str(data.get("workspace_id", "")) != self.workspace_id:
            log.warning(
                "stream_closed_workspace_mismatch",
                connection_id=connection_id,
            )
            return False

        def _close() -> None:
            state.closed = True
            error = data.get("error")
            if error:
                state.close_error = str(error)
            else:
                try:
                    code = data.get("exit_code")
                    state.exit_code = (
                        int(code) if code is not None else None
                    )
                except (TypeError, ValueError):
                    state.exit_code = None
            state.close_event.set()
            try:
                state.queue.put_nowait({"type": "closed"})
            except asyncio.QueueFull:
                pass
            # One structured line per stream close: exit code /
            # close error plus lifecycle counters. This is the line
            # that answers "did the server die, and when" — stderr
            # content itself stays in the sanitized excerpt only.
            log.info(
                "stream_closed",
                connection_id=connection_id,
                workspace_id=self.workspace_id,
                exit_code=state.exit_code,
                close_error=state.close_error,
                stdout_bytes=state.stdout_bytes,
                stderr_bytes=state.stderr_total,
                age_s=round(
                    max(
                        0.0,
                        asyncio.get_running_loop().time()
                        - (state.opened_at or 0.0),
                    ),
                    2,
                )
                if state.opened_at
                else 0.0,
            )

        self._schedule(_close)
        return True

    def fail_all_streams(self, error: str) -> None:
        """Fail every open byte stream (runner disconnect path)."""

        def _fail() -> None:
            for state in list(self._byte_streams.values()):
                state.closed = True
                state.close_error = error
                state.close_event.set()
                with contextlib.suppress(asyncio.QueueFull):
                    state.queue.put_nowait({"type": "closed"})

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if self._loop is not None and running is not self._loop:
            with contextlib.suppress(RuntimeError):
                self._loop.call_soon_threadsafe(_fail)
                return
        _fail()

    async def cancel_stream(self, connection_id: str) -> None:
        """Cancel one open byte stream (timeout/owner-gone path).

        Sends best-effort remote close and unregisters routing state so
        a later harness-run ``aclose`` is a no-op. Fail-closed: unknown
        ids raise ``StreamClosedError``.
        """
        if connection_id not in self._byte_streams:
            raise StreamClosedError(
                f"stream {connection_id!r} is not open"
            )
        await self._stream_close(connection_id)

    async def _stream_receive(self, connection_id: str) -> bytes:
        """Return the next stdout chunk for *connection_id* (``b""`` = EOF).

        No idle timeout: MCP connections may stay idle for a long time.
        Startup/request timeouts are enforced by the later MCP layer, not
        here.  Cancellation still propagates (and the caller closes the
        stream remotely via :meth:`RunnerByteStream.aclose`).
        """
        state = self._byte_streams.get(connection_id)
        if state is None:
            raise StreamClosedError(f"stream {connection_id!r} is not open")
        while True:
            item = await state.queue.get()
            kind = item.get("type")
            if kind == "closed":
                if state.close_error:
                    raise StreamClosedError(
                        f"stream {connection_id!r} closed: "
                        f"{state.close_error}"
                    )
                return b""
            if item.get("stream") == "stderr":
                if self._on_stderr is not None:
                    try:
                        raw = "".join(
                            str(item.get("data", "")).split()
                        )
                        self._on_stderr(
                            connection_id,
                            base64.b64decode(raw, validate=True),
                        )
                    except Exception:
                        log.warning(
                            "stream_stderr_callback_failed",
                            connection_id=connection_id,
                        )
                else:
                    log.debug(
                        "stream_stderr_dropped",
                        connection_id=connection_id,
                    )
                continue
            try:
                return base64.b64decode(
                    "".join(str(item.get("data", "")).split()),
                    validate=True,
                )
            except Exception as exc:
                raise StreamClosedError(
                    f"stream {connection_id!r}: invalid chunk"
                ) from exc

    async def _stream_send(self, connection_id: str, data: bytes) -> None:
        """Write bounded bytes to one stream (ACK each input event)."""
        state = self._byte_streams.get(connection_id)
        if state is None or state.closed:
            raise StreamClosedError(f"stream {connection_id!r} is not open")
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise ValueError("data must be non-empty bytes")
        if len(data) > STREAM_CHUNK_SIZE:
            raise ValueError(
                "stream write exceeds "
                f"{STREAM_CHUNK_SIZE} byte chunk limit"
            )
        payload = {
            "connection_id": connection_id,
            "workspace_id": self.workspace_id,
            "data": base64.b64encode(bytes(data)).decode("ascii"),
        }
        try:
            result = await self._stream_call(
                "workspace:stream_input", payload, None
            )
        except StreamClosedError:
            raise
        except (TimeoutError, RunnerAccessorError) as exc:
            raise StreamClosedError(
                f"stream {connection_id!r} send failed: {exc}"
            ) from exc
        if isinstance(result, dict) and result.get("ok") is False:
            raise StreamClosedError(
                f"stream {connection_id!r} send rejected: "
                f"{result.get('error', 'unknown error')}"
            )

    async def _stream_send_eof(self, connection_id: str) -> None:
        """Half-close one stream stdin (graceful EOF, best effort)."""
        state = self._byte_streams.get(connection_id)
        if state is None or state.closed:
            return
        with contextlib.suppress(Exception):
            await asyncio.shield(
                self._stream_call(
                    "workspace:stream_close",
                    {
                        "connection_id": connection_id,
                        "workspace_id": self.workspace_id,
                        "eof": True,
                    },
                    None,
                )
            )

    async def _stream_close(self, connection_id: str) -> None:
        """Close one stream remotely (best effort, always unregisters)."""
        try:
            with contextlib.suppress(Exception):
                await asyncio.shield(
                    self._stream_call(
                        "workspace:stream_close",
                        {
                            "connection_id": connection_id,
                            "workspace_id": self.workspace_id,
                        },
                        None,
                    )
                )
        finally:
            self._unregister_byte_stream(connection_id)

    async def _stream_wait_closed(self, connection_id: str) -> int | None:
        """Wait for the runner close notice (exit code when known).

        No default timeout: waits until the runner closes the stream,
        the stream fails, or the waiter is cancelled (cancel still
        triggers a remote close via the stream's ``aclose``).
        """
        state = self._byte_streams.get(connection_id)
        if state is None:
            return None
        await state.close_event.wait()
        if state.close_error:
            raise StreamClosedError(
                f"stream {connection_id!r} closed: {state.close_error}"
            )
        return state.exit_code

    async def _open_byte_stream(
        self,
        start_payload: dict[str, Any],
        timeout: float | None,
    ) -> RunnerByteStream:
        """Register state, ACK the open, and return the byte stream.

        On any open failure (runner timeout/reject) the local state is
        rolled back *and* a best-effort remote close is sent so a slow
        runner that accepted the spawn late does not leak the process.
        """
        connection_id = str(start_payload.get("connection_id", ""))
        if not connection_id:
            raise ValueError("connection_id must not be empty")
        if connection_id in self._byte_streams:
            raise ValueError(f"Duplicate connection_id: {connection_id!r}")
        state = self._register_byte_stream(connection_id)
        try:
            result = await self._stream_call(
                "workspace:stream_start", start_payload, timeout
            )
        except Exception:
            self._request_remote_close(connection_id)
            self._unregister_byte_stream(connection_id)
            raise
        if not isinstance(result, dict) or result.get("ok") is False:
            self._request_remote_close(connection_id)
            self._unregister_byte_stream(connection_id)
            error = (
                result.get("error", "unknown error")
                if isinstance(result, dict)
                else "unknown error"
            )
            raise StreamClosedError(
                f"stream {connection_id!r} open rejected: {error}"
            )
        _ = state
        return RunnerByteStream(self, self.workspace_id, connection_id)

    async def open_process(
        self,
        command: list[str],
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> WorkspaceByteStream:
        """Open a workspace-local stdio process stream."""
        safe_workdir = sanitize_exec_workdir(workdir)
        if not isinstance(command, list) or not command:
            raise ValueError("command must be a non-empty argv list")
        argv = [str(part) for part in command]
        if any(not part or "\x00" in part for part in argv):
            raise ValueError("Invalid command argv entry")
        connection_id = uuid.uuid4().hex
        log.info(
            "stream_open",
            connection_id=connection_id,
            workspace_id=self.workspace_id,
            kind="process",
            # argv[0] only: full args may embed flags/paths that echo
            # secrets in some setups; the runner logs nothing either.
            command=argv[0],
            workdir=safe_workdir,
        )
        try:
            return await self._open_byte_stream(
                {
                    "connection_id": connection_id,
                    "workspace_id": self.workspace_id,
                    "kind": "process",
                    "command": argv,
                    "workdir": safe_workdir,
                    "env": dict(env or {}),
                },
                timeout,
            )
        except Exception as exc:
            log.warning(
                "stream_open_failed",
                connection_id=connection_id,
                workspace_id=self.workspace_id,
                kind="process",
                command=argv[0],
                error=f"{type(exc).__name__}: {exc}".strip()[:300],
            )
            raise

    async def open_tcp(
        self,
        host: str,
        port: int,
        tls: bool = False,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> WorkspaceByteStream:
        """Open a workspace-local TCP stream (DNS+connect in workspace)."""
        clean_host = _validate_tcp_host(host)
        clean_port = _validate_tcp_port(port)
        connection_id = uuid.uuid4().hex
        return await self._open_byte_stream(
            {
                "connection_id": connection_id,
                "workspace_id": self.workspace_id,
                "kind": "tcp",
                "host": clean_host,
                "port": clean_port,
                "tls": bool(tls),
                "server_hostname": (server_hostname or "").strip()
                or clean_host,
            },
            timeout,
        )


def _resolve_future(
    future: asyncio.Future[dict[str, Any]], data: dict[str, Any]
) -> None:
    """Set *future* result if it is still waiting."""
    if not future.done():
        future.set_result(data)


def _resolve_future_exception(
    future: asyncio.Future[dict[str, Any]], exc: BaseException
) -> None:
    """Fail *future* with *exc* if it is still waiting."""
    if not future.done():
        future.set_exception(exc)


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

    async def _call(
        event: str, payload: dict[str, Any], timeout: float | None = None
    ) -> dict[str, Any]:
        """ACKed call to the workspace's current runner (stream control)."""
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
            return await service.call_stream_event(
                live_runner, event, payload, timeout=STREAM_CALL_TIMEOUT
            )
        except RunnerOfflineError as exc:
            raise RunnerAccessorError(f"{event} failed: {exc}") from exc

    def _on_stderr(connection_id: str, data: bytes) -> None:
        """Record relay stderr volume (never content — may hold secrets)."""
        log.info(
            "stream_stderr",
            connection_id=connection_id,
            byte_count=len(data),
        )

    return RunnerWorkspaceAccessor(
        workspace_id,
        emit=_emit,
        default_timeout=default_timeout,
        desktop_geometry=_desktop_geometry,
        call=_call,
        on_stderr=_on_stderr,
    )
