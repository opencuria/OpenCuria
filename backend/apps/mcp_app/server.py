"""Per-request MCP server bound to one authenticated caller.

``tools/list`` exposes only the tools whose permission the API key holds.
``tools/call`` validates arguments against the tool's JSON Schema, runs the
handler and turns every expected failure into an ``isError`` result whose
text tells the model how to recover.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import jsonschema
import structlog
from mcp.server.lowlevel import Server
from mcp.types import CallToolResult, TextContent, Tool

from common.exceptions import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    ServiceError,
)

from .context import ToolContext
from .errors import ToolError
from .registry import ToolSpec
from .results import dump, json_safe
from .tools import TOOLS

log = structlog.get_logger(__name__)

SERVER_NAME = "opencuria"

INSTRUCTIONS = """\
OpenCuria runs AI coding agents in isolated workspaces (VMs). A chat is a \
conversation with an agent inside one workspace; the agent works \
asynchronously.
- Start with opencuria_context to see the organization, the user's role and \
which engines are connected.
- Refer to workspaces, skills, plugins and scheduled tasks by ID or exact name.
- To delegate work: chat_start (or chat_send), then chat_read with \
wait_seconds until the agent is idle; answer its questions with chat_respond.
- Skills are reusable Markdown instructions; select them per chat or \
scheduled task. Plugins bundle skills and MCP servers and are enabled per \
workspace.
"""


def _error(message: str) -> CallToolResult:
    """Return an MCP tool execution error the model can act on."""
    return CallToolResult(
        content=[TextContent(type="text", text=f"Error: {message}")],
        isError=True,
    )


def _success(result: dict[str, Any]) -> CallToolResult:
    """Return the handler result as JSON text plus structured content."""
    payload = json_safe(result)
    return CallToolResult(
        content=[TextContent(type="text", text=dump(payload))],
        structuredContent=payload,
        isError=False,
    )


def _validation_message(spec: ToolSpec, error: jsonschema.ValidationError) -> str:
    """Describe a schema violation with its argument path."""
    path = ".".join(str(part) for part in error.absolute_path)
    where = f" (argument '{path}')" if path else ""
    return (
        f"Invalid arguments for {spec.name}{where}: {error.message}. "
        "Check the tool's input schema and retry."
    )


def _gap_hint(gaps: list[dict[str, Any]] | None) -> str:
    """Name the credentials a plugin selection is missing, if any."""
    if not gaps:
        return ""
    missing = ", ".join(
        f"{gap.get('service_name') or gap.get('key')} (for plugin "
        f"{gap.get('plugin_name') or gap.get('plugin_id')})"
        for gap in gaps
    )
    return (
        f" Missing credentials: {missing}. Attach a matching credential with "
        "workspace_update (credentials), then retry."
    )


async def call_tool(
    ctx: ToolContext,
    name: str,
    arguments: dict[str, Any] | None,
    *,
    tools: Sequence[ToolSpec] = TOOLS,
) -> CallToolResult:
    """Authorize, validate and run one tool call for *ctx*."""
    spec = next((tool for tool in tools if tool.name == name), None)
    if spec is None:
        return _error(f"Unknown tool '{name}'.")
    if not ctx.can(spec.permission):
        return _error(
            f"This API key lacks the permission {spec.permission.value} required "
            f"by {name}. Ask the user to add it under Settings → API keys."
        )
    args = dict(arguments or {})
    try:
        jsonschema.validate(args, spec.input_schema)
    except jsonschema.ValidationError as exc:
        return _error(_validation_message(spec, exc))

    try:
        result = await spec.handler(ctx, args)
    except ToolError as exc:
        return _error(exc.message)
    except NotFoundError as exc:
        return _error(f"{exc.message}. It may have been deleted or is not yours.")
    except AuthenticationError as exc:
        return _error(f"Not allowed: {exc.message}")
    except (ConflictError, ServiceError) as exc:
        return _error(exc.message + _gap_hint(getattr(exc, "gaps", None)))
    except (ValueError, KeyError) as exc:
        return _error(f"Invalid input: {exc}")
    except Exception:
        log.exception(
            "mcp_tool_failed",
            tool=name,
            organization_id=str(ctx.organization_id),
            user_id=ctx.user.id,
        )
        return _error(
            f"{name} failed unexpectedly. The error was logged on the server; "
            "retry once, then report the problem to the user."
        )
    return _success(result)


def visible_tools(ctx: ToolContext, tools: Sequence[ToolSpec] = TOOLS) -> list[Tool]:
    """Return the ``tools/list`` entries the caller's API key may use."""
    return [spec.to_mcp() for spec in tools if ctx.can(spec.permission)]


def create_mcp_server(ctx: ToolContext) -> Server:
    """Create an MCP server instance bound to *ctx*."""
    server: Server = Server(SERVER_NAME, instructions=INSTRUCTIONS)

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return visible_tools(ctx)

    @server.call_tool(validate_input=False)
    async def _call_tool(name: str, arguments: dict[str, Any] | None) -> CallToolResult:
        return await call_tool(ctx, name, arguments)

    return server
