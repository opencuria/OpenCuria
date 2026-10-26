# MCP

OpenCuria agents use tools through the **Model Context Protocol (MCP)**:

- **OpenCuria plugin**: manage workspaces, chats, skills, plugins and scheduled tasks.
- **Integration plugins**: get extra tools from the MCP servers you enable.

## OpenCuria MCP server

The built-in **OpenCuria** plugin connects agents to this server in-process.
Enable it per organization and workspace. Agents act as the workspace owner
within the workspace's organization and the owner's role. No API key or
credentials are required. The built-in plugin cannot be edited or deleted.

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
