# MCP Tools

OpenCuria exposes two **Model Context Protocol (MCP)** servers that let AI agents control workspaces and browsers directly from their conversation flow.

| Server | What it does | Transport |
|---|---|---|
| **OpenCuria MCP** | Manage workspaces, run prompts, handle credentials | Streamable HTTP |
| **Playwright MCP** | Browser automation — navigate, click, screenshot | stdio (local process) |

---

## OpenCuria MCP

Connect your AI agent to the OpenCuria backend. First [create an API key](/docs/api-keys) with `MCP_ACCESS` permission and any workspace/tool permissions you need.

**Endpoint:** `https://your-backend.example.com/mcp` · Local: `http://localhost:8000/mcp`  
**Auth:** `Authorization: Bearer kai_your_api_key_here`

<details>
<summary><strong>Claude Code</strong></summary>

Add to `.mcp.json` in your project root, or `~/.config/claude/mcp.json` globally:

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

Or via CLI:

```bash
claude mcp add --transport http opencuria http://localhost:8000/mcp \
  --header "Authorization: Bearer kai_your_api_key_here"
```

</details>

<details>
<summary><strong>GitHub Copilot CLI</strong></summary>

Add to `~/.copilot/mcp.json`:

```json
{
  "mcpServers": {
    "opencuria": {
      "type": "http",
      "url": "https://your-backend.example.com/mcp",
      "headers": {
        "Authorization": "Bearer kai_your_api_key_here"
      }
    }
  }
}
```

Restart the CLI — OpenCuria tools will be available immediately.

</details>

<details>
<summary><strong>OpenAI Codex CLI</strong></summary>

```bash
codex mcp add opencuria \
  --url https://your-backend.example.com/mcp \
  --bearer-token-env-var OPENCURIA_API_KEY

export OPENCURIA_API_KEY=kai_your_api_key_here
```

Or add to `~/.codex/config.toml`:

```toml
[mcp_servers.opencuria]
url = "https://your-backend.example.com/mcp"
headers = { Authorization = "Bearer kai_your_api_key_here" }
```

</details>

<details>
<summary><strong>Cursor / VS Code</strong></summary>

Go to **Settings → MCP → Add new MCP Server**, choose type `http`, and enter:

- **URL:** `https://your-backend.example.com/mcp`
- **Headers:** `Authorization: Bearer kai_your_api_key_here`

Or add to your `mcp.json` / `settings.json`:

```json
{
  "mcpServers": {
    "opencuria": {
      "type": "http",
      "url": "https://your-backend.example.com/mcp",
      "headers": {
        "Authorization": "Bearer kai_your_api_key_here"
      }
    }
  }
}
```

</details>

---

## Playwright MCP

Browser automation via [microsoft/playwright-mcp](https://github.com/microsoft/playwright-mcp). Requires **Node.js 18+**.

> Inside OpenCuria workspaces Node.js is pre-installed — no extra setup needed.

<details>
<summary><strong>Claude Code</strong></summary>

Add to `.mcp.json` in your project root, or `~/.config/claude/mcp.json` globally:

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

Or via CLI:

```bash
claude mcp add playwright npx @playwright/mcp@latest
```

</details>

<details>
<summary><strong>GitHub Copilot CLI</strong></summary>

Add to `~/.copilot/mcp.json`:

```json
{
  "mcpServers": {
    "playwright": {
      "type": "local",
      "command": "npx",
      "args": ["@playwright/mcp@latest", "--headless"],
      "tools": ["*"]
    }
  }
}
```

</details>

<details>
<summary><strong>OpenAI Codex CLI</strong></summary>

```bash
codex mcp add playwright npx "@playwright/mcp@latest"
```

Or add to `~/.codex/config.toml`:

```toml
[mcp_servers.playwright]
command = "npx"
args = ["@playwright/mcp@latest"]
```

</details>

<details>
<summary><strong>Cursor</strong></summary>

[![Install in Cursor](https://cursor.com/deeplink/mcp-install-dark.svg)](https://cursor.com/en/install-mcp?name=Playwright&config=eyJjb21tYW5kIjoibnB4IEBwbGF5d3JpZ2h0L21jcEBsYXRlc3QifQ%3D%3D)

Or go to **Cursor Settings → MCP → Add new MCP Server**, type `command`, command: `npx @playwright/mcp@latest`.

</details>

<details>
<summary><strong>VS Code</strong></summary>

[![Install in VS Code](https://img.shields.io/badge/VS_Code-Install_Server-0098FF?style=flat-square)](https://insiders.vscode.dev/redirect?url=vscode%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522playwright%2522%252C%2522command%2522%253A%2522npx%2522%252C%2522args%2522%253A%255B%2522%2540playwright%252Fmcp%2540latest%2522%255D%257D)

Or add to `settings.json`:

```json
{
  "mcp": {
    "servers": {
      "playwright": {
        "command": "npx",
        "args": ["@playwright/mcp@latest"]
      }
    }
  }
}
```

</details>

<details>
<summary><strong>Running inside a container / OpenCuria workspace</strong></summary>

Chromium requires `--headless` and `--no-sandbox` when running as root inside Docker:

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

If Chromium is not installed, run `npx playwright install chromium` first.

</details>
