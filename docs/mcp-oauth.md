# MCP OAuth connections

OpenCuria can authorize OAuth-capable HTTP MCP services using protected-resource and OAuth metadata. Credential services own fixed HTTPS MCP endpoints; plugin definitions depend on these reusable services. Access and refresh tokens stay encrypted in the backend and never appear in API responses.

## Deployment setup

The OAuth callback must be a fixed, public address. Configure both values on the backend:

```dotenv
MCP_OAUTH_CALLBACK_URL=https://app.example.com/api/v1/mcp-oauth/callback/
MCP_OAUTH_FRONTEND_RETURN_URL=https://app.example.com/?settings=credentials
```

Register the exact callback URL above with the MCP provider when it supports static redirect registration. The callback URL path is fixed at `/api/v1/mcp-oauth/callback/`; the callback must be HTTPS in production. The frontend return URL must be the fixed trusted Credentials deep link `/?settings=credentials`; any other path or query is rejected. Configure the frontend and backend callback to use the **same hostname and scheme**: the flow binds the callback to a host-only `SameSite=Lax` cookie, so distinct frontend/API hostnames (even sibling subdomains) are not supported by this redirect-based flow. A configuration mismatch fails when Connect starts instead of failing silently at callback time. You may serve the frontend and API on separate ports at the same hostname (for example, local Compose uses `127.0.0.1:8080` and `127.0.0.1:8000`); cross-origin browser API requests must allow credentialed CORS (`CORS_ALLOW_CREDENTIALS=true`, explicit allowed frontend origin, never wildcard) so the cookie is set. Use a valid `CREDENTIAL_ENCRYPTION_KEY` and keep it stable/backed up: losing it makes encrypted OAuth credentials and registered client secrets unrecoverable. Local Compose example URLs are in `.env.example`; production URL examples are in `.env.server.example`.

For local development with Vite, open the site as `http://127.0.0.1:5173` (not `localhost` or a different hostname) and keep the backend callback on `http://127.0.0.1:8000`; `localhost`, `127.0.0.1`, and `::1` are distinct cookie hosts. The Vite dev-server proxy points at that backend. Set `MCP_OAUTH_FRONTEND_RETURN_URL=http://127.0.0.1:5173/?settings=credentials` in the environment used by the backend if `.env` already overrides the development default. Docker Compose uses `http://127.0.0.1:8080` for the frontend and `http://127.0.0.1:8000` for the backend/callback.

## Upgrading an existing installation

Before upgrading, back up the database and `CREDENTIAL_ENCRYPTION_KEY`; keep the
same encryption key configured after the upgrade. Update the backend's
`MCP_OAUTH_FRONTEND_RETURN_URL` to the Credentials tab, for example
`https://app.example.com/?settings=credentials` (use your frontend origin).
The previous frontend return URL is not supported; there is no compatibility
redirect. Then apply the current forward migrations before starting the backend:

```bash
cd backend
.venv/bin/python manage.py migrate --noinput
```

The forward migrations preserve credential IDs and workspace associations for
safely matched connections, and preserve usable encrypted grants. Legacy
connections whose plugin/service/endpoint mapping is ambiguous are marked as
requiring reconnection; pending OAuth authorization transactions are invalidated
and must be started again. The former plugin/server-specific OAuth routes and
plugin-only workspace write route/tool have been removed without compatibility
shims. Use the credential-service OAuth flows and workspace create/update
selection APIs instead. Older migration files remain in the repository as
migration history; do not manually rerun or edit them.

The OAuth server endpoints and dynamic client registration must use HTTPS and public DNS addresses. OpenCuria requires PKCE S256 and dynamic client registration. A provider that does not support these capabilities cannot connect. The focused browser end-to-end tests exercise the application OAuth handshake and callback against a deterministic test provider; they do not authorize against Notion or verify a live Notion account. A live external-provider test has not been performed.

## Connect and use

1. In **Settings → Credentials**, choose a visible OAuth credential service and connect a named personal account. To share the account, an organization admin can instead create an organization-scoped account; members can use that shared connection. Each connection is a separately named credential, so you can keep multiple personal and organization accounts. New credentials require an organization admin to activate their service in **Settings → Credential Services**; plugin activation is not required. OAuth credential services can be referenced by multiple plugins only when their fixed endpoint matches exactly. Provider `code` and `state` are consumed by the backend and never appear in the frontend redirect URL.
2. Enable the plugin for the organization (admin) and select it for the workspace. Attach one credential from each required service. Both workspace creation and editing support plugin and credential selection together. The UI blocks saving until required credentials are selected, and the API rejects incomplete selections; OAuth connection alone does not grant a credential to a workspace. Plugin activation is independent of credential-service activation.
3. From **Settings → Credentials**, use **Reconnect** to authorize an existing account again. Reconnection updates that credential in place and preserves its ID and workspace attachments. Service activation gates new credentials only: an existing credential can be reconnected while its service is inactive. If a token expires or is revoked, OpenCuria indicates that reconnection is needed.

OAuth credential entries can be renamed or deleted using the standard credential actions; token values remain provider-managed and are never exposed or edited. Personal credentials can be managed by their owner; organization credentials require an organization admin. Deletion follows the usual workspace guard: it is blocked if the credential is required by an active plugin in an attached workspace. Disconnecting clears the provider grant but preserves workspace attachments; detach it in each workspace if it should no longer be associated. Plugin activation does not activate a credential service, and inactive services block only creation of new credentials.

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
