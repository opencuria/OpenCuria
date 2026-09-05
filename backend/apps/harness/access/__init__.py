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
from .runner_accessor import RunnerWorkspaceAccessor

__all__ = [
    "DirEntry",
    "ExecChunk",
    "ExecResult",
    "FileContent",
    "FileStat",
    "RunnerWorkspaceAccessor",
    "WorkspaceAccessor",
    "sanitize_harness_path",
]
