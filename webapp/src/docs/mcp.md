# MCP

OpenCuria speaks the **Model Context Protocol (MCP)** in two directions:

- **OpenCuria MCP server**: AI assistants such as Claude Code, Claude Desktop, Cursor or ChatGPT manage OpenCuria: workspaces, chats, skills, plugins and scheduled tasks.
- **MCP plugins**: agents inside workspaces get extra tools from the plugins you enable.

## OpenCuria MCP server

**Endpoint:** `https://your-backend.example.com/mcp` (local: `http://localhost:8000/mcp`), Streamable HTTP.

1. Create an API key under **Settings → API keys** with **`mcp:access`** plus the permissions for the tools you want, for example `workspaces:read`, `harness:read`, `harness:run`, `harness:permissions`, `skills:read`, `skills:write`, `plugins:read`, `plugins:write`. The server only offers tools the key may use.
2. Add the server to your client:

```bash
claude mcp add --transport http opencuria https://your-backend.example.com/mcp \
  --header "Authorization: Bearer kai_your_api_key"
```

```json
{
  "mcpServers": {
    "opencuria": {
      "type": "http",
      "url": "https://your-backend.example.com/mcp",
      "headers": { "Authorization": "Bearer kai_your_api_key" }
    }
  }
}
```

The key acts as its owner in the owner's first organization. To use another organization, also send `X-Organization-Id: <organization id>`.

### Tools

| Area | Tools |
|---|---|
| Orientation | `opencuria_context`, `model_list` |
| Workspaces | `workspace_list`, `workspace_get`, `workspace_options`, `workspace_create`, `workspace_update`, `workspace_start`, `workspace_stop`, `workspace_delete` |
| Chats | `chat_list`, `chat_read`, `chat_start`, `chat_send`, `chat_respond`, `chat_stop`, `chat_delete` |
| Skills | `skill_list`, `skill_get`, `skill_create`, `skill_update`, `skill_delete` |
| Plugins | `plugin_list`, `plugin_get`, `plugin_create`, `plugin_update`, `plugin_delete`, `plugin_set_active` |
| Scheduled tasks | `scheduled_task_list`, `scheduled_task_get`, `scheduled_task_create`, `scheduled_task_update`, `scheduled_task_delete`, `scheduled_task_run_now` |

Workspaces, skills, plugins and scheduled tasks can be referenced by ID or exact name.

### Typical flow

1. `chat_start` with a workspace and a prompt; the agent works asynchronously.
2. `chat_read` with `wait_seconds` returns the reply once the agent is done, or what it is waiting for.
3. `chat_respond` answers a question or permission request; `chat_send` adds a follow-up or redirects a running agent.

## MCP plugins

OpenCuria agents use MCP tools from the plugins selected for a workspace.

1. Open **Settings → Plugins** and activate the plugins you need.
2. Connect any required accounts under **Settings → Credentials**.
3. Select the plugins and credentials in the workspace settings.

The backend connects the selected MCP servers when an agent needs them.
Plugin definitions support stdio processes in the workspace and HTTP services.
Browser automation is available through the Playwright plugin.

The built-in **OpenCuria** plugin gives agents this MCP server without any setup: no API
key, no credentials. Turn it on per organization and per workspace; the agent then acts
as the workspace owner with every tool, within the owner's organization role. It cannot
be edited or deleted.
