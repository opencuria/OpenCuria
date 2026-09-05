"""Subagent (task) tool stub and optional webfetch tool."""

from __future__ import annotations

import structlog
from pydantic import BaseModel, Field

from .base import Tool, ToolContext, ToolError, ToolResult

log = structlog.get_logger(__name__)

# Fetch guard: never buffer more than this per URL.
WEBFETCH_MAX_BYTES = 256 * 1024
WEBFETCH_TIMEOUT = 15.0


class TaskArgs(BaseModel):
    """Arguments for the task (subagent) tool."""

    description: str = Field(description="Short task summary.")
    prompt: str = Field(description="Full instructions for the subagent.")
    subagent_type: str = Field(
        default="general", description="Subagent kind (M5 defines these)."
    )


class TaskTool(Tool):
    """Launch a subagent as a child session.

    v1 is an explicit stub: M5 owns child-session creation, depth limits,
    and result propagation. This stub fails loudly instead of running a
    half-implemented loop, so callers (and tests) see a clear signal.
    """

    name = "task"
    description = (
        "Launch a subagent for a subtask. Not available yet "
        "(implemented in M5: child sessions with depth limits)."
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
        """Reject with a clear M5 pointer instead of a partial loop."""
        validated = self.coerce_args(args)
        assert isinstance(validated, TaskArgs)
        args = validated
        log.info(
            "task_tool_stub_called",
            session_id=ctx.session_id,
            agent=ctx.agent_name,
        )
        raise ToolError(
            "Subagents are not available yet (milestone M5 will add "
            "child sessions, depth limits, and result propagation).",
            tool=self.name,
        )


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
