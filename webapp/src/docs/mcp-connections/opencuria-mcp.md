# opencuria MCP Server

The opencuria backend exposes a **Model Context Protocol (MCP)** server that lets AI agents (Claude Code, GitHub Copilot, etc.) control opencuria directly — create and manage workspaces, run prompts, handle credentials, and more — all from within their normal conversation flow.

The MCP server is mounted at `/mcp` on the opencuria backend. The primary transport is **Streamable HTTP**. A legacy **SSE** endpoint remains available at `/mcp/sse` for older clients that explicitly require it.

---

## Prerequisites

- A running opencuria backend (self-hosted or cloud)
- An API key with the `MCP_ACCESS` permission plus the permissions for the tools you need (see [API key setup](#api-key-setup) below)

---

## API Key Setup

### 1. Create an API key

Open the opencuria webapp, navigate to **API Keys**, and click **New API key**.

Give it a descriptive name (e.g. `claude-code-local`) and select the permissions your agent needs:

| Permission | Enables tools |
|---|---|
| `MCP_ACCESS` | Required for any MCP connection |
| `workspaces:read` | `list_workspaces`, `get_workspace` |
| `workspaces:create` | `create_workspace` |
| `workspaces:stop` | `stop_workspace` |
| `workspaces:resume` | `resume_workspace` |
| `workspaces:delete` | `remove_workspace` |
| `prompts:run` | `run_prompt` |
| `runners:read` | `list_runners` |
| `agents:read` | `list_agents` |
| `conversations:read` | `list_conversations` |
| `images:read` | `list_image_artifacts` |
| `images:create` | `create_image_artifact` |
| `credentials:read` | `list_credentials` |
| `org_agent_definitions:read/write` | Admin agent definition tools |
| `org_credential_services:read/write` | Admin credential service tools |

> The raw token is shown **once** — save it immediately.

### 2. Copy the backend URL

For modern MCP clients, use the primary MCP endpoint:

```
https://your-opencuria-backend.example.com/mcp
```

For local development:

```
http://localhost:8000/mcp
```

Legacy SSE-only clients can still use:

```
https://your-opencuria-backend.example.com/mcp/sse
```

Local legacy SSE endpoint:

```
http://localhost:8000/mcp/sse
```

---

## Connecting with Claude Code

Claude Code reads MCP server configuration from `.mcp.json` at the project root or from `~/.config/claude/mcp.json` for global configuration. For remote opencuria deployments, use the HTTP transport and point it at `/mcp`.

### Project-level config (.mcp.json)

Place the following file at the **root of your repository**:

```json
{
  "mcpServers": {
    "opencuria": {
      "type": "http",
      "url": "http://localhost:8000/mcp",
      "headers": {
        "Authorization": "Bearer kai_your_api_key_here"
      }
    }
  }
}
```

### Global config (~/.config/claude/mcp.json)

To make opencuria available in every project:

```json
{
  "mcpServers": {
    "opencuria": {
      "type": "http",
      "url": "https://your-opencuria-backend.example.com/mcp",
      "headers": {
        "Authorization": "Bearer kai_your_api_key_here"
      }
    }
  }
}
```

### Starting Claude Code

Once the config is in place, start Claude Code:

```bash
claude
```

Claude will automatically connect to the opencuria MCP server and list it under **Connected MCP servers**. Verify with `/mcp` inside Claude Code.

You can also add it directly from the CLI:

```bash
claude mcp add --transport http opencuria https://your-opencuria-backend.example.com/mcp \
  --header "Authorization: Bearer kai_your_api_key_here"
```

### Usage example

```
List all my opencuria workspaces and then run the prompt
"Write unit tests for the auth module" in the workspace named "backend-dev".
```

---

## Connecting with GitHub Copilot CLI

GitHub Copilot supports MCP servers via `~/.copilot/mcp.json` (or the path in `COPILOT_MCP_CONFIG`). Use the HTTP MCP endpoint at `/mcp`.

### ~/.copilot/mcp.json

```json
{
  "mcpServers": {
    "opencuria": {
      "type": "http",
      "url": "https://your-opencuria-backend.example.com/mcp",
      "headers": {
        "Authorization": "Bearer kai_your_api_key_here"
      }
    }
  }
}
```

After saving, restart Copilot CLI. On the next start it will connect and expose the opencuria tools.

## Connecting with OpenAI Codex CLI

Codex expects a Streamable HTTP MCP endpoint. Point it at `/mcp`, **not** `/mcp/sse`:

```bash
codex mcp add opencuria \
  --url https://your-opencuria-backend.example.com/mcp \
  --bearer-token-env-var OPENCURIA_API_KEY
```

Then export your API key before starting Codex:

```bash
export OPENCURIA_API_KEY=kai_your_api_key_here
```

### Verifying the connection

Inside a Copilot session:

```
What opencuria MCP tools do you have available?
```

The response should list all tools your API key has permission to access.

---

## Available Tools

The tools your agent can access depend on the permissions assigned to the API key.

### Workspace Management

| Tool | Required permission | Description |
|---|---|---|
| `list_workspaces` | `workspaces:read` | List workspaces in the active organization |
| `get_workspace` | `workspaces:read` | Get details of a single workspace including sessions |
| `create_workspace` | `workspaces:create` | Create a new workspace on a runner |
| `stop_workspace` | `workspaces:stop` | Stop a running workspace |
| `resume_workspace` | `workspaces:resume` | Resume a stopped workspace |
| `remove_workspace` | `workspaces:delete` | Remove a workspace and its container permanently |

### Agent Execution

| Tool | Required permission | Description |
|---|---|---|
| `run_prompt` | `prompts:run` | Send a prompt to an AI agent in a workspace |
| `list_agents` | `agents:read` | List available agent definitions |
| `list_runners` | `runners:read` | List runners in the active organization |

### Conversations & Images

| Tool | Required permission | Description |
|---|---|---|
| `list_conversations` | `conversations:read` | List conversations (chats) for the current user |
| `list_image_artifacts` | `images:read` | List image artifacts owned by the current user |
| `create_image_artifact` | `images:create` | Capture an image artifact from a workspace |

### Credentials

| Tool | Required permission | Description |
|---|---|---|
| `list_credentials` | `credentials:read` | List credentials (metadata only, no secrets) |

### Organization Admin (admin-only)

| Tool | Required permission | Description |
|---|---|---|
| `list_org_agent_definitions` | `org_agent_definitions:read` | List agent definitions with activation status |
| `create_org_agent_definition` | `org_agent_definitions:write` | Create an organization-owned agent definition |
| `update_org_agent_definition` | `org_agent_definitions:write` | Update an agent definition |
| `delete_org_agent_definition` | `org_agent_definitions:write` | Delete an agent definition |
| `toggle_org_agent_definition_activation` | `org_agent_definitions:write` | Activate / deactivate an agent for the org |
| `list_org_credential_services` | `org_credential_services:read` | List credential services with activation status |
| `toggle_org_credential_service_activation` | `org_credential_services:write` | Activate / deactivate a credential service |

---

## Common Usage Examples

### List workspaces

```
Show me all opencuria workspaces that are currently running.
```

### Create a workspace and run a task

```
Create a opencuria workspace called "feature-auth" using the claude agent,
clone the repository https://github.com/my-org/my-repo into it,
then run the prompt "Implement JWT refresh token rotation".
```

### Trigger an agent on a specific workspace

```
In my opencuria workspace "backend-dev", run the prompt:
"Review the PR diff and add inline comments for potential bugs."
```

### Take a snapshot before a risky change

```
Create a snapshot named "before-refactor" of workspace abc-123,
then run the prompt "Refactor the database models to use UUID primary keys".
```

---

## Troubleshooting

### 401 Unauthorized

- Check that your API key starts with `kai_` and is correct.
- Ensure the key has the `MCP_ACCESS` permission.
- Check that the `Authorization` header is formatted as `Bearer kai_...`.

### Tool not visible / permission denied

Each tool requires a specific API key permission. If a tool is missing, add the required permission to your API key in **API Keys** → edit key → permissions.

### Connection refused (local development)

Make sure the backend is running:

```bash
cd /path/to/opencuria/backend
source .venv/bin/activate
python manage.py runserver 0.0.0.0:8000
```

The primary MCP endpoint is `http://localhost:8000/mcp`.

Legacy SSE clients should use `http://localhost:8000/mcp/sse`.

### Streaming connection drops

If you are using the legacy SSE transport, the connection is long-lived. If your network has aggressive idle timeouts, configure the MCP client to reconnect automatically. Most MCP clients handle this transparently.
