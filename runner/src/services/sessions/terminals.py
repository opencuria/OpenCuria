"""Interactive terminal sessions (PTY create / write / resize / close).

Canonical home (Step 2) for ``TerminalSession`` and the PTY session
management previously living on ``WorkspaceService`` in
:mod:`src.service`. ``WorkspaceService`` keeps thin delegates (same
signatures/messages) plus a ``_terminals`` property alias onto the
manager-owned dict, so existing callers and tests keep working.

Extraction owner: Step 2 (leaf cluster: terminals + images).
"""

from __future__ import annotations

import asyncio
import shlex
import uuid
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

import structlog

from ...models import WorkspaceInfo
from ...runtime.base import PtyHandle, RuntimeBackend
from ..capture_fence import CaptureFence, live_interaction

logger = structlog.get_logger(__name__)


@dataclass
class TerminalSession:
    """Runtime PTY handle plus bounded output replay state."""

    handle: PtyHandle
    runtime: RuntimeBackend
    workspace_id: uuid.UUID | None = None
    sequence: int = 0
    output: deque[tuple[int, bytes]] = field(default_factory=deque)
    output_bytes: int = 0
    output_overflow_before: int = 0
    attached: bool = True
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    input_ids: set[str] = field(default_factory=set)
    recent_input_ids: deque[str] = field(default_factory=deque)


class TerminalReplayError(ValueError):
    """A terminal output cursor predates retained bounded replay history."""


class TerminalManager:
    """Owns terminal PTY sessions for workspaces.

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
    - ``evict_workspace``: ``(workspace_id) -> None``; drops a stale
      cache entry when the runtime no longer has the instance.
      ``WorkspaceService`` passes a pop on its live ``_cache`` (a
      closure, not the dict object) so ``sync_from_runtime``
      reassignments stay correct.
    - ``credential_env_file``: guest path sourced by the login shell.
      ``WorkspaceService`` passes its ``WORKSPACE_CREDENTIAL_ENV_FILE``
      constant; the default matches it.
    """

    OUTPUT_BUFFER_BYTES = 1024 * 1024
    OUTPUT_BUFFER_CHUNKS = 4096

    def __init__(
        self,
        runtimes: dict[str, RuntimeBackend] | None = None,
        get_cached: Callable[[uuid.UUID], WorkspaceInfo] | None = None,
        get_runtime: Callable[[uuid.UUID], RuntimeBackend] | None = None,
        evict_workspace: Callable[[uuid.UUID], None] | None = None,
        credential_env_file: str = "/root/.opencuria-env.sh",
    ) -> None:
        self.capture_fence: CaptureFence | None = None
        self._runtimes = runtimes if runtimes is not None else {}
        self._get_cached = get_cached
        self._get_runtime = get_runtime
        self._evict_workspace = evict_workspace
        self._credential_env_file = credential_env_file
        self._terminals: dict[str, TerminalSession] = {}
        self._workspace_terminals: dict[uuid.UUID, str] = {}
        self._workspace_start_locks: dict[uuid.UUID, asyncio.Lock] = {}

    @live_interaction
    async def start_terminal(
        self,
        workspace_id: uuid.UUID,
        cols: int = 80,
        rows: int = 24,
        *,
        terminal_id: str | None = None,
    ) -> str:
        """Open an interactive PTY shell in the workspace.

        Returns a ``terminal_id`` that identifies this PTY session.
        Persistent workspace credentials are sourced via a login shell.
        """
        lock = self._workspace_start_locks.setdefault(workspace_id, asyncio.Lock())
        async with lock:
            return await self._start_terminal_locked(
                workspace_id, cols, rows, terminal_id=terminal_id
            )

    async def _start_terminal_locked(
        self,
        workspace_id: uuid.UUID,
        cols: int,
        rows: int,
        *,
        terminal_id: str | None = None,
    ) -> str:
        """Create no more than one live PTY per workspace.

        A supplied UUID restores a known runner PTY identity; PTY handles stay
        in this stable manager while frontend/API sockets are reattached.
        """
        existing_id = self._workspace_terminals.get(workspace_id)
        if existing_id in self._terminals:
            if terminal_id is not None and existing_id != terminal_id:
                raise ValueError("Terminal id does not match active workspace PTY")
            return existing_id
        if terminal_id is not None and terminal_id in self._terminals:
            raise ValueError("Terminal id is already attached to another workspace")
        get_cached = self._get_cached
        get_runtime = self._get_runtime
        if get_cached is None or get_runtime is None:
            raise RuntimeError("TerminalManager has no workspace lookup configured")
        log = logger.bind(workspace_id=str(workspace_id))
        info = get_cached(workspace_id)
        runtime = get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")
        if not await runtime.workspace_exists(info.instance_id):
            if self._evict_workspace is not None:
                self._evict_workspace(workspace_id)
            raise RuntimeError("Workspace instance no longer exists")

        terminal_command = [
            "/bin/bash",
            "-lc",
            (
                f"if [ -f {shlex.quote(self._credential_env_file)} ]; then "
                f". {shlex.quote(self._credential_env_file)} "
                ">/dev/null 2>&1; fi; exec /bin/bash -l"
            ),
        ]

        handle = await runtime.exec_pty(
            info.instance_id,
            cols=cols,
            rows=rows,
            workdir="/workspace",
            env={"TERM": "xterm-256color"},
            command=terminal_command,
        )

        terminal_id = terminal_id or str(uuid.uuid4())
        self._terminals[terminal_id] = TerminalSession(
            handle=handle,
            runtime=runtime,
            workspace_id=workspace_id,
        )
        self._workspace_terminals[workspace_id] = terminal_id
        log.info("terminal_started", terminal_id=terminal_id)
        return terminal_id

    def has_terminal(self, terminal_id: str) -> bool:
        """Return whether this manager still owns the PTY identity."""
        return terminal_id in self._terminals

    def output_cursor(self, terminal_id: str) -> int:
        """Return the latest PTY output sequence assigned to this terminal."""
        entry = self._terminals.get(terminal_id)
        if entry is None:
            raise ValueError(f"Terminal {terminal_id} not found")
        return entry.sequence

    def confirmed_inputs(self, terminal_id: str) -> list[str]:
        """Return recently acknowledged input ids for reconnect UI recovery."""
        entry = self._terminals.get(terminal_id)
        if entry is None:
            raise ValueError(f"Terminal {terminal_id} not found")
        return list(entry.recent_input_ids)

    async def set_attached(self, terminal_id: str, attached: bool) -> None:
        """Toggle live network delivery while preserving PTY reads and buffers."""
        entry = self._terminals.get(terminal_id)
        if entry is None:
            return
        async with entry.lock:
            entry.attached = attached

    async def detach_all(self) -> None:
        """Continue draining every PTY into bounded buffers during disconnection."""
        for terminal_id in list(self._terminals):
            await self.set_attached(terminal_id, False)

    async def reattach_terminal(
        self,
        terminal_id: str,
        after_seq: int = 0,
        *,
        deliver: Callable[[int, bytes], object] | None = None,
    ) -> list[tuple[int, bytes]]:
        """Replay sequenced output after a cursor with overflow/gap detection."""
        entry = self._terminals.get(terminal_id)
        if entry is None:
            raise ValueError(f"Terminal {terminal_id} not found")
        async with entry.lock:
            first = entry.output[0][0] if entry.output else entry.sequence + 1
            if type(after_seq) is not int or after_seq < 0:
                raise ValueError("Invalid terminal output cursor")
            if after_seq > entry.sequence:
                raise ValueError("Terminal output cursor is ahead of the PTY")
            if after_seq < first - 1:
                raise TerminalReplayError("Terminal output buffer overflowed")
            cursor = after_seq
            while True:
                if entry.output and cursor < entry.output[0][0] - 1:
                    raise TerminalReplayError("Terminal output buffer overflowed")
                pending = [(seq, chunk) for seq, chunk in entry.output if seq > cursor]
                if not pending:
                    if deliver is not None:
                        entry.attached = True
                    return []
                if deliver is None:
                    return pending
                for sequence, chunk in pending:
                    result = deliver(sequence, chunk)
                    if asyncio.iscoroutine(result):
                        try:
                            await result
                        except Exception:
                            entry.attached = False
                            raise
                    cursor = sequence

    async def read_terminal_events(
        self, terminal_id: str
    ) -> AsyncIterator[tuple[int, bytes]]:
        """Yield sequenced output, maintaining the bounded attach replay buffer."""
        entry = self._terminals.get(terminal_id)
        if entry is None:
            raise ValueError(f"Terminal {terminal_id} not found")
        while not entry.handle.closed:
            data = await entry.runtime.pty_read(entry.handle)
            if not data:
                return
            async with entry.lock:
                entry.sequence += 1
                sequence = entry.sequence
                entry.output.append((sequence, data))
                entry.output_bytes += len(data)
                while entry.output and (
                    entry.output_bytes > self.OUTPUT_BUFFER_BYTES
                    or len(entry.output) > self.OUTPUT_BUFFER_CHUNKS
                ):
                    removed_seq, removed = entry.output.popleft()
                    entry.output_bytes -= len(removed)
                    entry.output_overflow_before = removed_seq
                attached = entry.attached
            if attached:
                yield sequence, data

    async def read_terminal(self, terminal_id: str) -> AsyncIterator[bytes]:
        """Yield raw PTY output bytes for existing facade callers.

        Stops when the PTY is closed or returns empty data (EOF).
        """
        entry = self._terminals.get(terminal_id)
        if entry is None:
            raise ValueError(f"Terminal {terminal_id} not found")
        async for _, chunk in self.read_terminal_events(terminal_id):
            yield chunk

    @live_interaction
    async def write_terminal(
        self, terminal_id: str, data: bytes, *, input_id: str | None = None
    ) -> bool:
        """Write PTY stdin once per idempotency key and explicitly acknowledge."""
        entry = self._terminals.get(terminal_id)
        if entry is None:
            raise ValueError(f"Terminal {terminal_id} not found")
        async with entry.write_lock:
            if input_id and input_id in entry.input_ids:
                return False
            await entry.runtime.pty_write(entry.handle, data)
            if input_id:
                entry.input_ids.add(input_id)
                entry.recent_input_ids.append(input_id)
                while len(entry.recent_input_ids) > 4096:
                    entry.input_ids.discard(entry.recent_input_ids.popleft())
        return True

    @live_interaction
    async def resize_terminal(self, terminal_id: str, cols: int, rows: int) -> None:
        """Resize the PTY window."""
        entry = self._terminals.get(terminal_id)
        if entry is None:
            raise ValueError(f"Terminal {terminal_id} not found")
        handle = entry.handle
        runtime = entry.runtime
        await runtime.pty_resize(handle, cols, rows)

    async def close_terminal(self, terminal_id: str) -> None:
        """Close a PTY session and release resources."""
        entry = self._terminals.pop(terminal_id, None)
        if entry is None:
            return
        if entry.workspace_id is not None:
            self._workspace_terminals.pop(entry.workspace_id, None)
        await entry.runtime.pty_close(entry.handle)
        logger.info("terminal_closed", terminal_id=terminal_id)
