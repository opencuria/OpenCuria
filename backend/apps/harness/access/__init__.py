"""Workspace access types for the agent harness."""

from __future__ import annotations

from .base import (
    DirEntry,
    ExecChunk,
    ExecResult,
    FileContent,
    FileStat,
    StreamClosedError,
    WorkspaceAccessor,
    WorkspaceByteStream,
    sanitize_exec_workdir,
    sanitize_harness_path,
)
from .runner_accessor import RunnerWorkspaceAccessor, create_harness_accessor

__all__ = [
    "DirEntry",
    "ExecChunk",
    "ExecResult",
    "FileContent",
    "FileStat",
    "RunnerWorkspaceAccessor",
    "StreamClosedError",
    "WorkspaceAccessor",
    "WorkspaceByteStream",
    "create_harness_accessor",
    "sanitize_exec_workdir",
    "sanitize_harness_path",
]
