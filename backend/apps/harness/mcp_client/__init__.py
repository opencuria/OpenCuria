"""MCP client runtime for harness runs (workspace-local transports).

All MCP traffic leaves the backend through a
:class:`~apps.harness.access.base.WorkspaceAccessor`:

- ``stdio`` servers run as workspace-local processes opened via
  :meth:`WorkspaceAccessor.open_process`; their stdout is a
  newline-delimited JSON-RPC byte stream (stderr stays separate and is
  never mixed into MCP JSON).
- ``streamable_http``/``sse`` servers are reached through
  :meth:`WorkspaceAccessor.open_tcp` via the custom httpcore network
  backend in :mod:`apps.harness.mcp_client.workspace_http`
  (no backend DNS/connect; TLS terminates in the workspace relay).

The MCP wire protocol itself always comes from the installed
``mcp>=1,<2`` SDK (:class:`~mcp.client.session.ClientSession`,
``streamable_http_client``/``sse_client``); this package only adapts
workspace byte streams into the SDK's memory-object-stream shape.
"""

from __future__ import annotations
