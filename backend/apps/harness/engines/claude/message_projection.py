"""Bounded file snapshots and unified diffs for native Claude edits."""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any

from ...access.base import HARNESS_WORKSPACE_ROOT, sanitize_harness_path
from ...access.runner_accessor import RunnerAccessorError

MAX_NATIVE_FILE_SNAPSHOT_BYTES = 512 * 1024
MAX_NATIVE_UNIFIED_DIFF_BYTES = 128 * 1024


@dataclass(frozen=True)
class FileSnapshot:
    """One bounded, text-only workspace file snapshot."""

    path: str
    text: str


class NativeFileChangeTracker:
    """Capture bounded pre/post text for native Claude Write/Edit tools."""

    def __init__(self, accessor: Any) -> None:
        """Bind the workspace accessor used for both sides of an edit."""
        self.accessor = accessor

    @staticmethod
    def safe_workspace_path(raw_path: Any) -> str | None:
        """Normalize a path, declining anything outside the workspace root."""
        if not isinstance(raw_path, str) or not raw_path.strip():
            return None
        try:
            path = sanitize_harness_path(raw_path)
        except ValueError:
            return None
        if path != HARNESS_WORKSPACE_ROOT and not path.startswith(
            f"{HARNESS_WORKSPACE_ROOT}/"
        ):
            return None
        return path

    async def capture(self, path: str) -> FileSnapshot | None:
        """Read bounded UTF-8 content; missing files are represented as empty."""
        try:
            stored = await self.accessor.read_file(
                path, max_size=MAX_NATIVE_FILE_SNAPSHOT_BYTES + 1
            )
        except FileNotFoundError:
            return FileSnapshot(path, "")
        except RunnerAccessorError as exc:
            detail = str(exc).lower()
            if any(
                marker in detail
                for marker in ("no such file", "not found", "does not exist")
            ):
                return FileSnapshot(path, "")
            return None
        except (OSError, ValueError):
            return None
        content = stored.content
        if stored.truncated or len(content) > MAX_NATIVE_FILE_SNAPSHOT_BYTES:
            return None
        if b"\x00" in content[:4096]:
            return None
        try:
            return FileSnapshot(path, content.decode("utf-8"))
        except UnicodeDecodeError:
            return None

    async def capture_before(
        self, source_tool: str, arguments: dict[str, Any]
    ) -> FileSnapshot | None:
        """Capture existing file content immediately before a native mutation."""
        if source_tool not in {"Write", "Edit"}:
            return None
        path = self.safe_workspace_path(
            arguments.get("file_path") or arguments.get("path")
        )
        return await self.capture(path) if path else None

    @staticmethod
    def diff_text(before: FileSnapshot, after: FileSnapshot) -> str:
        """Build a bounded unified diff from already captured snapshots."""
        relative = before.path.removeprefix(f"{HARNESS_WORKSPACE_ROOT}/")
        unified = "\n".join(
            difflib.unified_diff(
                before.text.splitlines(keepends=True),
                after.text.splitlines(keepends=True),
                fromfile=f"a/{relative}",
                tofile=f"b/{relative}",
                lineterm="",
            )
        )
        if len(unified.encode("utf-8")) > MAX_NATIVE_UNIFIED_DIFF_BYTES:
            return ""
        return unified

    async def diff(self, before: FileSnapshot) -> dict[str, str] | None:
        """Return a bounded unified diff only after a successful real change."""
        after = await self.capture(before.path)
        if after is None or after.text == before.text:
            return None
        unified = self.diff_text(before, after)
        if not unified:
            return None
        return {"path": before.path, "unified_diff": unified}


__all__ = [
    "FileSnapshot",
    "MAX_NATIVE_FILE_SNAPSHOT_BYTES",
    "MAX_NATIVE_UNIFIED_DIFF_BYTES",
    "NativeFileChangeTracker",
]
