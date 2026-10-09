"""Claude SDK transport backed by a runner-managed workspace process.

This intentionally implements the SDK's pinned ``Transport`` protocol rather
than using its local subprocess transport: Claude Code runs inside the selected
workspace, not on the backend host.
"""

from __future__ import annotations

import asyncio
import codecs
import json
from collections.abc import AsyncIterator
from typing import Any

try:  # The SDK is installed by the engine deployment, not the base harness.
    from claude_agent_sdk import Transport as _SdkTransport
except ImportError:  # Keep native-engine imports usable before SDK installation.
    _SdkTransport = object  # type: ignore[assignment,misc]

from ...access.base import StreamClosedError, WorkspaceAccessor

STREAM_CHUNK_SIZE = 64 * 1024
MAX_JSON_LINE_BYTES = 8 * 1024 * 1024


class ClaudeWorkspaceTransport(_SdkTransport):
    """Adapt ``WorkspaceByteStream`` to Claude Agent SDK's raw NDJSON API."""

    def __init__(
        self,
        accessor: WorkspaceAccessor,
        command: list[str],
        *,
        cwd: str,
        env: dict[str, str],
        owner: dict[str, str] | None = None,
        open_timeout: float = 45.0,
        close_timeout: float = 12.0,
    ) -> None:
        self._accessor = accessor
        self._command = list(command)
        self._cwd = cwd
        self._env = dict(env)
        self._owner = dict(owner) if owner else None
        self._open_timeout = open_timeout
        self._close_timeout = close_timeout
        self._stream: Any | None = None
        self._ready = False
        self._closed = False
        self._input_ended = False
        self._write_lock = asyncio.Lock()

    async def connect(self) -> None:
        """Open the remote Claude Code process once."""
        if self._stream is not None:
            return
        kwargs: dict[str, Any] = {}
        if self._owner is not None:
            kwargs["owner"] = self._owner
        try:
            self._stream = await asyncio.wait_for(
                self._accessor.open_process(
                    self._command,
                    self._cwd,
                    self._env,
                    timeout=self._open_timeout,
                    **kwargs,
                ),
                timeout=self._open_timeout,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError as exc:
            raise TimeoutError("Claude process startup timed out") from exc
        self._ready = True

    async def write(self, data: str) -> None:
        """Write SDK control JSON as bounded UTF-8 chunks."""
        if not isinstance(data, str):
            raise TypeError("Claude transport writes must be strings")
        encoded = data.encode("utf-8")
        async with self._write_lock:
            if not self.is_ready() or self._input_ended:
                raise StreamClosedError("Claude workspace transport is not writable")
            for offset in range(0, len(encoded), STREAM_CHUNK_SIZE):
                await self._stream.send(encoded[offset : offset + STREAM_CHUNK_SIZE])

    def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        """Read complete JSON objects from the remote NDJSON stream."""
        return self._read_messages()

    async def _read_messages(self) -> AsyncIterator[dict[str, Any]]:
        if self._stream is None:
            raise StreamClosedError("Claude workspace transport is not connected")
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        pending = ""
        try:
            while True:
                chunk = await self._stream.receive()
                if chunk == b"":
                    break
                if not isinstance(chunk, (bytes, bytearray)):
                    raise StreamClosedError("Claude workspace returned invalid bytes")
                pending += decoder.decode(bytes(chunk), final=False)
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    if len(line.encode("utf-8")) > MAX_JSON_LINE_BYTES:
                        raise ValueError("Claude SDK JSON message exceeds size limit")
                    value = self._parse_line(line)
                    if value is not None:
                        yield value
                if len(pending.encode("utf-8")) > MAX_JSON_LINE_BYTES:
                    raise ValueError("Claude SDK JSON message exceeds size limit")
            pending += decoder.decode(b"", final=True)
            if pending.strip():
                value = self._parse_line(pending)
                if value is not None:
                    yield value
            exit_code = await asyncio.wait_for(
                self._stream.wait_closed(), timeout=self._close_timeout
            )
            if exit_code not in (None, 0):
                try:
                    from claude_agent_sdk import ProcessError
                except ImportError:  # pragma: no cover - SDK required at runtime
                    raise RuntimeError("Claude Code process exited unsuccessfully")
                raise ProcessError(
                    "Claude Code process exited unsuccessfully", exit_code
                )
        except asyncio.CancelledError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            await self.close()
            raise ValueError("Claude SDK stream contained invalid NDJSON") from exc
        except Exception:
            await self.close()
            raise

    @staticmethod
    def _parse_line(line: str) -> dict[str, Any] | None:
        """Parse one JSON record, ignoring blank and non-JSON diagnostics."""
        stripped = line.strip()
        if not stripped:
            return None
        if not stripped.startswith("{"):
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError:
                # The SDK CLI occasionally writes [SandboxDebug] diagnostics
                # to stdout. They are not NDJSON protocol messages.
                return None
            if not isinstance(value, dict):
                raise ValueError("Claude SDK stream record must be a JSON object")
            return value
        value = json.loads(stripped)
        if not isinstance(value, dict):
            raise ValueError("Claude SDK stream record must be a JSON object")
        return value

    def is_ready(self) -> bool:
        """Return whether the underlying workspace stream is open."""
        return self._ready and not self._closed

    async def end_input(self) -> None:
        """Half-close stdin once, allowing the CLI to flush its transcript."""
        async with self._write_lock:
            if self._stream is None or self._input_ended:
                return
            self._input_ended = True
            await self._stream.send_eof()

    async def close(self) -> None:
        """Close the remote stream, bounding waits and remaining idempotent."""
        if self._closed:
            return
        self._closed = True
        self._ready = False
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            if not self._input_ended:
                self._input_ended = True
                try:
                    await asyncio.wait_for(stream.send_eof(), timeout=2.0)
                except Exception:
                    pass
            try:
                await asyncio.wait_for(stream.wait_closed(), timeout=5.0)
            except Exception:
                pass
        finally:
            try:
                await asyncio.wait_for(stream.aclose(), timeout=self._close_timeout)
            except Exception:
                # The runner owns last-resort process cleanup for a broken
                # workspace stream; never let remote teardown hang the harness.
                pass


__all__ = ["ClaudeWorkspaceTransport", "MAX_JSON_LINE_BYTES", "STREAM_CHUNK_SIZE"]
