# MCP OAuth connections

OpenCuria can authorize OAuth-capable HTTP MCP servers using the server's protected-resource and OAuth metadata. Access and refresh tokens stay encrypted in the backend; neither the credential API nor the webapp returns or displays token values. The feature is generic to MCP plugins; Notion is seeded as the first official integration.

## Deployment setup

The OAuth callback must be a fixed, public address. Configure both values on the backend:

```dotenv
MCP_OAUTH_CALLBACK_URL=https://app.example.com/api/v1/mcp-oauth/callback/
MCP_OAUTH_FRONTEND_RETURN_URL=https://app.example.com/?settings=plugins
```

Register the exact callback URL above with the MCP provider when it supports static redirect registration. The callback URL path is fixed at `/api/v1/mcp-oauth/callback/`; the callback must be HTTPS in production. The frontend return URL must be the fixed trusted Plugins deep link `/?settings=plugins`; any other path or query is rejected. Configure the frontend and backend callback to use the **same hostname and scheme**: the flow binds the callback to a host-only `SameSite=Lax` cookie, so distinct frontend/API hostnames (even sibling subdomains) are not supported by this redirect-based flow. A configuration mismatch fails when Connect starts instead of failing silently at callback time. You may serve the frontend and API on separate ports at the same hostname (for example, local Compose uses `127.0.0.1:8080` and `127.0.0.1:8000`); cross-origin browser API requests must allow credentialed CORS (`CORS_ALLOW_CREDENTIALS=true`, explicit allowed frontend origin, never wildcard) so the cookie is set. Use a valid `CREDENTIAL_ENCRYPTION_KEY` and keep it stable/backed up: losing it makes encrypted OAuth credentials and registered client secrets unrecoverable. Local Compose example URLs are in `.env.example`; production URL examples are in `.env.server.example`.

For local development with Vite, open the site as `http://127.0.0.1:5173` (not `localhost` or a different hostname) and keep the backend callback on `http://127.0.0.1:8000`; `localhost`, `127.0.0.1`, and `::1` are distinct cookie hosts. The Vite dev-server proxy points at that backend. Set `MCP_OAUTH_FRONTEND_RETURN_URL=http://127.0.0.1:5173/?settings=plugins` in the environment used by the backend if `.env` already overrides the development default. Docker Compose uses `http://127.0.0.1:8080` for the frontend and `http://127.0.0.1:8000` for the backend/callback.

The OAuth server endpoints and dynamic client registration must use HTTPS and public DNS addresses. OpenCuria requires PKCE S256 and dynamic client registration. A provider that does not support these capabilities cannot connect. Notion authorization requires a real Notion account and browser interaction, so an end-to-end provider connection is not available in automated tests.

## Connect and use

1. In **Settings → Plugins**, locate a published OAuth MCP plugin. Members can connect their personal account; only organization admins can connect a shared organization account. The OAuth flow navigates the current tab to the provider and returns to the open Plugins sheet with a success/failure toast. Provider `code` and `state` are consumed by the backend and never appear in the frontend redirect URL.
2. Open a workspace's **Edit** dialog and attach the connected OAuth credential under Credentials. OAuth connection alone does not grant the credential to a workspace.
3. Enable the plugin for the organization (admin) and select it for the workspace. OpenCuria checks both the workspace attachment and current OAuth connection state before allowing activation.
4. For a shared connection, an admin completes the organization OAuth flow once; members can attach that organization credential to their workspaces.

OAuth credential entries are informational in **Settings → Credentials**. They cannot be manually created, edited, or deleted there; use the plugin's Connect, Reconnect, and Disconnect actions. Disconnecting revokes the local usable connection but preserves workspace attachments; detach it in each workspace if it should no longer be associated. Reconnect replaces the authorization through the provider flow. If a token expires or is revoked, OpenCuria will indicate that reconnection is needed.

## Security and operations

- Connect/reconnect only through the authenticated OpenCuria session and expected organization. The flow uses a short-lived, HttpOnly browser-binding cookie; complete authorization in the same browser tab.
- OAuth authorization is interactive and not available to API keys. Organization-scoped connections and disconnections require an organization admin.
- Treat the backend encryption key and database backups as sensitive. Restrict backend/network access and use HTTPS for both public app and API origins.
- Do not add bearer tokens to MCP plugin headers or logs. OAuth-managed `Authorization` headers are deliberately controlled server-side.
- A failed provider return displays a generic status; inspect backend operational logs for sanitized diagnostic context, never log provider codes, access/refresh tokens, or callback query strings.

## Provider references

- [Notion MCP client guide](https://developers.notion.com/guides/mcp/build-mcp-client)
- [Model Context Protocol authorization specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)
- [OAuth 2.0 Authorization Framework (RFC 6749)](https://www.rfc-editor.org/rfc/rfc6749)
- [Proof Key for Code Exchange (RFC 7636)](https://www.rfc-editor.org/rfc/rfc7636)
- [OAuth 2.0 Authorization Server Metadata (RFC 8414)](https://www.rfc-editor.org/rfc/rfc8414)
- [Protected Resource Metadata (RFC 9728)](https://www.rfc-editor.org/rfc/rfc9728)
