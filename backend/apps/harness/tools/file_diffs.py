"""Pure file patch projections with serialized background computation."""

from __future__ import annotations

import asyncio
import difflib
from concurrent.futures import ThreadPoolExecutor
from functools import partial

# One lazy worker preserves the former serialized CPU/memory cost across tools
# and sessions, without occupying the event loop or the ORM's executor. Python
# joins the executor's worker at process shutdown; no per-call threads are owned.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="harness-file-diff")


def _build_patch_metadata(
    path: str, *, old_text: str, new_text: str
) -> dict[str, str | int]:
    """Build metadata from immutable text, without accessing workspace state."""
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    diff_lines = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"a/{path.lstrip('/')}",
        tofile=f"b/{path.lstrip('/')}",
        lineterm="",
    )
    unified = "\n".join(diff_lines)
    if not unified and old_text != new_text:
        unified = f"--- a/{path}\n+++ b/{path}\n(content changed)"
    diff_lines = unified.splitlines()
    return {
        "path": path,
        "unified_diff": unified,
        "additions": sum(
            1
            for line in diff_lines
            if line.startswith("+") and not line.startswith("+++")
        ),
        "deletions": sum(
            1
            for line in diff_lines
            if line.startswith("-") and not line.startswith("---")
        ),
    }


async def build_patch_metadata(
    path: str, *, old_text: str, new_text: str
) -> dict[str, str | int]:
    """Await a diff without blocking the loop or computing diffs in parallel.

    Cancelling a queued job prevents its calculation. An active calculation
    finishes in the same worker; it owns only immutable text and cannot write
    files or publish events, so cancellation can safely propagate immediately.
    """
    return await asyncio.get_running_loop().run_in_executor(
        _executor,
        partial(_build_patch_metadata, path, old_text=old_text, new_text=new_text),
    )
