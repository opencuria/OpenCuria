"""Interactive terminal sessions (PTY create / write / resize / close).

Canonical home (Step 2) for ``TerminalSession`` and the PTY session
management previously living on ``WorkspaceService`` in
:mod:`src.service`. ``WorkspaceService`` keeps thin delegates (same
signatures/messages) plus a ``_terminals`` property alias onto the
manager-owned dict, so existing callers and tests keep working.

Extraction owner: Step 2 (leaf cluster: terminals + images).
"""

from __future__ import annotations

import shlex
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

import structlog

from ...models import WorkspaceInfo
from ...runtime.base import PtyHandle, RuntimeBackend

logger = structlog.get_logger(__name__)


@dataclass
class TerminalSession:
    """Runtime PTY handle for an interactive terminal session."""

    handle: PtyHandle
    runtime: RuntimeBackend


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

    def __init__(
        self,
        runtimes: dict[str, RuntimeBackend] | None = None,
        get_cached: Callable[[uuid.UUID], WorkspaceInfo] | None = None,
        get_runtime: Callable[[uuid.UUID], RuntimeBackend] | None = None,
        evict_workspace: Callable[[uuid.UUID], None] | None = None,
        credential_env_file: str = "/root/.opencuria-env.sh",
    ) -> None:
        self._runtimes = runtimes if runtimes is not None else {}
        self._get_cached = get_cached
        self._get_runtime = get_runtime
        self._evict_workspace = evict_workspace
        self._credential_env_file = credential_env_file
        self._terminals: dict[str, TerminalSession] = {}

    async def start_terminal(
        self,
        workspace_id: uuid.UUID,
        cols: int = 80,
        rows: int = 24,
    ) -> str:
        """Open an interactive PTY shell in the workspace.

        Returns a ``terminal_id`` that identifies this PTY session.
        Persistent workspace credentials are sourced via a login shell.
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
        self._terminals[terminal_id] = TerminalSession(
            handle=handle,
            runtime=runtime,
        )
        log.info("terminal_started", terminal_id=terminal_id)
        return terminal_id

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
            yield data

    async def write_terminal(self, terminal_id: str, data: bytes) -> None:
        """Write raw bytes (user input) to the PTY stdin."""
        entry = self._terminals.get(terminal_id)
        if entry is None:
            raise ValueError(f"Terminal {terminal_id} not found")
        handle = entry.handle
        runtime = entry.runtime
        await runtime.pty_write(handle, data)

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
        await entry.runtime.pty_close(entry.handle)
        logger.info("terminal_closed", terminal_id=terminal_id)
