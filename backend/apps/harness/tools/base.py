"""Tool ABC, context and registry.

Every harness tool reaches the workspace exclusively through a
:class:`~apps.harness.access.base.WorkspaceAccessor` (provided via
:class:`ToolContext`). Tools never touch the local filesystem, never spawn
local shells, and never use the ORM directly.
"""

from __future__ import annotations

import abc
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog
from pydantic import BaseModel

from ..access.base import WorkspaceAccessor
from ..constants import DEFAULT_MAX_DEPTH
from ..providers.base import ToolSchema
from .truncate import truncate_tool_output

log = structlog.get_logger(__name__)


class ToolError(Exception):
    """Raised when a tool cannot fulfill a validly dispatched call."""

    def __init__(
        self,
        message: str,
        *,
        tool: str = "",
        content: list[dict[str, Any]] | None = None,
    ) -> None:
        self.tool = tool
        self.content = content
        super().__init__(message)


@dataclass
class ToolContext:
    """Runtime context handed to every tool execution.

    ``depth``/``max_depth`` implement the M5 subagent recursion limit
    (OpenCode-style): the top-level turn runs at ``depth=0``; each
    ``task`` child runs at ``depth+1``. When ``depth >= max_depth`` the
    runner withholds the ``task`` tool (and denies direct calls), so
    nesting cannot recurse without a bound. ``todowrite`` is additionally
    withheld from any child (``depth > 0``).

    ``model`` carries the parent's effective model for inheritance.
    ``run_subagent`` delegates child execution to the persistent harness
    service; ``registry`` supplies the active todo service for events.
    """

    session_id: str
    workspace_id: str
    accessor: WorkspaceAccessor
    agent_name: str = ""
    directory: str = "/workspace"
    depth: int = 0
    max_depth: int = DEFAULT_MAX_DEPTH
    model: str = ""
    registry: ToolRegistry | None = None
    run_subagent: Any | None = None
    call_id: str = ""
    on_question: Callable[..., Awaitable[list[Any]]] | None = None
    question_timeout: float | None = None


@dataclass
class ToolResult:
    """Structured result returned by every tool.

    ``attachments`` carries generic file attachments (OpenCode parity):
    each entry is ``{"type": "file", "mime": str,
    "url": "data:<mime>;base64,<payload>", "filename": str}``.
    ``filename`` is the workspace basename (``""`` fallback) so
    providers and compaction can name the file without parsing the
    data URL. Tools that produce attachments should also mirror them
    into ``metadata["attachments"]`` for ``HarnessPart`` persistence.
    ``ToolRegistry.execute`` only clips ``output`` and never truncates
    ``attachments``.
    """

    output: str
    truncated: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    # Checked provider content preserves MCP block boundaries and ordering.
    content: list[dict[str, Any]] | None = None


class Tool(abc.ABC):
    """Abstract base class for all harness tools."""

    omit_tools_from: tuple[str, ...] = ()

    name: str = ""
    description: str = ""
    args_schema: type[BaseModel] = BaseModel
    permission_key: str = ""
    truncate_direction: str = "head"

    def title(self, args: BaseModel) -> str:
        """Return a short human-readable title for this invocation."""
        return self.name or type(self).__name__

    def parameters_schema(self) -> dict[str, Any]:
        """Return the JSON schema advertised to the provider.

        Default: the Pydantic v2 ``args_schema``. MCP tools override
        this with the original MCP ``inputSchema`` (validated at
        discovery, never a lossy translation).
        """
        return self.args_schema.model_json_schema()

    def coerce_args(self, args: BaseModel | dict[str, Any]) -> BaseModel:
        """Validate *args* against the tool schema.

        Accepts an already-validated model (pass-through) or a raw dict
        (validated via pydantic). Direct ``tool.execute({...})`` calls in
        tests and future callers therefore behave like registry dispatch.
        """
        if isinstance(args, self.args_schema):
            return args
        if isinstance(args, dict):
            return self.args_schema.model_validate(args)
        raise ToolError(
            f"Invalid arguments for tool '{self.name}': {type(args).__name__}",
            tool=self.name,
        )

    @abc.abstractmethod
    async def execute(
        self, args: BaseModel | dict[str, Any], ctx: ToolContext
    ) -> ToolResult:
        """Execute the tool with validated *args* in context *ctx*."""


class ToolRegistry:
    """Register, validate and execute harness tools."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        """Register *tool* under its ``name``."""
        if not tool.name or not tool.name.strip():
            raise ValueError("Tool must define a non-empty name")
        key = tool.name.strip().lower()
        if key in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[key] = tool
        log.debug("tool_registered", tool=tool.name)
        return tool

    def get(self, name: str) -> Tool:
        """Return the tool registered as *name*."""
        try:
            return self._tools[name.strip().lower()]
        except KeyError as exc:
            raise KeyError(f"Unknown tool: {name}") from exc

    def list(self) -> list[Tool]:
        """Return all registered tools in registration order."""
        return list(self._tools.values())

    def names(self) -> list[str]:
        """Return all registered tool names."""
        return [tool.name for tool in self._tools.values()]

    def __contains__(self, name: object) -> bool:
        """Return True when a tool called *name* is registered."""
        return isinstance(name, str) and name.strip().lower() in self._tools

    def schemas(self) -> list[ToolSchema]:
        """Return LLM function-calling schemas for all tools.

        The JSON schema comes from each tool's ``parameters_schema()``
        (MCP tools serve the original MCP ``inputSchema``), so provider
        payloads stay in sync with validation.
        """
        return [
            ToolSchema(
                name=tool.name,
                description=tool.description,
                parameters=tool.parameters_schema(),
            )
            for tool in self._tools.values()
        ]

    async def execute(
        self,
        name: str,
        args: BaseModel | dict[str, Any],
        ctx: ToolContext,
    ) -> ToolResult:
        """Validate *args*, execute the named tool and bound its output."""
        tool = self.get(name)
        validated = tool.coerce_args(args)
        log.info(
            "tool_execute",
            tool=tool.name,
            session_id=ctx.session_id,
            agent=ctx.agent_name,
        )
        result = await tool.execute(validated, ctx)
        direction = getattr(tool, "truncate_direction", "head")
        if direction not in ("head", "tail"):
            direction = "head"
        clipped = truncate_tool_output(
            result.output,
            direction=direction,  # type: ignore[arg-type]
        )
        if clipped.truncated:
            from .spill import preserve_full_output

            full_output = result.output
            result.output = clipped.content
            await preserve_full_output(ctx, result, full_output)
        return result
