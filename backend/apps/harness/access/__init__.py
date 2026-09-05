"""Workspace access types for the agent harness."""

from __future__ import annotations

from .base import (
    DirEntry,
    ExecChunk,
    ExecResult,
    FileContent,
    FileStat,
    WorkspaceAccessor,
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
    "WorkspaceAccessor",
    "create_harness_accessor",
    "sanitize_harness_path",
]
