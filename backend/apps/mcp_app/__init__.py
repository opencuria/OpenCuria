"""Management MCP server for OpenCuria (``/mcp``).

Lets AI assistants (Claude, ChatGPT, Cursor, …) manage OpenCuria with a
small set of task-oriented tools: workspaces, chats, skills, plugins and
scheduled tasks. Tools are thin adapters over the same services as the
REST API; every call is authorized by the caller's API key
(``mcp:access`` plus the per-tool permission) and scoped to one
organization. Agents inside OpenCuria reach the same server in-process
through the built-in OpenCuria plugin (see :mod:`.inprocess`).
"""
