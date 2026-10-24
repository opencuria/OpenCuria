"""Result formatting shared by all MCP tool handlers.

Every handler returns MCP ``TextContent``: JSON payloads via
:func:`text_result` and human-readable failures via :func:`error_result`.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from mcp.types import TextContent

#: Every tool handler receives ``(api_key, org_id, args)`` and returns
#: MCP text content, either directly or as an awaitable.
ToolResult = list[TextContent]
ToolHandler = Callable[[Any, uuid.UUID, dict], ToolResult | Awaitable[ToolResult]]


def serialise(obj) -> dict:
    """Convert a Django ORM-returned dict/object to a JSON-safe dict."""
    if isinstance(obj, dict):
        return {k: serialise(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [serialise(item) for item in obj]
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def text_result(data) -> list[TextContent]:
    """Wrap arbitrary data as a JSON text MCP response."""
    return [
        TextContent(
            type="text", text=json.dumps(serialise(data), indent=2, default=str)
        )
    ]


def error_result(msg: str) -> list[TextContent]:
    """Return an error as a text MCP response."""
    return [TextContent(type="text", text=f"Error: {msg}")]
