"""Registration point for the MCP server built into OpenCuria (``builtin``).

The harness never imports the apps built on it. The app serving the
built-in server (``apps.mcp_app``) registers its opener from
``AppConfig.ready()``; connections with the ``builtin`` transport open it
for their run's workspace and organization.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

#: ``(workspace_id, organization_id) -> async context of (read, write) streams``.
BuiltinServerOpener = Callable[[Any, Any], AbstractAsyncContextManager[Any]]

_opener: BuiltinServerOpener | None = None


def register_builtin_server(opener: BuiltinServerOpener) -> None:
    """Install the process-wide opener of the built-in MCP server."""
    global _opener
    _opener = opener


def open_builtin_server(
    workspace_id: Any, organization_id: Any
) -> AbstractAsyncContextManager[Any]:
    """Return the built-in server's client streams for one run.

    Raises:
        RuntimeError: If no app has registered the built-in server.
    """
    if _opener is None:
        raise RuntimeError("No built-in MCP server registered")
    return _opener(workspace_id, organization_id)
