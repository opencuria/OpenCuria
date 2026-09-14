"""Async stdio adapter: WorkspaceByteStream -> MCP ClientSession streams.

The workspace process is opened via
:meth:`WorkspaceAccessor.open_process`; stdout bytes are newline-delimited
JSON-RPC (incremental UTF-8 decoding, 8 MiB line/buffer cap). A single
corrupt *line* (invalid JSON) is delivered to the MCP session as an
``Exception`` and the stream continues — newline framing stays intact,
matching the SDK's own ``stdio_client`` behaviour. Framing-level
failures (oversize line/buffer, invalid UTF-8, transport errors) fail
the server: one generic transport ``Exception`` is delivered and the
read stream then closes, so the session cannot desynchronize. No
payload, exception text, or raw line is ever logged (payloads may carry
secrets).

Outgoing :class:`SessionMessage` values are serialized with
``model_dump_json(by_alias=True, exclude_none=True) + '\\n'``.

Cancellation/exit always half-closes stdin (``send_eof``), closes the
stream, and cancels the pump tasks. Stderr is surfaced only through the
runner ``on_stderr`` callback and never mixed into MCP JSON.
"""

from __future__ import annotations

import codecs
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import anyio
import mcp.types as types
from anyio.streams.memory import (
    MemoryObjectReceiveStream,
    MemoryObjectSendStream,
)
from mcp.shared.message import SessionMessage

logger = logging.getLogger(__name__)

#: Max bytes for one JSON-RPC line and for the undecoded tail buffer.
MAX_STDIO_LINE_BYTES = 8 * 1024 * 1024

_STDIO_TRANSPORT_ERROR = "MCP stdio transport error (8MiB limit)"


async def _fail(
    read_tx: MemoryObjectSendStream[SessionMessage | Exception],
) -> None:
    """Deliver one generic transport error (no payload details)."""
    try:
        await read_tx.send(ValueError(_STDIO_TRANSPORT_ERROR))
    except anyio.ClosedResourceError:
        pass


@asynccontextmanager
async def workspace_stdio_client(
    accessor: Any,
    command: list[str],
    *,
    cwd: str = "/workspace",
    env: dict[str, str] | None = None,
) -> AsyncIterator[
    tuple[
        MemoryObjectReceiveStream[SessionMessage | Exception],
        MemoryObjectSendStream[SessionMessage],
    ]
]:
    """Yield ``(read_stream, write_stream)`` for one workspace stdio server."""
    read_tx, read_rx = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    write_tx, write_rx = anyio.create_memory_object_stream[SessionMessage](0)
    stream = await accessor.open_process(list(command), cwd, dict(env or {}))
    try:
        async with anyio.create_task_group() as task_group:

            async def _stdout_reader() -> None:
                decoder = codecs.getincrementaldecoder("utf-8")("strict")
                text_tail = ""
                tail_bytes = 0
                failed = False
                try:
                    async with read_tx:
                        while True:
                            try:
                                chunk = await stream.receive()
                            except anyio.ClosedResourceError:
                                break
                            except Exception:
                                # Transport error: fail the server without
                                # leaking backend exception text.
                                await _fail(read_tx)
                                break
                            if chunk is None:
                                continue
                            if chunk == b"":
                                tail = text_tail.strip()
                                if tail and not failed:
                                    try:
                                        message = (
                                            types.JSONRPCMessage.model_validate_json(
                                                tail
                                            )
                                        )
                                    except Exception:
                                        logger.warning("mcp_stdio_tail_parse_failed")
                                        try:
                                            await read_tx.send(
                                                ValueError("MCP stdio line parse error")
                                            )
                                        except anyio.ClosedResourceError:
                                            pass
                                    else:
                                        try:
                                            await read_tx.send(SessionMessage(message))
                                        except anyio.ClosedResourceError:
                                            pass
                                break
                            if failed:
                                # Framing already failed: stop reading
                                # instead of draining until remote EOF, so a
                                # babbling server cannot spin this reader
                                # forever. The session already saw the
                                # generic transport error; the writer is
                                # cancelled via the task-group scope below.
                                break
                            if not isinstance(chunk, (bytes, bytearray)):
                                await _fail(read_tx)
                                break
                            raw = bytes(chunk)
                            if tail_bytes + len(raw) > MAX_STDIO_LINE_BYTES:
                                logger.warning("mcp_stdio_buffer_overflow")
                                await _fail(read_tx)
                                failed = True
                                continue
                            try:
                                text = decoder.decode(raw, False)
                            except UnicodeDecodeError:
                                logger.warning("mcp_stdio_invalid_utf8")
                                await _fail(read_tx)
                                failed = True
                                continue
                            if not text:
                                tail_bytes += len(raw)
                                continue
                            tail_bytes = 0
                            lines = (text_tail + text).split("\n")
                            text_tail = lines.pop()
                            tail_bytes = len(text_tail.encode("utf-8"))
                            if tail_bytes > MAX_STDIO_LINE_BYTES:
                                logger.warning("mcp_stdio_line_overflow")
                                await _fail(read_tx)
                                failed = True
                                text_tail = ""
                                tail_bytes = 0
                                continue
                            for line in lines:
                                stripped = line.strip()
                                if not stripped:
                                    continue
                                if len(stripped.encode("utf-8")) > MAX_STDIO_LINE_BYTES:
                                    logger.warning("mcp_stdio_line_overflow")
                                    await _fail(read_tx)
                                    failed = True
                                    break
                                try:
                                    message = types.JSONRPCMessage.model_validate_json(
                                        stripped
                                    )
                                except Exception:
                                    logger.warning("mcp_stdio_line_parse_failed")
                                    try:
                                        # Deliver the parse failure but keep
                                        # the stream open only for this
                                        # line: a single corrupt line must
                                        # not desynchronize later frames.
                                        # Fail-closed would close here;
                                        # the SDK contract (matching its
                                        # own stdio_client) is to surface
                                        # the exception and continue, since
                                        # newline framing stays intact.
                                        await read_tx.send(
                                            ValueError("MCP stdio line parse error")
                                        )
                                    except anyio.ClosedResourceError:
                                        break
                                    continue
                                try:
                                    await read_tx.send(SessionMessage(message))
                                except anyio.ClosedResourceError:
                                    break
                            if failed:
                                # Framing already failed (see above): the
                                # loop breaks on the next chunk, so this is
                                # unreachable — kept as a defensive reset.
                                text_tail = ""
                                tail_bytes = 0
                                try:
                                    decoder.reset()
                                except Exception:  # pragma: no cover - best effort
                                    pass
                finally:
                    try:
                        await stream.send_eof()
                    except Exception:  # pragma: no cover - best effort
                        pass

            async def _stdin_writer() -> None:
                try:
                    async with write_rx:
                        async for session_message in write_rx:
                            payload = (
                                session_message.message.model_dump_json(
                                    by_alias=True, exclude_none=True
                                )
                                + "\n"
                            )
                            try:
                                await stream.send(payload.encode("utf-8"))
                            except anyio.ClosedResourceError:
                                break
                except anyio.get_cancelled_exc_class():
                    raise
                except Exception:  # pragma: no cover - transport error
                    logger.warning("mcp_stdio_write_failed")
                finally:
                    try:
                        await stream.send_eof()
                    except Exception:  # pragma: no cover - best effort
                        pass

            task_group.start_soon(_stdout_reader)
            task_group.start_soon(_stdin_writer)
            try:
                yield read_rx, write_tx
            finally:
                task_group.cancel_scope.cancel()
    finally:
        try:
            await stream.send_eof()
        except Exception:  # pragma: no cover - best effort
            pass
        try:
            await stream.aclose()
        except Exception:  # pragma: no cover - best effort
            pass
