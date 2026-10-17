"""Private, authenticated IPC primitives for supervised runner generations.

The local socket is deliberately Unix-only. Messages are bounded, length-prefixed
JSON frames so large file payloads are never truncated by line-oriented reads.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
import struct
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import structlog

_logger = structlog.get_logger(__name__)
MAX_FRAME_BYTES = 220 * 1024 * 1024
_HEADER = struct.Struct("!I")


class IPCError(RuntimeError):
    """An IPC protocol or connection failure."""


async def _read_frame(reader: asyncio.StreamReader) -> dict[str, Any]:
    header = await reader.readexactly(_HEADER.size)
    (size,) = _HEADER.unpack(header)
    if size <= 0 or size > MAX_FRAME_BYTES:
        raise IPCError("Invalid IPC frame size")
    raw = await reader.readexactly(size)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise IPCError("IPC frame must be an object")
    return value


async def _write_frame(
    writer: asyncio.StreamWriter, lock: asyncio.Lock, value: dict[str, Any]
) -> None:
    raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
    if not raw or len(raw) > MAX_FRAME_BYTES:
        raise IPCError("IPC frame exceeds configured limit")
    async with lock:
        writer.write(_HEADER.pack(len(raw)))
        writer.write(raw)
        await writer.drain()


class IPCPeer:
    """Bidirectional request/reply peer over one private Unix stream."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        request_handler: Callable[[str, dict[str, Any]], Awaitable[Any]],
        event_handler: Callable[[str, dict[str, Any]], Awaitable[None]],
        close_handler: Callable[[], None] | None = None,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.request_handler = request_handler
        self.event_handler = event_handler
        self.close_handler = close_handler
        self._write_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._closed = False
        self._serving: set[asyncio.Task] = set()
        self._reader_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start routing replies, events and incoming RPC requests."""
        self._reader_task = asyncio.create_task(self._read_loop())

    async def call(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        timeout: float | None = 120,
    ) -> Any:
        """Call the opposite process and await its explicit response."""
        if self._closed:
            raise IPCError("IPC peer is closed")
        if len(self._pending) >= 512:
            raise IPCError("IPC pending request limit exceeded")
        request_id = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await _write_frame(
                self.writer,
                self._write_lock,
                {
                    "type": "request",
                    "id": request_id,
                    "method": method,
                    "data": payload or {},
                },
            )
            return await asyncio.wait_for(future, timeout) if timeout else await future
        finally:
            self._pending.pop(request_id, None)

    async def send_event(self, event: str, data: dict[str, Any]) -> None:
        """Send a non-acknowledged event to the opposite process."""
        if self._closed:
            raise IPCError("IPC peer is closed")
        await _write_frame(
            self.writer,
            self._write_lock,
            {"type": "event", "event": event, "data": data},
        )

    async def _read_loop(self) -> None:
        try:
            while True:
                frame = await _read_frame(self.reader)
                kind = frame.get("type")
                if kind == "response":
                    future = self._pending.get(str(frame.get("id")))
                    if future is not None and not future.done():
                        if frame.get("ok"):
                            future.set_result(frame.get("data"))
                        else:
                            future.set_exception(
                                IPCError(str(frame.get("error", "IPC request failed")))
                            )
                elif kind == "event":
                    if len(self._serving) >= 512:
                        raise IPCError("IPC concurrency limit exceeded")
                    self._track_task(
                        self._serve_event(
                            str(frame.get("event", "")), frame.get("data") or {}
                        )
                    )
                elif kind == "request":
                    if len(self._serving) >= 512:
                        raise IPCError("IPC concurrency limit exceeded")
                    self._track_task(self._serve_request(frame))
                else:
                    raise IPCError("Unknown IPC frame type")
        except asyncio.CancelledError:
            raise
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except IPCError as exc:
            _logger.warning("supervised_ipc_protocol_failed", error=str(exc))
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            _logger.warning("supervised_ipc_read_failed", error=str(exc))
        finally:
            self._closed = True
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(
                        IPCError("IPC connection closed; result unknown")
                    )
            if self.close_handler is not None:
                self.close_handler()

    def _track_task(self, awaitable) -> None:
        task = asyncio.create_task(awaitable)
        self._serving.add(task)
        task.add_done_callback(self._serving.discard)

    async def _serve_event(self, event: str, data: dict[str, Any]) -> None:
        """Run a one-way event handler without blocking response processing."""
        try:
            await self.event_handler(event, data)
        except (IPCError, ValueError, OSError) as exc:
            _logger.warning("supervised_ipc_event_failed", event=event, error=str(exc))

    async def _serve_request(self, frame: dict[str, Any]) -> None:
        request_id = str(frame.get("id", ""))
        try:
            result = await self.request_handler(
                str(frame.get("method", "")), frame.get("data") or {}
            )
            response = {
                "type": "response",
                "id": request_id,
                "ok": True,
                "data": result,
            }
        except (
            IPCError,
            ValueError,
            TypeError,
            KeyError,
            OSError,
            RuntimeError,
        ) as exc:
            response = {
                "type": "response",
                "id": request_id,
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}"[:2000],
            }
        if not self._closed:
            try:
                await _write_frame(self.writer, self._write_lock, response)
            except (ConnectionError, IPCError):
                self._closed = True

    async def close(self) -> None:
        """Close the connection without changing remote workspace effects."""
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
        self.writer.close()
        await self.writer.wait_closed()


async def connect_private_socket(
    path: str,
    token: str,
    generation: str,
    *,
    request_handler: Callable[[str, dict[str, Any]], Awaitable[Any]],
    event_handler: Callable[[str, dict[str, Any]], Awaitable[None]],
) -> IPCPeer:
    """Connect to the supervisor and authenticate this same-UID worker."""
    socket_path = Path(path)
    if not hasattr(socket, "SO_PEERCRED"):
        raise IPCError("Supervised mode requires Unix peer credential support")
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    sock = writer.get_extra_info("socket")
    if sock is None:
        writer.close()
        raise IPCError("Unable to verify supervisor peer credentials")
    try:
        peer_pid, peer_uid, _ = struct.unpack(
            "3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )
    except OSError as exc:
        writer.close()
        raise IPCError("Unable to verify supervisor peer credentials") from exc
    if peer_pid <= 0 or peer_uid != os.getuid():
        writer.close()
        raise IPCError("Supervisor Unix peer credentials do not match")
    peer = IPCPeer(
        reader,
        writer,
        request_handler=request_handler,
        event_handler=event_handler,
    )
    await _write_frame(
        writer,
        peer._write_lock,
        {"type": "auth", "token": token, "generation": generation, "pid": os.getpid()},
    )
    response = await _read_frame(reader)
    if response.get("type") != "auth_ok":
        writer.close()
        raise IPCError("Supervisor rejected worker authentication")
    await peer.start()
    return peer


def peer_uid(writer: asyncio.StreamWriter) -> tuple[int, int]:
    """Return authenticated Unix peer pid/uid or fail closed."""
    sock = writer.get_extra_info("socket")
    if sock is None or not hasattr(socket, "SO_PEERCRED"):
        raise IPCError("Supervised mode requires Unix peer credential support")
    try:
        pid, uid, _ = struct.unpack(
            "3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )
    except OSError as exc:
        raise IPCError("Unable to authenticate Unix peer") from exc
    return pid, uid


def prepare_socket_path(path: str) -> None:
    """Create a private socket directory and remove only a stale socket."""
    socket_path = Path(path)
    socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(socket_path.parent, 0o700)
    try:
        mode = socket_path.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(mode):
        raise IPCError("Refusing to replace a non-socket IPC path")
    socket_path.unlink()
