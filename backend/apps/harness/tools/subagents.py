"""Subagent (task) tool with real child-run execution (M5) and webfetch."""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from pydantic import BaseModel, Field

from .base import Tool, ToolContext, ToolError, ToolResult

log = structlog.get_logger(__name__)

# Fetch guard: never buffer more than this per URL.
WEBFETCH_MAX_BYTES = 256 * 1024
WEBFETCH_TIMEOUT = 15.0

#: Max characters of a child result forwarded as the parent tool output.
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


class TaskTool(Tool):
    """Launch a subagent child run via a fresh HarnessRunner.

    The tool spawns a child loop (in-memory history: just the sub-prompt)
    with the ``explore``/``general`` agent definitions, one level deeper
    than the parent. Depth is hard-capped via ``ToolContext``: calls at
    ``depth >= max_depth`` are rejected, and the runner withholds the
    ``task`` tool from children at the limit (see ``runner``). The model
    is inherited from the parent unless ``model_override`` is given.
    Child events propagate as ``subtask_started``/``subtask_finished``
    via ``ctx.parent_emit``; the child result returns as truncated text.
    """

    name = "task"
    description = (
        "Launch a subagent (general|explore|computeruse) for a subtask and "
        "return its result text. Subagents cannot launch further subagents "
        "beyond the depth limit and cannot use todowrite."
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
        """Run the child agent loop and return its result text."""
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
        if ctx.provider is None or ctx.registry is None:
            raise ToolError(
                "Subagent execution is not wired (no provider/registry "
                "in ToolContext).",
                tool=self.name,
            )
        from ..runner import HarnessRunner, RunOptions

        subtask_id = uuid.uuid4().hex[:12]
        if ctx.run_subagent is not None:
            return await ctx.run_subagent(validated, ctx, subtask_id)
        model = (args.model_override or ctx.model or "").strip()
        if not model:
            raise ToolError(
                "No model available for subagent run (parent model "
                "missing and no model_override given).",
                tool=self.name,
            )
        log.info(
            "task_child_started",
            session_id=ctx.session_id,
            agent=agent,
            subtask_id=subtask_id,
            depth=ctx.depth + 1,
        )
        await self._emit_parent(
            ctx,
            {
                "type": "subtask_started",
                "subtask_id": subtask_id,
                "agent": agent,
                "description": args.description,
            },
        )
        child_registry = _child_registry(ctx.registry, agent)
        child = HarnessRunner(
            provider=ctx.provider,
            tools=child_registry,
            evaluator=ctx.evaluator,
            accessor=ctx.accessor,
            emit=self._child_emit(ctx),
        )
        child_opts = RunOptions(
            history=[],
            session_id=f"{ctx.session_id}/sub-{subtask_id}",
            workspace_id=ctx.workspace_id,
            cwd=ctx.directory,
            auto_approve=True,
            depth=ctx.depth + 1,
            max_depth=ctx.max_depth,
        )
        try:
            result = await child.run(args.prompt, agent, model, "build", child_opts)
        except Exception as exc:
            await self._emit_parent(
                ctx,
                {
                    "type": "subtask_finished",
                    "subtask_id": subtask_id,
                    "agent": agent,
                    "status": "error",
                    "summary": str(exc)[:500],
                },
            )
            log.warning(
                "task_child_failed",
                session_id=ctx.session_id,
                subtask_id=subtask_id,
                error=str(exc),
            )
            raise ToolError(f"Subagent '{agent}' failed: {exc}", tool=self.name)
        output = result.output or ""
        if agent == "computeruse":
            from ..computeruse_loop import sanitize_run_id, truncate_task_output

            output, truncated = truncate_task_output(
                output,
                sanitize_run_id(child_opts.session_id),
                TASK_OUTPUT_MAX_CHARS,
            )
        else:
            truncated = len(output) > TASK_OUTPUT_MAX_CHARS
            if truncated:
                output = (
                    output[:TASK_OUTPUT_MAX_CHARS]
                    + f"\n…[truncated {len(result.output)} chars total]"
                )
        await self._emit_parent(
            ctx,
            {
                "type": "subtask_finished",
                "subtask_id": subtask_id,
                "agent": agent,
                "status": "completed",
                "summary": output[:500],
            },
        )
        log.info(
            "task_child_finished",
            session_id=ctx.session_id,
            subtask_id=subtask_id,
            status="completed",
        )
        return ToolResult(
            output=output,
            truncated=truncated,
            metadata={
                "subtask_id": subtask_id,
                "agent": agent,
                "status": "completed",
                "steps": result.steps,
            },
        )

    async def _emit_parent(self, ctx: ToolContext, event: dict[str, Any]) -> None:
        """Forward *event* to the parent emitter (never breaks the tool)."""
        if ctx.parent_emit is None:
            return
        try:
            await ctx.parent_emit(event)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("subtask_emit_failed", error=str(exc))

    def _child_emit(self, ctx: ToolContext):  # type: ignore[no-untyped-def]
        """Build the child emitter (deltas wrapped, never breaking)."""

        async def _emit(event: dict[str, Any]) -> None:
            if ctx.parent_emit is None:
                return
            try:
                wrapped = dict(event)
                wrapped.setdefault("subtask", True)
                await ctx.parent_emit(wrapped)
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("subtask_child_emit_failed", error=str(exc))

        return _emit


def _child_registry(registry: Any, agent_name: str = "") -> Any:
    """Return a child registry without disallowed parent tools.

    Depth enforcement is belt-and-braces: the runner also withholds
    ``task`` at the depth limit and ``TaskTool`` rejects direct calls.
    Filtering here keeps nested ``task`` and ``todowrite`` out of the
    child tool schemas entirely (OpenCode parity: subagents get no
    todowrite). ``computeruse`` children receive the dedicated
    computer-use registry instead of the parent tool set.
    """
    from . import computeruse_tool_registry
    from .base import ToolRegistry
    from .computeruse import COMPUTER_USE_TOOL_NAMES

    agent = (agent_name or "").strip().lower()
    if agent == "computeruse":
        child = computeruse_tool_registry()
        for hook in getattr(registry, "before_hooks", []):
            child.add_before_hook(hook)
        for hook in getattr(registry, "after_hooks", []):
            child.add_after_hook(hook)
        return child

    child = ToolRegistry()
    for tool in registry.list():
        key = (tool.name or "").strip().lower()
        if key in ("task", "todowrite"):
            continue
        if key in COMPUTER_USE_TOOL_NAMES:
            continue
        child.register(tool)
    for hook in getattr(registry, "before_hooks", []):
        child.add_before_hook(hook)
    for hook in getattr(registry, "after_hooks", []):
        child.add_after_hook(hook)
    return child


class WebfetchArgs(BaseModel):
    """Arguments for the webfetch tool."""

    url: str = Field(description="https:// URL to fetch.")
    max_size: int = Field(default=WEBFETCH_MAX_BYTES, gt=0)


class WebfetchTool(Tool):
    """Fetch a URL as text with size and timeout guards."""

    name = "webfetch"
    description = (
        "Fetch a URL and return its text content. Enforces a timeout "
        f"({WEBFETCH_TIMEOUT:.0f}s) and a size limit. HTTPS only."
    )
    args_schema: type[BaseModel] = WebfetchArgs
    permission_key = "webfetch"

    def title(self, args: BaseModel) -> str:
        """Return a short title for a webfetch invocation."""
        assert isinstance(args, WebfetchArgs)
        return f"Fetch {args.url[:80]}"

    async def execute(
        self, args: BaseModel | dict[str, object], ctx: ToolContext
    ) -> ToolResult:
        """Fetch *url* via httpx without touching the workspace."""
        validated = self.coerce_args(args)
        assert isinstance(validated, WebfetchArgs)
        args = validated
        url = args.url.strip()
        if not url.startswith("https://"):
            raise ToolError(
                f"Only https:// URLs are allowed: {args.url}",
                tool=self.name,
            )
        import httpx

        try:
            async with httpx.AsyncClient(
                timeout=WEBFETCH_TIMEOUT, follow_redirects=True
            ) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    chunks: list[bytes] = []
                    buffered = 0
                    async for chunk in response.aiter_bytes(65536):
                        buffered += len(chunk)
                        if buffered > args.max_size:
                            raise ToolError(
                                f"Response exceeds {args.max_size} bytes: {url}",
                                tool=self.name,
                            )
                        chunks.append(chunk)
                    raw = b"".join(chunks)
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(f"Failed to fetch {url}: {exc}", tool=self.name) from exc
        text = raw.decode("utf-8", errors="replace")
        return ToolResult(
            output=text,
            metadata={"url": url, "size": len(raw)},
        )
