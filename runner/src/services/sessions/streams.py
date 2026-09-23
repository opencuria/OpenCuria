"""Generic stream sessions: validators plus stateful StreamManager.

Canonical home (Step 1 for constants/validators, Step 3 for state) for
the stream helpers and session management previously living in
:mod:`src.service`.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

import structlog

from ...models import WorkspaceInfo
from ...runtime.base import ProcessHandle, RuntimeBackend
from ..exec_kernel import sanitize_exec_workdir

logger = structlog.get_logger(__name__)

#: Max generic stream sessions per workspace (process + TCP relay).
STREAM_MAX_PER_WORKSPACE = 8

#: Max raw bytes per stream chunk in either direction.
STREAM_CHUNK_SIZE = 64 * 1024

#: Env keys never forwarded into a stream process (shell/runtime hijack
#: surface).  Mirrors the backend harness guard.
STREAM_BLOCKED_ENV_PREFIXES = (
    "LD_",
    "PYTHON",
    "PATH",
    "HOME",
    "SHELL",
    "IFS",
    "ENV",
    "BASH_ENV",
)
STREAM_BLOCKED_ENV_EXACT = {
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "PATH",
    "PYTHONPATH",
    "PYTHONHOME",
    "HOME",
    "SHELL",
}

#: Static workspace-local TCP relay: resolves DNS *inside* the workspace
#: and connects ``host:port``, optionally upgrading to TLS with default
#: certificate verification and ``server_hostname`` SNI.  Runs via the
#: same process transport (``python3 -u -c <constant> -- host port tls
#: server_hostname``); stdio is the raw byte stream, stderr carries the
#: relay error line on failure.  No host shell connect happens anywhere.
TCP_RELAY_CODE = """\
import socket, ssl, sys

def _fail(msg):
    sys.stderr.write("relay-error: " + msg + "\\n")
    sys.stderr.flush()
    sys.exit(1)

if len(sys.argv) != 6:
    _fail("usage: relay host port tls server_hostname")
host = sys.argv[2]
try:
    port = int(sys.argv[3])
except ValueError:
    _fail("invalid port")
if not 1 <= port <= 65535:
    _fail("port out of range")
tls = sys.argv[4] == "1"
server_hostname = sys.argv[5] or None
try:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
except socket.gaierror as exc:
    _fail("dns failed: %s" % (exc,))
if not infos:
    _fail("dns returned no addresses")
last = None
raw = None
for family, socktype, proto, _canon, sockaddr in infos:
    try:
        raw = socket.socket(family, socktype, proto)
        raw.settimeout(15)
        raw.connect(sockaddr)
        last = None
        break
    except OSError as exc:
        last = exc
        try:
            raw.close()
        except OSError:
            pass
        raw = None
if raw is None:
    _fail("connect failed: %s" % (last,))
try:
    raw.settimeout(None)
    if tls:
        ctx = ssl.create_default_context()
        try:
            conn = ctx.wrap_socket(raw, server_hostname=server_hostname or host)
        except Exception as exc:
            raw.close()
            _fail("tls failed: %s" % (exc,))
    else:
        conn = raw
    fdin = sys.stdin.buffer
    fdout = sys.stdout.buffer
    import os as _os
    import select
    stdin_fd = fdin.fileno()
    stdin_eof = False
    conn_write_closed = False
    # Full-duplex: stdin EOF only half-closes the server direction —
    # the socket stays open for reading until the server sends EOF.
    # Conversely a server EOF ends the relay (pending stdin bytes were
    # already forwarded when select reported both sides readable).
    while True:
        wait_for_stdin = [] if stdin_eof else [stdin_fd]
        r, _w, _x = select.select(wait_for_stdin + [conn], [], [])
        if stdin_fd in r:
            try:
                chunk = _os.read(stdin_fd, 65536)
            except OSError as exc:
                _fail("stdin read failed: %s" % (exc,))
            if not chunk:
                stdin_eof = True
                if not conn_write_closed:
                    try:
                        conn.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    conn_write_closed = True
                # Fall through: still drain the server reply below and
                # keep looping on the socket alone until server EOF.
            else:
                try:
                    conn.sendall(chunk)
                except OSError as exc:
                    _fail("send failed: %s" % (exc,))
        if conn in r:
            try:
                data = conn.recv(65536)
            except OSError as exc:
                _fail("recv failed: %s" % (exc,))
            if not data:
                break
            try:
                fdout.write(data)
                fdout.flush()
            except OSError:
                break
except BrokenPipeError:
    pass
except Exception as exc:
    _fail("relay failed: %s" % (exc,))
finally:
    try:
        conn.close()
    except Exception:
        pass
"""

_TCP_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,253}[A-Za-z0-9])?$")


def _validate_stream_host(host: str) -> str:
    """Validate a TCP relay host (DNS name, IPv4/IPv6, or workspace-localhost)."""
    cleaned = (host or "").strip()
    if not cleaned or len(cleaned) > 255 or "\x00" in cleaned or "\n" in cleaned:
        raise ValueError(f"Invalid stream host: {host!r}")
    if cleaned in ("localhost", "127.0.0.1", "::1"):
        return cleaned
    import ipaddress as _ip

    try:
        _ip.ip_address(cleaned)
        return cleaned
    except ValueError:
        pass
    if not _TCP_HOST_RE.match(cleaned):
        raise ValueError(f"Invalid stream host: {host!r}")
    return cleaned


def _validate_stream_port(port: object) -> int:
    """Validate a TCP relay port (1..65535)."""
    try:
        number = int(port)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid stream port: {port!r}") from exc
    if isinstance(port, bool) or not 1 <= number <= 65535:
        raise ValueError(f"Invalid stream port: {port!r}")
    return number


@dataclass
class StreamSession:
    """One generic bidirectional stream bound to a workspace."""

    connection_id: str
    workspace_id: uuid.UUID
    kind: str  # "process" | "tcp"
    handle: ProcessHandle | None
    runtime: RuntimeBackend
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: bool = False


class StreamManager:
    """Owns generic bidirectional stream sessions (process / TCP relay).

    Canonical home (Step 3) for the stream session management previously
    living on ``WorkspaceService`` in :mod:`src.service`.
    ``WorkspaceService`` keeps thin delegates (same names/signatures/
    messages) plus ``_streams`` property aliases onto the manager-owned
    dict, so existing callers and tests keep working.

    Workspace resolution is injected so this module never imports
    ``src.service`` (no dependency cycle):

    - ``runtimes``: runtime backends by type (kept for introspection;
      resolution itself goes through the callables below).
    - ``get_cached``: ``(workspace_id) -> WorkspaceInfo``; raises
      ``ValueError("... not found")`` for unknown ids (same shape as
      ``WorkspaceService._get_cached``).
    - ``get_runtime``: ``(workspace_id) -> RuntimeBackend``; raises
      ``RuntimeError`` for unknown runtimes (same shape as
      ``WorkspaceService._get_runtime``).
    - ``sanitize_exec_workdir``: ``(path) -> str``; mirrors
      ``WorkspaceService._sanitize_exec_workdir`` (canonical logic in
      ``src.services.exec_kernel``).
    """

    def __init__(
        self,
        runtimes: dict[str, RuntimeBackend] | None = None,
        get_cached: Callable[[uuid.UUID], WorkspaceInfo] | None = None,
        get_runtime: Callable[[uuid.UUID], RuntimeBackend] | None = None,
        sanitize_exec_workdir: Callable[[str], str] | None = None,
    ) -> None:
        self._runtimes = runtimes if runtimes is not None else {}
        self._get_cached = get_cached
        self._get_runtime = get_runtime
        self._sanitize_exec_workdir = sanitize_exec_workdir
        self._streams: dict[str, StreamSession] = {}
        self._streams_guard = asyncio.Lock()

    @staticmethod
    def _sanitize_stream_connection_id(connection_id: str) -> str:
        """Validate a stream connection id (opaque, bounded, fail-closed)."""
        cleaned = (connection_id or "").strip()
        if (
            not cleaned
            or len(cleaned) > 128
            or "\x00" in cleaned
            or "\n" in cleaned
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", cleaned)
        ):
            raise ValueError(f"Invalid connection_id: {connection_id!r}")
        return cleaned

    @staticmethod
    def _sanitize_stream_env(
        env: dict[str, str] | None,
    ) -> dict[str, str]:
        """Validate explicit stream env (least privilege: no credential sourcing).

        Only explicitly passed entries reach the child; persistent
        workspace credential files are never sourced for streams.
        """
        if not env:
            return {}
        if not isinstance(env, dict) or len(env) > 64:
            raise ValueError("Invalid stream env")
        cleaned: dict[str, str] = {}
        for key, value in env.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError("Invalid stream env entry")
            if len(key) > 128 or len(value) > 8192:
                raise ValueError("Invalid stream env entry")
            if "\x00" in key or "\x00" in value or "\n" in key:
                raise ValueError("Invalid stream env entry")
            upper = key.upper()
            if upper in STREAM_BLOCKED_ENV_EXACT or any(
                upper.startswith(prefix) for prefix in STREAM_BLOCKED_ENV_PREFIXES
            ):
                raise ValueError(f"env var '{key}' is blocked for streams")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ValueError(f"Invalid env var name: {key!r}")
            cleaned[key] = value
        return cleaned

    @staticmethod
    def _sanitize_stream_command(command: object) -> list[str]:
        """Validate a stream argv list (never shell-joined)."""
        if not isinstance(command, list) or not command:
            raise ValueError("command must be a non-empty argv list")
        if len(command) > 64:
            raise ValueError("command has too many args")
        argv: list[str] = []
        for part in command:
            if not isinstance(part, str) or not part or len(part) > 4096:
                raise ValueError("Invalid command argv entry")
            if "\x00" in part:
                raise ValueError("Invalid command argv entry")
            argv.append(part)
        return argv

    def _stream_count_for_workspace(self, workspace_id: uuid.UUID) -> int:
        """Return the number of live streams bound to *workspace_id*."""
        return sum(
            1
            for session in self._streams.values()
            if session.workspace_id == workspace_id and not session.closed
        )

    async def stream_start_process(
        self,
        workspace_id: uuid.UUID,
        connection_id: str,
        command: list[str],
        workdir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> StreamSession:
        """Spawn a workspace-bound stdio process stream (least-privilege env).

        The slot (id + per-workspace limit) is reserved under the guard,
        but the slow ``spawn_process`` runs *outside* the guard so one
        slow spawn never head-of-line-blocks other stream operations.
        A spawn failure rolls the reservation back; a close racing the
        spawn releases the just-spawned process instead of leaking it.
        """
        conn_id = self._sanitize_stream_connection_id(connection_id)
        argv = self._sanitize_stream_command(command)
        clean_env = self._sanitize_stream_env(env)
        sanitize_workdir = self._sanitize_exec_workdir or sanitize_exec_workdir
        safe_workdir = sanitize_workdir(workdir or "/workspace")
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")
        async with self._streams_guard:
            if conn_id in self._streams:
                raise ValueError(f"Duplicate connection_id: {conn_id!r}")
            if self._stream_count_for_workspace(workspace_id) >= (
                STREAM_MAX_PER_WORKSPACE
            ):
                raise ValueError("too many streams for workspace")
            # Placeholder reservation (handle=None until spawn commits).
            self._streams[conn_id] = StreamSession(
                connection_id=conn_id,
                workspace_id=workspace_id,
                kind="process",
                handle=None,
                runtime=runtime,
            )
        try:
            handle = await runtime.spawn_process(
                info.instance_id,
                command=argv,
                workdir=safe_workdir,
                env=clean_env,
            )
        except Exception:
            async with self._streams_guard:
                reserved = self._streams.get(conn_id)
                if reserved is not None and reserved.handle is None:
                    del self._streams[conn_id]
            raise
        async with self._streams_guard:
            reserved = self._streams.get(conn_id)
            if reserved is not None and reserved.handle is None:
                reserved.handle = handle
                return reserved
        with contextlib.suppress(Exception):
            await runtime.process_close(handle)
        raise ValueError(f"Duplicate connection_id: {conn_id!r}")

    async def stream_start_tcp(
        self,
        workspace_id: uuid.UUID,
        connection_id: str,
        host: str,
        port: int,
        tls: bool = False,
        server_hostname: str | None = None,
    ) -> StreamSession:
        """Open a workspace-local TCP stream via the static in-workspace relay.

        Slot reservation + spawn-outside-guard mirrors
        :meth:`stream_start_process` (see there for the race protocol).
        """
        conn_id = self._sanitize_stream_connection_id(connection_id)
        clean_host = _validate_stream_host(host)
        clean_port = _validate_stream_port(port)
        use_tls = bool(tls)
        sni = (server_hostname or "").strip() or clean_host
        if len(sni) > 255 or "\x00" in sni or "\n" in sni:
            raise ValueError(f"Invalid server_hostname: {server_hostname!r}")
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")
        relay_argv = [
            "python3",
            "-u",
            "-c",
            TCP_RELAY_CODE,
            "--",
            clean_host,
            str(clean_port),
            "1" if use_tls else "0",
            sni,
        ]
        async with self._streams_guard:
            if conn_id in self._streams:
                raise ValueError(f"Duplicate connection_id: {conn_id!r}")
            if self._stream_count_for_workspace(workspace_id) >= (
                STREAM_MAX_PER_WORKSPACE
            ):
                raise ValueError("too many streams for workspace")
            self._streams[conn_id] = StreamSession(
                connection_id=conn_id,
                workspace_id=workspace_id,
                kind="tcp",
                handle=None,
                runtime=runtime,
            )
        try:
            handle = await runtime.spawn_process(
                info.instance_id,
                command=relay_argv,
                workdir="/workspace",
                env={},
            )
        except Exception as exc:
            async with self._streams_guard:
                reserved = self._streams.get(conn_id)
                if reserved is not None and reserved.handle is None:
                    del self._streams[conn_id]
            raise RuntimeError(
                "workspace python3 relay unavailable: "
                f"{exc}. The workspace image needs python3 for TCP streams."
            ) from exc
        async with self._streams_guard:
            reserved = self._streams.get(conn_id)
            if reserved is not None and reserved.handle is None:
                reserved.handle = handle
                return reserved
        with contextlib.suppress(Exception):
            await runtime.process_close(handle)
        raise ValueError(f"Duplicate connection_id: {conn_id!r}")

    def get_stream(self, connection_id: str) -> StreamSession:
        """Return the live session for *connection_id* or raise.

        Sessions still reserving their spawn (``handle is None``) are
        not yet usable and report as unknown — callers retry after the
        start ACK instead of observing a half-open handle.

        Public (workspace-scoped callers such as the websocket layer use
        this instead of reaching into ``_streams``).
        """
        conn_id = self._sanitize_stream_connection_id(connection_id)
        session = self._streams.get(conn_id)
        if session is None or session.closed or session.handle is None:
            raise ValueError(f"Unknown stream: {conn_id!r}")
        return session

    def _get_stream(self, connection_id: str) -> StreamSession:
        """Back-compat alias for :meth:`get_stream`."""
        return self.get_stream(connection_id)

    async def stream_read(
        self, connection_id: str, stream: str = "stdout"
    ) -> AsyncIterator[tuple[str, bytes]]:
        """Yield ``(stream, data)`` chunks until the stream ends (EOF)."""
        session = self.get_stream(connection_id)
        while True:
            stdout_data = await session.runtime.process_read(
                session.handle, "stdout", STREAM_CHUNK_SIZE
            )
            stderr_data = await session.runtime.process_read(
                session.handle, "stderr", 4096
            )
            emitted = False
            if stdout_data:
                emitted = True
                yield ("stdout", bytes(stdout_data[:STREAM_CHUNK_SIZE]))
            if stderr_data:
                emitted = True
                yield ("stderr", bytes(stderr_data[:STREAM_CHUNK_SIZE]))
            if not emitted:
                return

    async def stream_read_once(
        self,
        connection_id: str,
        stream: str = "stdout",
        size: int = STREAM_CHUNK_SIZE,
    ) -> bytes:
        """Read one bounded chunk from one stream (``b""`` on EOF)."""
        session = self.get_stream(connection_id)
        if stream not in ("stdout", "stderr"):
            raise ValueError(f"unknown stream: {stream!r}")
        data = await session.runtime.process_read(
            session.handle, stream, max(1, min(int(size), STREAM_CHUNK_SIZE))
        )
        return bytes(data)

    async def stream_write(self, connection_id: str, data: bytes) -> None:
        """Write bounded bytes to the stream stdin (per-connection locked)."""
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise ValueError("data must be non-empty bytes")
        if len(data) > STREAM_CHUNK_SIZE:
            raise ValueError("stream write exceeds 64KiB chunk limit")
        session = self.get_stream(connection_id)
        async with session.write_lock:
            await session.runtime.process_write(
                session.handle, bytes(data)
            )

    async def stream_write_eof(self, connection_id: str) -> None:
        """Half-close the stream stdin (graceful EOF)."""
        session = self.get_stream(connection_id)
        async with session.write_lock:
            await session.runtime.process_write_eof(session.handle)

    async def stream_wait(self, connection_id: str) -> int | None:
        """Wait for the stream process tree to exit; return exit code.

        Returns ``None`` when the exit code is unknown (Docker reports
        ``None`` while the exec is still ``Running`` after the pump
        drained, on framing corruption, or on inspect failures) — the
        websocket pump omits ``exit_code`` then instead of sending a
        bogus 0.
        """
        session = self.get_stream(connection_id)
        code = await session.runtime.process_wait(session.handle)
        logger.info(
            "stream_wait_result",
            connection_id=connection_id,
            exit_code=code,
        )
        return code

    async def stream_close(self, connection_id: str) -> dict[str, object]:
        """Close one stream and kill its process tree (idempotent-ish).

        ``process_close`` returns ``None`` by contract; the exit code is
        intentionally not collected here (it is reported by the pump via
        ``stream_wait`` on natural EOF). Reservations still spawning
        (``handle is None``) are dropped without touching the runtime —
        the racing spawn rolls itself back on commit.
        """
        conn_id = self._sanitize_stream_connection_id(connection_id)
        async with self._streams_guard:
            session = self._streams.pop(conn_id, None)
        if session is None:
            return {"connection_id": conn_id, "closed": False}
        session.closed = True
        if session.handle is None:
            return {"connection_id": conn_id, "closed": True}
        try:
            await session.runtime.process_close(session.handle)
        except Exception:
            logger.exception("stream_close_failed", connection_id=conn_id)
        logger.info(
            "stream_closed",
            connection_id=conn_id,
            kind=session.kind,
        )
        return {
            "connection_id": conn_id,
            "closed": True,
        }

    async def close_workspace_streams(
        self, workspace_id: uuid.UUID, *, reason: str = ""
    ) -> int:
        """Close every stream bound to *workspace_id*; return closed count."""
        async with self._streams_guard:
            targets = [
                conn_id
                for conn_id, session in self._streams.items()
                if session.workspace_id == workspace_id
            ]
            sessions = [self._streams.pop(conn_id) for conn_id in targets]
        closed = 0
        for session in sessions:
            session.closed = True
            if session.handle is None:
                continue
            try:
                await session.runtime.process_close(session.handle)
                closed += 1
            except Exception:
                logger.exception(
                    "workspace_stream_close_failed",
                    connection_id=session.connection_id,
                    reason=reason,
                )
        if targets:
            logger.info(
                "workspace_streams_closed",
                workspace_id=str(workspace_id),
                count=closed,
                reason=reason,
            )
        return closed

    async def close_all_streams(self, *, reason: str = "") -> int:
        """Close every tracked stream (shutdown/disconnect path)."""
        async with self._streams_guard:
            sessions = list(self._streams.values())
            self._streams.clear()
        closed = 0
        for session in sessions:
            session.closed = True
            if session.handle is None:
                continue
            try:
                await session.runtime.process_close(session.handle)
                closed += 1
            except Exception:
                logger.exception(
                    "stream_close_all_failed",
                    connection_id=session.connection_id,
                    reason=reason,
                )
        if sessions:
            logger.info(
                "all_streams_closed", count=closed, reason=reason
            )
        return closed
