"""Subagent task dispatch and bounded results for persistent child sessions."""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field

from .base import Tool, ToolContext, ToolError, ToolResult
from .spill import preserve_full_output

#: Max characters of a child answer kept in the parent tool preview.
TASK_OUTPUT_MAX_CHARS = 8000

#: Agents allowed as ``task`` targets. Anything else (notably primary
#: agents like ``build``/``plan`` and hidden agents) is rejected.
ALLOWED_SUBAGENT_TYPES = ("general", "explore", "computeruse")


class TaskArgs(BaseModel):
    """Arguments for the task (subagent) tool."""

    description: str = Field(description="Short task summary.")
    prompt: str = Field(description="Full instructions for the subagent.")
    subagent_type: str = Field(
        default="general",
        description="Subagent kind: general|explore|computeruse.",
    )
    agent: str | None = Field(
        default=None,
        description="Alias for subagent_type (general|explore|computeruse).",
    )
    model_override: str | None = Field(
        default=None, description="Optional model for the child run."
    )
    task_id: str | None = Field(
        default=None,
        description=(
            "This should only be set if you mean to resume a previous task "
            "(you can pass a prior task_id and the task will continue the "
            "same subagent session as before instead of creating a fresh one)"
        ),
    )


def validate_subagent_delegation(parent_agent: str, child_agent: str) -> None:
    """Keep a read-only researcher from delegating to a writable agent."""
    if parent_agent == "explore" and child_agent != "explore":
        raise ToolError(
            "Read-only explore agents can only delegate to explore subagents.",
            tool="task",
        )


def render_task_output(task_id: str, text: str, *, state: str = "completed") -> str:
    """Wrap child text in an OpenCode-style ``<task>`` envelope.

    The envelope carries the child session id so the parent model can
    pass it back via ``TaskArgs.task_id`` to resume the same session.
    """
    tag = "task_result"
    return f'<task id="{task_id}" state="{state}">\n<{tag}>\n{text}\n</{tag}>\n</task>'


async def build_task_result(
    task_id: str,
    output: str,
    ctx: ToolContext,
    *,
    agent: str,
    metadata: dict[str, Any],
) -> ToolResult:
    """Build a bounded child result, preserving the full answer when clipped."""
    if agent == "computeruse":
        from ..agent_s.harness import extract_recording_path, truncate_task_output

        preview, truncated = truncate_task_output(
            output, extract_recording_path(output), TASK_OUTPUT_MAX_CHARS
        )
    else:
        truncated = len(output) > TASK_OUTPUT_MAX_CHARS
        preview = output
        if truncated:
            preview = (
                output[:TASK_OUTPUT_MAX_CHARS]
                + f"\n…[truncated {len(output)} chars total]"
            )
    result = ToolResult(
        output=render_task_output(task_id, preview),
        truncated=truncated,
        metadata=metadata,
    )
    if truncated:
        await preserve_full_output(ctx, result, output)
    return result


class TaskTool(Tool):
    """Delegate to the service that owns persistent child sessions.

    Validate the requested agent and depth before dispatch. The service
    owns model selection, permissions, events, resume, and result formatting.
    """

    name = "task"
    description = (
        "Launch a subagent (general|explore|computeruse) for a subtask and "
        "return its result text. Launch multiple independent task calls in "
        "one message to run them in parallel. Subagents cannot launch further "
        "subagents beyond the depth limit and cannot use todowrite. "
        "The result includes a task_id you can reuse later to continue the "
        "same subagent session: each call starts fresh unless you pass "
        "task_id to resume (which continues with its previous messages and "
        "tool outputs). When starting fresh, the prompt must contain a "
        "highly detailed task description."
    )
    args_schema: type[BaseModel] = TaskArgs
    permission_key = "task"

    def title(self, args: BaseModel) -> str:
        """Return a short title for a task invocation."""
        assert isinstance(args, TaskArgs)
        return f"Subagent: {args.description[:80]}"

    async def execute(
        self, args: BaseModel | dict[str, object], ctx: ToolContext
    ) -> ToolResult:
        """Dispatch a child session through the configured service callback."""
        validated = self.coerce_args(args)
        assert isinstance(validated, TaskArgs)
        args = validated
        requested = args.agent or args.subagent_type or "general"
        agent = requested.strip().lower()
        if agent not in ALLOWED_SUBAGENT_TYPES:
            raise ToolError(
                f"Unknown subagent '{requested}'; "
                f"expected one of {', '.join(ALLOWED_SUBAGENT_TYPES)}.",
                tool=self.name,
            )
        if ctx.depth >= ctx.max_depth:
            raise ToolError(
                f"Subagent depth limit reached (depth={ctx.depth}, "
                f"max_depth={ctx.max_depth}); nested task calls are "
                "not allowed.",
                tool=self.name,
            )
        if ctx.run_subagent is None:
            raise ToolError(
                "Subagent execution is not wired (no run_subagent in ToolContext).",
                tool=self.name,
            )
        return await ctx.run_subagent(validated, ctx, uuid.uuid4().hex[:12])
