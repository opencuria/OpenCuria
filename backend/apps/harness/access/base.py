"""WorkspaceAccessor ABC and shared types for harness workspace access.

All harness tools reach workspace files and processes exclusively through
this interface. File paths are sandboxed to ``/workspace``; exec working
directories may be any absolute path in the workspace VM/container.
Stdout and stderr of executed commands stay strictly separated.
"""

from __future__ import annotations

import abc
import mimetypes
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

HARNESS_WORKSPACE_ROOT = "/workspace"

#: Env vars never forwarded to the workspace (injected shell/runtime
#: hijack surface). Matched case-insensitively by prefix; exact names
#: in :data:`BLOCKED_ENV_EXACT` are always denied.
BLOCKED_ENV_PREFIXES = (
    "LD_",
    "PYTHON",
    "PATH",
    "HOME",
    "SHELL",
    "IFS",
    "ENV",
    "BASH_ENV",
)

#: Exact env names denied even if a prefix rule is relaxed later.
BLOCKED_ENV_EXACT = {
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "PATH",
    "PYTHONPATH",
    "PYTHONHOME",
    "HOME",
    "SHELL",
}


def validate_harness_env(env: dict[str, str] | None) -> dict[str, str]:
    """Return a copy of *env* with dangerous keys rejected.

    Args:
        env: Caller-provided extra environment (``None`` means empty).

    Raises:
        ValueError: If a key matches :data:`BLOCKED_ENV_EXACT` or a
            prefix in :data:`BLOCKED_ENV_PREFIXES` (case-insensitive).
    """
    if not env:
        return {}
    for key in env:
        upper = str(key).upper()
        blocked = upper in BLOCKED_ENV_EXACT or any(
            upper.startswith(prefix) for prefix in BLOCKED_ENV_PREFIXES
        )
        if blocked:
            raise ValueError(
                f"env var '{key}' is blocked: it would override "
                "shell/runtime search paths or home/shell resolution"
            )
    return dict(env)


def sanitize_harness_path(path: str) -> str:
    """Validate that *path* stays inside the harness workspace root.

    Args:
        path: Absolute or workspace-relative path requested by a tool.

    Returns:
        The normalized absolute path under ``/workspace``.

    Raises:
        ValueError: If the path escapes the ``/workspace`` sandbox.
    """
    if not path or not path.strip():
        raise ValueError(f"Path must be under /workspace: {path}")
    candidate = path if os.path.isabs(path) else f"/workspace/{path}"
    normalized = os.path.normpath(candidate)
    if normalized != HARNESS_WORKSPACE_ROOT and not normalized.startswith(
        HARNESS_WORKSPACE_ROOT + "/"
    ):
        raise ValueError(f"Path must be under /workspace: {path}")
    return normalized


def sanitize_exec_workdir(path: str) -> str:
    """Normalize an exec working directory (not sandboxed to /workspace).

    Relative paths resolve against ``/workspace`` and may escape it
    after ``normpath``. Absolute paths anywhere in the workspace
    filesystem are allowed. Empty input defaults to ``/workspace``.

    Raises:
        ValueError: If the path is empty after normalize, not absolute,
            or contains a NUL/newline.
    """
    raw_input = path or ""
    if "\x00" in raw_input or "\n" in raw_input:
        raise ValueError(f"Invalid workdir: {path}")
    raw = raw_input.strip() or HARNESS_WORKSPACE_ROOT
    candidate = raw if os.path.isabs(raw) else f"{HARNESS_WORKSPACE_ROOT}/{raw}"
    normalized = os.path.normpath(candidate)
    if not os.path.isabs(normalized):
        raise ValueError(f"Invalid workdir: {path}")
    return normalized


def guess_mime_type(path: str) -> str:
    """Return a best-effort MIME type for *path*."""
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


@dataclass(frozen=True)
class ExecChunk:
    """One streamed output chunk of a running command."""

    stream: str  # "stdout" or "stderr"
    data: str = ""
    exit_code: int | None = None
    done: bool = False


@dataclass(frozen=True)
class ExecResult:
    """Buffered result of a command execution."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class FileContent:
    """Content returned by ``read_file``."""

    content: bytes = b""
    size: int = 0
    truncated: bool = False
    mime: str = "application/octet-stream"


@dataclass(frozen=True)
class DirEntry:
    """A single entry returned by ``list_dir``."""

    name: str
    path: str
    is_dir: bool = False
    size: int = 0


@dataclass(frozen=True)
class FileStat:
    """Metadata returned by ``stat``."""

    path: str
    is_dir: bool = False
    size: int = 0
    mime: str = "application/octet-stream"
    extra: dict = field(default_factory=dict)


class WorkspaceAccessor(abc.ABC):
    """Abstract workspace access for the agent harness."""

    def __init__(self, workspace_id: str) -> None:
        self.workspace_id = workspace_id

    @abc.abstractmethod
    def exec_stream(
        self,
        command: list[str] | str,
        workdir: str = HARNESS_WORKSPACE_ROOT,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[ExecChunk]:
        """Stream stdout/stderr chunks; final chunk carries the exit code."""
        raise NotImplementedError
        yield ExecChunk(stream="stdout")  # pragma: no cover - iterator marker

    @abc.abstractmethod
    async def exec_wait(
        self,
        command: list[str] | str,
        workdir: str = HARNESS_WORKSPACE_ROOT,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> ExecResult:
        """Execute a command and return the buffered result."""

    @abc.abstractmethod
    async def read_file(
        self,
        path: str,
        max_size: int | None = None,
    ) -> FileContent:
        """Read a file from the sandboxed workspace."""

    @abc.abstractmethod
    async def write_file(
        self,
        path: str,
        content: bytes,
        mode: int = 0o644,
    ) -> None:
        """Write a file atomically into the sandboxed workspace."""

    @abc.abstractmethod
    async def list_dir(self, path: str) -> list[DirEntry]:
        """List directory entries inside the sandboxed workspace."""

    @abc.abstractmethod
    async def stat(self, path: str) -> FileStat:
        """Stat a path inside the sandboxed workspace."""

    @abc.abstractmethod
    async def desktop_action(
        self,
        action: str,
        args: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Run a desktop automation action in the workspace.

        Forwards to the runner via ``harness:desktop_action`` /
        ``harness:desktop_action_result``. Returns the runner result dict
        (e.g. ``ok``, ``image_b64``, ``width``, ``height`` for screenshots).
        Raises on runner-reported ``error``.
        """

    @abc.abstractmethod
    async def process_start(
        self,
        command: str,
        workdir: str = HARNESS_WORKSPACE_ROOT,
        env: dict[str, str] | None = None,
        name: str = "",
    ) -> dict[str, Any]:
        """Start a detached background process in the workspace.

        ``name`` is the identity per workspace (required by the tool
        layer): reusing an existing name restarts the same application
        in place (stable id, new log, run count +1, command/workdir
        overwritten). Returns a JSON-serializable dict (``process_id``,
        ``status``, ``pid``, ``exit_code``, ``log_path``, ``run_count``,
        ...). The backend assigns the process id; logs stay in the
        workspace and are read back via ``read_file`` using the returned
        ``log_path``.
        """

    @abc.abstractmethod
    async def process_list(self) -> list[dict[str, Any]]:
        """List background processes of the workspace (newest first)."""

    @abc.abstractmethod
    async def process_get(self, process_id: str) -> dict[str, Any]:
        """Return one background process scoped to the workspace.

        ``process_id`` accepts the process UUID or its exact name.
        """

    @abc.abstractmethod
    async def process_stop(self, process_id: str) -> dict[str, Any]:
        """Stop a background process (SIGTERM, then SIGKILL after grace).

        ``process_id`` accepts the process UUID or its exact name.
        """

    @abc.abstractmethod
    async def process_restart(self, process_id: str) -> dict[str, Any]:
        """Restart a background process on the same row (stable id, new log).

        ``process_id`` accepts the process UUID or its exact name; the
        stored command/workdir are reused.
        """

    @abc.abstractmethod
    async def process_delete(self, process_id: str) -> dict[str, Any]:
        """Delete a background process from the list (stops first if running).

        ``process_id`` accepts the process UUID or its exact name.
        Returns ``{"process_id": ..., "deleted": True}``.
        """
