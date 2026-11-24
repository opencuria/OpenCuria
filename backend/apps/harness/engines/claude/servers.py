"""Names of the in-process MCP servers through which Claude Code reaches OpenCuria.

``opencuria`` carries the tools known when the run starts. Tools of plugins
enabled or edited mid-run go to ``opencuria_plugins``: adding or re-adding a
server never disturbs a call that is in flight on the other one.
"""

from __future__ import annotations

SERVER_NAME = "opencuria"
PLUGIN_SERVER_NAME = "opencuria_plugins"
_PREFIXES = (f"mcp__{SERVER_NAME}__", f"mcp__{PLUGIN_SERVER_NAME}__")


def managed_wire_name(raw_name: str) -> str | None:
    """Return the wire tool name of an OpenCuria-served call, else ``None``."""
    for prefix in _PREFIXES:
        if raw_name.startswith(prefix):
            return raw_name[len(prefix) :]
    return None
