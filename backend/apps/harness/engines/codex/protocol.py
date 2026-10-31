"""Bounded bidirectional app-server NDJSON, independent of any vendor SDK.

The reader never waits for a server request handler: tool/auth callbacks may
make RPCs themselves. Wire data and provider errors are deliberately not logged.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

MAX_FRAME = 16 * 1024 * 1024
MAX_PENDING = 64
MAX_EVENTS = 4096
MAX_QUEUE_BYTES = 32 * 1024 * 1024
MAX_SERVER_REQUESTS = 32


class CodexProtocolError(RuntimeError):
    """A transport failure, not evidence that an upstream action was rejected."""


class CodexEventBufferError(CodexProtocolError):
    """A bounded notification queue could not retain the next event."""


class CodexRPCError(CodexProtocolError):
    """An explicit, correlated rejection (never includes secret server payloads)."""

    def __init__(self, method: str, code: Any) -> None:
        """Retain only a safe correlated error code, never upstream payloads."""
        self.code = code
        super().__init__(f"Codex rejected {method} (code {code})")


class CodexProtocol:
    """Multiplex bounded notifications, client RPCs and host-executed requests."""

    def __init__(
        self,
        stream: Any,
        handler: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
    ) -> None:
        """Start one stream reader; request callbacks never block that reader."""
        self.stream = stream
        self.handler = handler
        self.events: asyncio.Queue = asyncio.Queue(MAX_EVENTS)
        self.pending: dict[int, asyncio.Future] = {}
        self.queued_bytes = 0
        self.handlers: dict[Any, asyncio.Task] = {}
        self.sequence = 0
        self.failure: BaseException | None = None
        self.failed = asyncio.Event()
        self.lock = asyncio.Lock()
        self.reader = asyncio.create_task(self._read())

    def _fail(self, exc: BaseException) -> None:
        if self.failure is not None:
            return
        self.failure = exc
        self.failed.set()
        for future in self.pending.values():
            if not future.done():
                future.set_exception(exc)
        for task in self.handlers.values():
            task.cancel()

    async def send(self, value: dict[str, Any]) -> None:
        """Write one serialized frame without interleaving concurrent requests."""
        encoded = (
            json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode()
        if len(encoded) > MAX_FRAME:
            raise CodexProtocolError("Codex outgoing frame exceeds limit")
        try:
            async with asyncio.timeout(15):
                async with self.lock:
                    if self.failure:
                        raise self.failure
                    for start in range(0, len(encoded), 65536):
                        await self.stream.send(encoded[start : start + 65536])
        except BaseException:
            self._fail(
                CodexProtocolError("Codex write interrupted; acceptance unknown")
            )
            raise

    async def request(
        self, method: str, params: dict[str, Any], timeout: float = 45
    ) -> dict[str, Any]:
        """Await one correlated reply; uncertain acceptance makes the stream fatal."""
        if self.failure:
            raise self.failure
        if len(self.pending) >= MAX_PENDING:
            raise CodexProtocolError("Codex pending request limit reached")
        self.sequence += 1
        key = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[key] = future
        try:
            await self.send({"id": key, "method": method, "params": params})
            async with asyncio.timeout(timeout):
                message = await future
            if "error" in message:
                raise CodexRPCError(method, (message.get("error") or {}).get("code"))
            result = message.get("result")
            if not isinstance(result, dict):
                raise CodexProtocolError("Codex response is not an object")
            return result
        except (TimeoutError, asyncio.CancelledError):
            self._fail(
                CodexProtocolError("Codex request interrupted; acceptance unknown")
            )
            raise
        finally:
            self.pending.pop(key, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def event(self) -> dict[str, Any]:
        """Take the next bounded notification or propagate a transport failure."""
        if self.failure:
            raise self.failure
        get = asyncio.create_task(self.events.get())
        failed = asyncio.create_task(self.failed.wait())
        try:
            await asyncio.wait({get, failed}, return_when=asyncio.FIRST_COMPLETED)
            if self.failure:
                raise self.failure
            message, size = get.result()
            self.queued_bytes -= size
            return message
        finally:
            get.cancel()
            failed.cancel()
            await asyncio.gather(get, failed, return_exceptions=True)

    async def _handle(
        self, key: str | int, method: str, params: dict[str, Any]
    ) -> None:
        try:
            result = await self.handler(method, params)
            await self.send({"id": key, "result": result})
        except asyncio.CancelledError:
            raise
        except Exception:
            try:
                await self.send(
                    {
                        "id": key,
                        "error": {
                            "code": -32603,
                            "message": "OpenCuria request denied or failed",
                        },
                    }
                )
            except Exception:
                self._fail(CodexProtocolError("Codex server response failed"))
        finally:
            self.handlers.pop(key, None)

    def _dispatch(self, message: Any) -> None:
        if not isinstance(message, dict):
            raise CodexProtocolError("Codex frame is not an object")
        method = message.get("method")
        key = message.get("id")
        if method is not None:
            if not isinstance(method, str) or not isinstance(
                message.get("params", {}), dict
            ):
                raise CodexProtocolError("Invalid Codex method frame")
            if key is not None:
                if not isinstance(key, (str, int)) or isinstance(key, bool):
                    raise CodexProtocolError("Invalid Codex request ID")
                if key in self.handlers or len(self.handlers) >= MAX_SERVER_REQUESTS:
                    raise CodexProtocolError(
                        "Codex server request limit or duplicate ID"
                    )
                self.handlers[key] = asyncio.create_task(
                    self._handle(key, method, message.get("params", {}))
                )
            else:
                try:
                    size = len(json.dumps(message).encode())
                    if self.queued_bytes + size > MAX_QUEUE_BYTES:
                        raise CodexEventBufferError("Codex notification byte limit")
                    self.events.put_nowait((message, size))
                    self.queued_bytes += size
                except asyncio.QueueFull:
                    raise CodexEventBufferError(
                        "Codex notification queue overflow"
                    ) from None
        elif isinstance(key, int) and not isinstance(key, bool):
            future = self.pending.get(key)
            if future is not None and not future.done():
                future.set_result(message)
        else:
            raise CodexProtocolError("Invalid Codex response ID")

    async def _read(self) -> None:
        pending = bytearray()
        try:
            while True:
                chunk = await self.stream.receive()
                if not chunk:
                    raise CodexProtocolError("Codex stream ended")
                if not isinstance(chunk, bytes) or len(chunk) > MAX_FRAME:
                    raise CodexProtocolError("Invalid Codex stream chunk")
                pending.extend(chunk)
                while True:
                    index = pending.find(b"\n")
                    if index < 0:
                        break
                    if index > MAX_FRAME:
                        raise CodexProtocolError("Codex frame exceeds limit")
                    line = bytes(pending[:index])
                    del pending[: index + 1]
                    if line.strip():
                        self._dispatch(json.loads(line.decode("utf-8", "strict")))
                if len(pending) > MAX_FRAME:
                    raise CodexProtocolError("Codex frame exceeds limit")
        except asyncio.CancelledError:
            raise
        except CodexProtocolError as exc:
            self._fail(exc)
        except Exception:
            self._fail(
                CodexProtocolError("Codex stream failed or contained invalid NDJSON")
            )

    async def close(self) -> None:
        """Flush the process, drain callbacks and close its managed stream."""
        # Give the app-server time to flush rollout writers and close SQLite
        # before managed tree teardown. Failure still requires lease release.
        try:
            async with asyncio.timeout(5):
                await self.stream.send_eof()
                await self.stream.wait_closed()
        except Exception:
            pass
        self._fail(CodexProtocolError("Codex protocol closed"))
        self.reader.cancel()
        tasks = [self.reader, *self.handlers.values()]
        for task in tasks:
            task.cancel()
        async with asyncio.timeout(10):
            await asyncio.gather(*tasks, return_exceptions=True)
        # aclose terminates the managed process tree. Lease.release is the
        # authoritative confirmation, including when this stream is broken.
        async with asyncio.timeout(15):
            await self.stream.aclose()
