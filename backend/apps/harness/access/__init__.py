"""Workspace access contracts, independent of their runner implementation."""

from __future__ import annotations

from .base import (
    DirEntry,
    ExecResult,
    FileContent,
    FileStat,
    StreamClosedError,
    WorkspaceAccessor,
    WorkspaceByteStream,
    sanitize_exec_workdir,
    sanitize_harness_path,
)

__all__ = [
    "DirEntry",
    "ExecResult",
    "FileContent",
    "FileStat",
    "StreamClosedError",
    "WorkspaceAccessor",
    "WorkspaceByteStream",
    "sanitize_exec_workdir",
    "sanitize_harness_path",
]
