"""Spill truncated tool output to a workspace file (OpenCode parity).

When the registry clips a tool result, the full output is written to
``/workspace/.opencuria/tool-output/<uuid>.log`` so the model can page
through it with ``read``/``grep`` or delegate it to an explore subagent.
Spilling is best-effort: any failure is logged and never breaks the tool.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from .base import ToolContext

log = structlog.get_logger(__name__)

#: Workspace-absolute directory for spilled full tool outputs.
SPILL_DIR = "/workspace/.opencuria/tool-output"


def task_available(ctx: ToolContext) -> bool:
    """Return True when a subagent task tool could process spilled output."""
    registry = getattr(ctx, "registry", None)
    if registry is None:
        return False
    try:
        has_task = "task" in registry
    except Exception:
        return False
    return bool(has_task) and ctx.depth < ctx.max_depth


def build_spill_hint(path: str, *, task_hint: bool = False) -> str:
    """Build the recovery hint suffix appended after a spilled preview."""
    hint = f"\n\nFull output: {path}. Use read with offset/limit or grep to inspect."
    if task_hint:
        hint += " Use the task tool (explore agent) to process this file."
    return hint


async def spill_full_output(ctx: ToolContext, full_output: str) -> str | None:
    """Write *full_output* to a workspace log file; return path or None.

    Returns None (without raising) when no accessor is wired (e.g. the
    loud ``_MissingAccessor`` fallback) or when the write fails.
    At most one spill file is created per call.
    """
    if not full_output:
        return None
    accessor = getattr(ctx, "accessor", None)
    if accessor is None:
        return None
    if type(accessor).__name__ == "_MissingAccessor":
        return None
    path = f"{SPILL_DIR}/{uuid.uuid4().hex}.log"
    try:
        await accessor.write_file(path, full_output.encode("utf-8"))
    except Exception as exc:
        log.warning("tool_output_spill_failed", path=path, error=str(exc))
        return None
    return path
