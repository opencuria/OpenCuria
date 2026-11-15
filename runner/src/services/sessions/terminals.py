"""Interactive PTY terminal sessions for workspaces.

Every terminal holds one guest SSH session, a resource shared with agent
streams, git and file operations. The runner therefore bounds terminal
lifetime on its own instead of trusting clients to close them:

* a lease that only the backend renews while a client is attached,
* an idle timeout without input, output or resize,
* a per-workspace cap that closes the least recently active terminal.
"""

from __future__ import annotations

import asyncio
import math
import shlex
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass, field

import structlog

from ...models import WorkspaceInfo
from ...runtime.base import PtyHandle, RuntimeBackend
from ..capture_fence import CaptureFence, live_interaction

logger = structlog.get_logger(__name__)

#: Close reasons reported with ``terminal:closed``.
CLOSED = "closed"
EXITED = "exited"
DETACHED = "detached"
IDLE = "idle"
EVICTED = "evicted"

# A guest that never acknowledges the channel close must not stall callers.
_PTY_CLOSE_TIMEOUT = 5.0

ClosedListener = Callable[[str, uuid.UUID | None, str], Awaitable[None]]


@dataclass
class TerminalSession:
    """Runtime PTY handle for an interactive terminal session."""

    handle: PtyHandle
    runtime: RuntimeBackend
    workspace_id: uuid.UUID | None = None
    last_activity: float = field(default_factory=time.monotonic)
    lease_expires_at: float = math.inf


class TerminalManager:
    """Owns terminal PTY sessions for workspaces.

    Runtime lookup, stale-cache eviction, and credential env-file path are
    injected by the runner composition root. ``on_closed`` is notified once
    per terminal, whatever ended it.
    """

    def __init__(
        self,
        runtime: RuntimeBackend | None = None,
        get_cached: Callable[[uuid.UUID], WorkspaceInfo] | None = None,
        get_runtime: Callable[[uuid.UUID], RuntimeBackend] | None = None,
        evict_workspace: Callable[[uuid.UUID], None] | None = None,
        credential_env_file: str = "/root/.opencuria-env.sh",
        *,
        lease_timeout: float = 90,
        idle_timeout: float = 3600,
        max_per_workspace: int = 4,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.capture_fence: CaptureFence | None = None
        self.on_closed: ClosedListener | None = None
        self._runtime = runtime
        self._get_cached = get_cached
        self._get_runtime = get_runtime
        self._evict_workspace = evict_workspace
        self._credential_env_file = credential_env_file
        self._lease_timeout = lease_timeout
        self._idle_timeout = idle_timeout
        self._max_per_workspace = max(1, max_per_workspace)
        self._clock = clock
        self._terminals: dict[str, TerminalSession] = {}

    @live_interaction
    async def start_terminal(
        self,
        workspace_id: uuid.UUID,
        cols: int = 80,
        rows: int = 24,
    ) -> str:
        """Open an interactive PTY shell in the workspace.

        Returns a ``terminal_id`` that identifies this PTY session.
        Persistent workspace credentials are sourced via a login shell.
        Least recently active terminals beyond the per-workspace cap are
        closed first, so the new PTY never exhausts the guest's sessions.
        """
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

        await self._close_surplus(workspace_id)

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

        terminal_id = str(uuid.uuid4())
        now = self._clock()
        self._terminals[terminal_id] = TerminalSession(
            handle=handle,
            runtime=runtime,
            workspace_id=workspace_id,
            last_activity=now,
            lease_expires_at=now + self._lease_timeout,
        )
        # Concurrent starts may have passed the cap check together.
        await self._close_surplus(workspace_id, keep=terminal_id)
        log.info("terminal_started", terminal_id=terminal_id)
        return terminal_id

    def belongs_to(self, terminal_id: str, workspace_id: uuid.UUID) -> bool:
        """Return whether *terminal_id* is open in *workspace_id*."""
        entry = self._terminals.get(terminal_id)
        return entry is not None and entry.workspace_id == workspace_id

    async def read_terminal(self, terminal_id: str) -> AsyncIterator[bytes]:
        """Yield raw bytes from the PTY as they arrive.

        Stops when the PTY is closed or returns empty data (EOF).
        """
        entry = self._terminals.get(terminal_id)
        if entry is None:
            raise ValueError(f"Terminal {terminal_id} not found")
        handle = entry.handle
        runtime = entry.runtime

        while not handle.closed:
            data = await runtime.pty_read(handle)
            if not data:
                break
            entry.last_activity = self._clock()
            yield data

    @live_interaction
    async def write_terminal(self, terminal_id: str, data: bytes) -> None:
        """Write raw bytes (user input) to the PTY stdin."""
        entry = self._terminals.get(terminal_id)
        if entry is None:
            raise ValueError(f"Terminal {terminal_id} not found")
        entry.last_activity = self._clock()
        await entry.runtime.pty_write(entry.handle, data)

    @live_interaction
    async def resize_terminal(self, terminal_id: str, cols: int, rows: int) -> None:
        """Resize the PTY window."""
        entry = self._terminals.get(terminal_id)
        if entry is None:
            raise ValueError(f"Terminal {terminal_id} not found")
        entry.last_activity = self._clock()
        await entry.runtime.pty_resize(entry.handle, cols, rows)

    def renew_terminals(
        self, leases: Iterable[tuple[str, uuid.UUID]]
    ) -> list[tuple[str, uuid.UUID]]:
        """Extend the lease of attached terminals; return the unknown ones.

        Only the backend renews leases, and only for terminals that still have
        a connected client. Unknown pairs let it drop routes to terminals that
        ended while their close notification could not be delivered.
        """
        expires_at = self._clock() + self._lease_timeout
        unknown: list[tuple[str, uuid.UUID]] = []
        for terminal_id, workspace_id in leases:
            entry = self._terminals.get(terminal_id)
            if entry is None or entry.workspace_id != workspace_id:
                unknown.append((terminal_id, workspace_id))
                continue
            entry.lease_expires_at = expires_at
        return unknown

    async def expire_terminals(self) -> None:
        """Close terminals whose lease expired or that sat idle too long."""
        now = self._clock()
        expired: list[tuple[str, str]] = []
        for terminal_id, entry in self._terminals.items():
            if now >= entry.lease_expires_at:
                expired.append((terminal_id, DETACHED))
            elif self._idle_timeout and now - entry.last_activity >= self._idle_timeout:
                expired.append((terminal_id, IDLE))
        for terminal_id, reason in expired:
            await self.close_terminal(terminal_id, reason)

    async def close_terminal(self, terminal_id: str, reason: str = CLOSED) -> bool:
        """Close a PTY session, release its SSH channel and notify once.

        Returns ``False`` when the terminal was already closed.
        """
        entry = self._terminals.pop(terminal_id, None)
        if entry is None:
            return False
        log = logger.bind(
            terminal_id=terminal_id,
            workspace_id=str(entry.workspace_id),
            reason=reason,
        )
        try:
            await asyncio.wait_for(
                entry.runtime.pty_close(entry.handle), _PTY_CLOSE_TIMEOUT
            )
        except Exception:
            log.warning("terminal_close_unconfirmed", exc_info=True)
        log.info("terminal_closed")
        if self.on_closed is not None:
            try:
                await self.on_closed(terminal_id, entry.workspace_id, reason)
            except Exception:
                log.warning("terminal_closed_notification_failed", exc_info=True)
        return True

    async def _close_surplus(
        self, workspace_id: uuid.UUID, *, keep: str | None = None
    ) -> None:
        """Close least recently active terminals so a new one fits the cap."""
        others = sorted(
            (
                (entry.last_activity, terminal_id)
                for terminal_id, entry in self._terminals.items()
                if entry.workspace_id == workspace_id and terminal_id != keep
            ),
        )
        surplus = len(others) - (self._max_per_workspace - 1)
        for _, terminal_id in others[: max(0, surplus)]:
            await self.close_terminal(terminal_id, EVICTED)
