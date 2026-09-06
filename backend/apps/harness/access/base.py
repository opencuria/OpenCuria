"""WorkspaceAccessor ABC and shared types for harness workspace access.

All harness tools reach workspace files and processes exclusively through
this interface. The interface sandboxes every path to ``/workspace`` and
keeps stdout and stderr of executed commands strictly separated.
"""

from __future__ import annotations

import abc
import mimetypes
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

HARNESS_WORKSPACE_ROOT = "/workspace"


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

        Returns a JSON-serializable dict (``process_id``, ``status``,
        ``pid``, ``exit_code``, ``log_path``, ...). The backend assigns
        the process id; logs stay in the workspace and are read back
        via ``read_file`` using the returned ``log_path``.
        """

    @abc.abstractmethod
    async def process_list(self) -> list[dict[str, Any]]:
        """List background processes of the workspace (newest first)."""

    @abc.abstractmethod
    async def process_get(self, process_id: str) -> dict[str, Any]:
        """Return one background process scoped to the workspace."""

    @abc.abstractmethod
    async def process_stop(self, process_id: str) -> dict[str, Any]:
        """Stop a background process (SIGTERM, then SIGKILL after grace)."""
