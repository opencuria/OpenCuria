# Playwright MCP

The [Playwright MCP](https://github.com/microsoft/playwright-mcp) server exposes a browser automation interface over the **Model Context Protocol (MCP)**. It lets AI coding agents (Claude Code, GitHub Copilot, etc.) control a real Chromium browser to visit pages, take screenshots, fill forms, and extract content — all from within their normal conversation flow.

---

## Prerequisites

- **Node.js 18+** installed on the machine where the agent runs
- The agent workspace image already includes Node.js, so no extra setup is needed inside opencuria workspaces

---

## Installation

Install the Playwright MCP server globally (or into a project):

```bash
npm install -g @playwright/mcp@latest
```

Verify the install:

```bash
npx @playwright/mcp --version
```

---

## Connecting with Claude Code

Claude Code reads MCP server configuration from a `.mcp.json` file at the project root or from `~/.config/claude/mcp.json` for global configuration.

### Project-level config (.mcp.json)

Add the following file to the **root of your repository**:

```json
{
  "mcpServers": {
    "playwright": {
      "command": "npx",
      "args": ["@playwright/mcp@latest"],
      "env": {}
    }
  }
}
```

### Global config (~/.config/claude/mcp.json)

To make Playwright MCP available in every project, add it to your global Claude config:

```json
{
  "mcpServers": {
    "playwright": {
      "command": "npx",
      "args": ["@playwright/mcp@latest"]
    }
  }
}
```

### Starting Claude Code with the server

Once the config is in place, simply start Claude Code in the project directory:

```bash
claude
```

Claude will automatically start the Playwright MCP server and list it under **Connected MCP servers**. You can verify with the `/mcp` slash command inside Claude Code.

### Running in a headless environment (e.g. inside a opencuria workspace)

When running inside a Docker container or a remote VM without a display, add the `--headless` flag:

```json
{
  "mcpServers": {
    "playwright": {
      "command": "npx",
      "args": ["@playwright/mcp@latest", "--headless"]
    }
  }
}
```

---

## Connecting with GitHub Copilot CLI

GitHub Copilot CLI supports MCP servers through a configuration file located at `~/.copilot/mcp.json` (or the path set by the `COPILOT_MCP_CONFIG` environment variable).

### ~/.copilot/mcp.json

```json
{
  "mcpServers": {
    "playwright": {
      "command": "npx",
      "args": ["@playwright/mcp@latest", "--headless"]
    }
  }
}
```

After saving the file, restart Copilot CLI. On the next start it will connect to the Playwright MCP server and show it as an available tool.

### Verifying the connection

Inside a Copilot CLI session you can ask:

```
What MCP tools do you have available?
```

The response should list the Playwright browser tools such as `browser_navigate`, `browser_snapshot`, `browser_click`, etc.

---

## Available Tools

Once connected, the agent gains access to the following browser automation tools:

| Tool | Description |
|------|-------------|
| `browser_navigate` | Navigate to a URL |
| `browser_snapshot` | Capture accessibility snapshot of the current page |
| `browser_take_screenshot` | Take a PNG screenshot |
| `browser_click` | Click an element |
| `browser_type` | Type text into an input |
| `browser_fill_form` | Fill multiple form fields at once |
| `browser_select_option` | Select a dropdown option |
| `browser_hover` | Hover over an element |
| `browser_evaluate` | Run JavaScript in the page context |
| `browser_wait_for` | Wait for text to appear / disappear |
| `browser_network_requests` | List all network requests |
| `browser_console_messages` | Retrieve browser console output |
| `browser_resize` | Resize the browser window |
| `browser_tabs` | Manage browser tabs |

---

## Common Usage Examples

### Visit a page and take a screenshot

```
Visit http://localhost:3000 and take a screenshot
```

### Fill out and submit a form

```
Go to http://localhost:3000/login, fill in the email field with "user@example.com"
and the password field with "secret", then click the Login button.
```

### Extract data from a table

```
Navigate to http://localhost:8000/admin/users and extract all user email addresses
from the table.
```

---

## Troubleshooting

### browserType.launch: Executable doesn't exist

Run the Playwright browser install command:

```bash
npx playwright install chromium
```

### Running as root without a sandbox (Docker / opencuria workspaces)

Chromium refuses to run as root by default. Pass the `--no-sandbox` flag via the config:

```json
{
  "mcpServers": {
    "playwright": {
      "command": "npx",
      "args": [
        "@playwright/mcp@latest",
        "--headless",
        "--",
        "--no-sandbox",
        "--disable-setuid-sandbox"
      ]
    }
  }
}
```

### Port conflicts

By default Playwright MCP starts an internal HTTP server on a random port. If you need a fixed port, set the `PLAYWRIGHT_MCP_PORT` environment variable:

```json
{
  "mcpServers": {
    "playwright": {
      "command": "npx",
      "args": ["@playwright/mcp@latest"],
      "env": {
        "PLAYWRIGHT_MCP_PORT": "8931"
      }
    }
  }
}
```
