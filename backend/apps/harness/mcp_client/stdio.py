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

import asyncio
import codecs
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import anyio
import mcp.types as types
import structlog
from anyio.streams.memory import (
    MemoryObjectReceiveStream,
    MemoryObjectSendStream,
)
from mcp.shared.message import SessionMessage

from ..access.base import StreamClosedError
from ..access.runner_accessor import STREAM_CHUNK_SIZE

logger = structlog.get_logger(__name__)

#: Max chars of sanitized stderr kept per stream for failure diagnosis.
#: Stderr is captured per stream (bounded); on startup failure the
#: excerpt is logged and attached to the skip note. Secrets are never
#: logged verbatim: the excerpt is sanitized first (see
#: :func:`sanitize_stderr_excerpt`).
MAX_STDERR_EXCERPT_CHARS = 2000

#: Max raw stderr bytes buffered per stream for the excerpt. Bounded so
#: a chatty server cannot grow backend memory; the newest bytes win
#: (crash reasons print at the end).
MAX_STDERR_BUFFER_BYTES = 64 * 1024


def sanitize_stderr_excerpt(raw: bytes | bytearray | None) -> str:
    """Return a bounded, secret-free stderr excerpt for logs/skip notes.

    Decoding is lossy (``errors="replace"``) so binary noise cannot
    break logging. Control characters are collapsed to spaces, runs of
    whitespace/newlines are folded, and common secret assignments
    (``token=...``, ``--password ...``) are redacted — stderr of MCP
    servers may echo env or CLI material on crash. The result is capped
    at :data:`MAX_STDERR_EXCERPT_CHARS` chars.
    """
    import re as _re

    if not raw:
        return ""
    try:
        text = bytes(raw).decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover - defensive
        return ""
    text = "".join(ch if (ch.isprintable() or ch in "\n\t") else " " for ch in text)
    text = _re.sub(r"[ \t]+", " ", text)
    text = _re.sub(r"\n{2,}", "\n", text).strip()
    text = _re.sub(
        r"(?i)(token|secret|password|passwd|pwd|api[_-]?key|auth|bearer"
        r"|credential|private[_-]?key)\s*[:=]\s*\S+",
        r"\1=[redacted]",
        text,
    )
    text = _re.sub(
        r"(?i)(--(?:token|secret|password|api[_-]?key)\s+)\S+",
        r"\1[redacted]",
        text,
    )
    if len(text) > MAX_STDERR_EXCERPT_CHARS:
        text = text[:MAX_STDERR_EXCERPT_CHARS] + "…"
    return text


#: Max bytes for one JSON-RPC line and for the undecoded tail buffer.
MAX_STDIO_LINE_BYTES = 8 * 1024 * 1024

_STDIO_TRANSPORT_ERROR = "MCP stdio transport error (8MiB limit)"


def _accessor_stderr_excerpt(accessor: Any) -> str:
    """Best-effort sanitized stderr excerpt from the accessor (if any)."""
    get_excerpt = getattr(accessor, "get_stream_stderr_excerpt", None)
    if not callable(get_excerpt):  # pragma: no cover - accessor w/o stderr sink
        return ""
    try:
        raw = get_excerpt()
    except Exception:  # pragma: no cover - never break teardown for logs
        return ""
    if isinstance(raw, str):
        return raw[:MAX_STDERR_EXCERPT_CHARS]
    return sanitize_stderr_excerpt(raw)


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
    server_desc: str = "",
    timeout: float | None = None,
) -> AsyncIterator[
    tuple[
        MemoryObjectReceiveStream[SessionMessage | Exception],
        MemoryObjectSendStream[SessionMessage],
    ]
]:
    """Yield ``(read_stream, write_stream)`` for one workspace stdio server.

    Stderr is captured per stream (bounded to
    :data:`MAX_STDERR_BUFFER_BYTES`, newest bytes win) for failure
    diagnosis — but never logged verbatim: on early EOF the caller
    sanitizes it via :func:`sanitize_stderr_excerpt` first.

    The pump task group is **not** nested inside this generator's
    ``async with``: ``AsyncExitStack`` drives ``__aexit__`` via
    ``agen.athrow()`` *in the exiting task*, which runs the generator's
    ``finally`` blocks there. Exiting a task group created by a
    *different* task raises ``RuntimeError: Attempted to exit a cancel
    scope ...`` under anyio/asyncio. Instead the pumps run in a
    task group owned by a dedicated supervisor task (same task that
    entered it); teardown cancels that task and closes the memory
    streams explicitly.
    """
    read_tx, read_rx = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    write_tx, write_rx = anyio.create_memory_object_stream[SessionMessage](0)
    stream = await accessor.open_process(
        list(command), cwd, dict(env or {}), timeout=timeout
    )
    desc = server_desc or " ".join(list(command)[:1]) or "stdio-server"
    supervisor_should_run = True

    async def _run_pumps() -> None:
        """Own the pump task group (must run in the supervisor task)."""
        nonlocal supervisor_should_run
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
                                except StreamClosedError as exc:
                                    # Remote stream failed (runner close, queue
                                    # overflow, open reject): stderr reached
                                    # us only as volume before the close, so
                                    # log the failure cause now; the
                                    # sanitized tail is attached at teardown
                                    # (see mcp_stdio_stderr_excerpt below).
                                    logger.bind(
                                        server=desc,
                                        connection_id=getattr(
                                            stream, "connection_id", ""
                                        ),
                                    ).warning(
                                        "mcp_stdio_stream_closed",
                                        error=str(exc),
                                    )
                                    await _fail(read_tx)
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
                                            logger.warning(
                                                "mcp_stdio_tail_parse_failed"
                                            )
                                            try:
                                                await read_tx.send(
                                                    ValueError(
                                                        "MCP stdio line parse error"
                                                    )
                                                )
                                            except anyio.ClosedResourceError:
                                                pass
                                        else:
                                            try:
                                                await read_tx.send(
                                                    SessionMessage(message)
                                                )
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
                                    if (
                                        len(stripped.encode("utf-8"))
                                        > MAX_STDIO_LINE_BYTES
                                    ):
                                        logger.warning("mcp_stdio_line_overflow")
                                        await _fail(read_tx)
                                        failed = True
                                        break
                                    try:
                                        message = (
                                            types.JSONRPCMessage.model_validate_json(
                                                stripped
                                            )
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
                                                ValueError(
                                                    "MCP stdio line parse error"
                                                )
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
                                    # The generic workspace stream accepts at
                                    # most 64 KiB per input event. MCP JSON
                                    # (e.g. tool args or prompts) may be much
                                    # larger; framing is byte-stream based, so
                                    # split without inserting newlines.
                                    encoded = payload.encode("utf-8")
                                    for offset in range(
                                        0, len(encoded), STREAM_CHUNK_SIZE
                                    ):
                                        await stream.send(
                                            encoded[offset : offset + STREAM_CHUNK_SIZE]
                                        )
                                except (anyio.ClosedResourceError, StreamClosedError):
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
                # Park until teardown cancels us; exiting here would
                # close the pumps while the session still uses them.
                while supervisor_should_run:
                    await anyio.sleep_forever()
        except anyio.get_cancelled_exc_class():
            # Teardown path: task group exit cancels the pumps in the
            # task that owns the cancel scope (this supervisor). After
            # that, close the memory streams so session readers/writers
            # observe EOF instead of hanging on a dead transport.
            raise
        finally:
            supervisor_should_run = False
            with anyio.CancelScope(shield=True):
                for closer in (read_tx.close, write_tx.close):
                    try:
                        closer()
                    except Exception:  # pragma: no cover - best effort
                        pass

    supervisor: asyncio.Task | None = None
    try:
        import asyncio as _asyncio

        supervisor = _asyncio.get_running_loop().create_task(
            _run_pumps(), name=f"mcp-stdio-pumps-{desc}"
        )
        # Let the supervisor enter its task group before yielding, so a
        # fast failure still tears down pumps that are actually running.
        await anyio.sleep(0)
        yield read_rx, write_tx
    finally:
        supervisor_should_run = False
        if supervisor is not None:
            supervisor.cancel()
            try:
                import asyncio as _asyncio

                await _asyncio.shield(supervisor)
            except (asyncio.CancelledError, anyio.get_cancelled_exc_class()):
                pass
            except BaseException:  # pragma: no cover - teardown must not raise
                pass
        # Teardown: exactly one structured line so a failure always
        # carries the sanitized stderr tail — this is the excerpt that
        # answers "why did the server die" (e.g. missing binary,
        # bad flag, chrome crash). Secrets never appear verbatim.
        try:
            excerpt = _accessor_stderr_excerpt(accessor)
        except Exception:  # pragma: no cover - never break teardown
            excerpt = ""
        logger.bind(
            server=desc,
            connection_id=getattr(stream, "connection_id", ""),
        ).warning(
            "mcp_stdio_stderr_excerpt",
            excerpt=excerpt,
        )
        try:
            await stream.send_eof()
        except Exception:  # pragma: no cover - best effort
            pass
        try:
            await stream.aclose()
        except Exception:  # pragma: no cover - best effort
            pass
