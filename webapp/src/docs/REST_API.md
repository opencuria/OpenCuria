# REST API Reference

Base URL: `/api/v1`

Interactive docs (Swagger UI): `/api/v1/docs`

---

## Authentication

All endpoints (except `/auth/login/`, `/auth/register/`, `/auth/refresh/`, and `/health/`) require authentication.

Two authentication methods are supported and can be used interchangeably:

### 1. JWT Bearer Token (short-lived, 30 min)

Obtain tokens via `POST /auth/login/`. Pass in the `Authorization` header:

```
Authorization: Bearer <access_token>
```

Use `POST /auth/refresh/` to exchange a refresh token for a new token pair.

### 2. API Key (long-lived)

Create API keys via `POST /auth/api-keys/` (requires active JWT session to bootstrap).

**Option A — Authorization Bearer header** (compatible with most HTTP clients):
```
Authorization: Bearer kai_xxxxxxxxxxxxxxxx
```

**Option B — X-API-Key header** (for n8n, Zapier, and tools with dedicated API key fields):
```
X-API-Key: kai_xxxxxxxxxxxxxxxx
```

#### Token format

Tokens are prefixed with `kai_` for easy identification in logs and secret scanners:
```
kai_<48-byte url-safe random string>
```

#### Security notes

- The raw token is returned **once** at creation time and is not stored by opencuria (only a SHA-256 hash is kept).
- Save your token immediately after creation — it cannot be retrieved again.
- Revoke keys via `DELETE /auth/api-keys/{id}/`.

---

## Organization Header

Most endpoints that operate on organization-scoped resources require an additional header:

```
X-Organization-Id: <org-uuid>
```

Endpoints that require this header are marked with **[org]** below.

---

## Error Responses

All errors follow this shape:

```json
{
  "detail": "Human-readable message",
  "code": "machine_readable_code"
}
```

Common HTTP status codes:

| Status | Meaning |
|--------|---------|
| 400 | Invalid request body / validation error |
| 401 | Missing or invalid credentials |
| 403 | Authenticated but not authorised |
| 404 | Resource not found |
| 409 | Conflict (duplicate resource) |
| 422 | Validation error (missing or invalid fields) |

---

## Endpoints

### Health

#### `GET /health/`

No authentication required.

**Response 200:**
```json
{"status": "ok"}
```

---

### Auth

#### `POST /auth/login/`

No authentication required.

**Request:**
```json
{
  "email": "user@example.com",
  "password": "s3cr3t"
}
```

**Response 200:**
```json
{
  "access_token": "<jwt>",
  "refresh_token": "<jwt>"
}
```

---

#### `POST /auth/refresh/`

No authentication required.

**Request:**
```json
{"refresh_token": "<jwt>"}
```

**Response 200:** same shape as login.

---

#### `GET /auth/me/`

Returns the authenticated user and their organization memberships.

**Response 200:**
```json
{
  "id": 1,
  "email": "user@example.com",
  "first_name": "Alice",
  "last_name": "Smith",
  "organizations": [
    {
      "id": "uuid",
      "name": "Acme Corp",
      "slug": "acme-corp",
      "role": "admin",
      "created_at": "2026-01-01T00:00:00Z"
    }
  ]
}
```

---

### API Key Management

#### `GET /auth/api-keys/`

List all API keys for the authenticated user. Raw tokens are never returned.

**Response 200:**
```json
[
  {
    "id": "uuid",
    "name": "n8n prod",
    "key_prefix": "kai_abc12345",
    "is_active": true,
    "created_at": "2026-02-01T10:00:00Z",
    "last_used_at": "2026-02-27T09:15:00Z",
    "expires_at": null
  }
]
```

---

#### `POST /auth/api-keys/`

Create a new API key. The raw token is returned **once** — save it immediately.

**Request:**
```json
{
  "name": "n8n prod",
  "expires_at": null
}
```

Set `expires_at` to an ISO 8601 datetime to create an expiring key, or `null` for a key that never expires.

**Response 201:**
```json
{
  "id": "uuid",
  "name": "n8n prod",
  "key_prefix": "kai_abc12345",
  "is_active": true,
  "created_at": "2026-02-27T12:00:00Z",
  "last_used_at": null,
  "expires_at": null,
  "key": "kai_abc12345xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
}
```

The `key` field is **not** available after this response.

---

#### `DELETE /auth/api-keys/{key_id}/`

Revoke an API key immediately. Subsequent requests using this key return 401.

**Response 204:** No content.

**Response 404:**
```json
{"detail": "API key not found.", "code": "not_found"}
```

---

### Organizations

#### `GET /organizations/`

List organizations the authenticated user belongs to.

**Response 200:**
```json
[
  {
    "id": "uuid",
    "name": "My Team",
    "slug": "my-team",
    "role": "admin",
    "created_at": "2026-01-01T00:00:00Z"
  }
]
```

---

#### `POST /organizations/`

Create a new organization.

**Request:**
```json
{
  "name": "My Team",
  "slug": "my-team"
}
```

**Response 201:** Organization object (same shape as list item).

---

#### `GET /organizations/{org_id}/`

Get a single organization by UUID.

> **Note:** The path parameter is the organization **UUID**, not the slug.

**Response 200:** Organization object.

---

### Runners

#### `GET /runners/` [org]

List all runners visible to the organization.

**Response 200:**
```json
[
  {
    "id": "uuid",
    "name": "local-runner",
    "status": "online",
    "available_runtimes": ["docker"],
    "organization_id": "uuid",
    "connected_at": "2026-01-01T00:00:00Z",
    "disconnected_at": null,
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:00:00Z"
  }
]
```

---

#### `GET /runners/{runner_id}/` [org]

Get a single runner.

**Response 200:** Runner object (same shape as list item).

---

### Workspaces

#### `GET /workspaces/` [org]

List workspaces. Supports query params:
- `runner_id` — filter by runner
- `status` — filter by status (`creating`, `running`, `stopped`, `failed`)

**Response 200:** Array of workspace objects.

---

#### `POST /workspaces/` [org]

Create (and start) a new workspace. Returns immediately — creation happens asynchronously.

**Request:**
```json
{
  "runner_id": "uuid",
  "name": "My workspace",
  "repos": ["https://github.com/org/repo"]
}
```

- `repos`: list of Git repository URLs to clone (can be empty `[]`)

**Response 202:**
```json
{
  "workspace_id": "uuid",
  "task_id": "uuid",
  "status": "creating"
}
```

---

#### `GET /workspaces/{workspace_id}/` [org]

Get a single workspace without chat session history.

**Response 200:**
```json
{
  "id": "uuid",
  "runner_id": "uuid",
  "status": "running",
  "name": "My workspace",
  "runtime_type": "docker",
  "repos": ["https://github.com/org/repo"],
  "created_by_id": 1,
  "created_at": "2026-01-01T00:00:00Z",
  "updated_at": "2026-01-01T00:00:00Z",
  "has_active_session": false
}
```

---

#### `POST /workspaces/{workspace_id}/stop/` [org]

Stop a running workspace. Asynchronous.

**Response 202:** Task object.

---

#### `POST /workspaces/{workspace_id}/resume/` [org]

Resume a stopped workspace. Asynchronous.

**Response 202:** Task object.

---

#### `DELETE /workspaces/{workspace_id}/` [org]

Remove a workspace (stops and deletes the container). Asynchronous.

**Response 202:** Task object.

---

#### `POST /workspaces/{workspace_id}/prompt/` [org]

Send a prompt to a workspace. Returns immediately; output is streamed via WebSocket.

**Request:**
```json
{
  "prompt": "Add unit tests for the auth module"
}
```

Optionally include `"chat_id"` to continue an existing chat thread.

**Response 202:**
```json
{
  "session_id": "uuid",
  "task_id": "uuid",
  "chat_id": "uuid",
  "status": "running"
}
```

Streaming output is delivered over WebSocket (`/ws/frontend/`).

---

#### `GET /workspaces/{workspace_id}/sessions/` [org]

List all sessions (prompt/response pairs) for a workspace across all chats.

**Response 200:** Array of session objects.

---

#### `GET /workspaces/{workspace_id}/chats/` [org]

List all chat threads for a workspace.

**Response 200:**
```json
[
  {
    "id": "uuid",
    "workspace_id": "uuid",
    "name": "Chat title…",
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:00:00Z",
    "session_count": 3
  }
]
```

---

#### `POST /workspaces/{workspace_id}/chats/` [org]

Create a new chat thread.

**Response 201:** Chat object.

---

#### `PATCH /workspaces/{workspace_id}/chats/{chat_id}/` [org]

Rename a chat thread.

**Request:**
```json
{"name": "New chat name"}
```

**Response 200:** Updated chat object.

---

#### `DELETE /workspaces/{workspace_id}/chats/{chat_id}/` [org]

Delete a chat thread and all its sessions.

**Response 204:** No content.

---

#### `GET /workspaces/{workspace_id}/chats/{chat_id}/sessions/` [org]

List sessions within a specific chat thread.

**Response 200:** Array of session objects.

---

#### `GET /workspaces/{workspace_id}/image-artifacts/` [org]

List image artifacts for a workspace.

**Response 200:** Array of image artifact objects.

---

#### `POST /workspaces/{workspace_id}/image-artifacts/` [org]

Capture an image artifact from the current workspace state.

**Response 201:** Image artifact object.

---

#### `DELETE /workspaces/{workspace_id}/image-artifacts/{image_artifact_id}/` [org]

Delete an image artifact.

**Response 204:** No content.

---

#### `POST /workspaces/{workspace_id}/image-artifacts/{image_artifact_id}/workspaces/` [org]

Create a workspace from an image artifact.

**Response 202:** Task object with new workspace info.

---

### Conversations

#### `GET /conversations/` [org]

List all chat threads across all workspaces, with metadata about each chat.

Supports `workspace_id` query param to filter.

**Response 200:**
```json
[
  {
    "chat_id": "uuid",
    "workspace_id": "uuid",
    "workspace_name": "My workspace",
    "workspace_status": "running",
    "agent_definition_id": "uuid",
    "agent_type": "claude",
    "chat_name": "Short title…",
    "last_session": {
      "id": "uuid",
      "prompt": "The last prompt",
      "status": "completed",
      "created_at": "2026-01-01T00:00:00Z"
    },
    "session_count": 3,
    "updated_at": "2026-01-01T00:00:00Z",
    "is_read": true
  }
]
```

---

#### `POST /conversations/read/` [org]

Mark a session as read.

**Request:**
```json
{"session_id": "uuid"}
```

**Response 204:** No content.

---

### Agents

#### `GET /agents/` [org]

List available agent definitions.

**Response 200:**
```json
[
  {
    "id": "uuid",
    "name": "claude",
    "description": "Claude Code — autonomous coding agent powered by Anthropic models.",
    "available_options": [
      {
        "key": "model",
        "label": "Model",
        "choices": ["claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5"],
        "default": "claude-sonnet-4-6"
      }
    ],
    "default_env": {},
    "supports_multi_chat": true,
    "has_online_runner": true
  }
]
```

---

### Credentials

#### `GET /credential-services/`

List supported credential service types (Anthropic API Key, SSH Key, etc.).

**Response 200:**
```json
[
  {
    "id": "uuid",
    "name": "Anthropic API Key",
    "slug": "anthropic-api-key",
    "description": "Anthropic API Key for Claude Code agent",
    "credential_type": "env",
    "env_var_name": "ANTHROPIC_API_KEY",
    "label": "Anthropic API Key"
  }
]
```

---

#### `GET /org-credential-services/` [org]

List all credential services with per-organization activation status.
Admin role required.

**Response 200:**
```json
[
  {
    "id": "uuid",
    "name": "GitHub Token",
    "slug": "github-token",
    "description": "GitHub Personal Access Token for repository access.",
    "credential_type": "env",
    "env_var_name": "GITHUB_TOKEN",
    "label": "GitHub PAT",
    "is_active": true
  }
]
```

---

#### `POST /org-credential-services/` [org]

Create a new credential service from organization settings and activate it for the current organization.
Admin role required.

**Request:**
```json
{
  "name": "GitHub Enterprise Token",
  "slug": "github-enterprise-token",
  "description": "Token for GitHub Enterprise API access",
  "credential_type": "env",
  "env_var_name": "GHE_TOKEN",
  "label": "Personal Access Token"
}
```

**Response 201:** Credential service with `is_active: true`.

---

#### `POST /org-credential-services/{service_id}/activation/` [org]

Activate or deactivate a credential service for the current organization.
Admin role required.

**Request:**
```json
{
  "active": true
}
```

**Response 200:** Credential service with updated `is_active`.

---

#### `GET /credentials/` [org]

List the authenticated user's stored credentials.

---

#### `POST /credentials/` [org]

Store a new credential (encrypted at rest).

**Request:**
```json
{
  "service_id": "uuid",
  "name": "My Anthropic Key",
  "value": "sk-ant-xxxx"
}
```

- `service_id`: UUID of the credential service from `GET /credential-services/`

**Response 201:** Credential object.

---

#### `PATCH /credentials/{credential_id}/` [org]

Update an existing credential (e.g. rotate the value).

**Request:** Same shape as POST (all fields optional).

**Response 200:** Updated credential object.

---

#### `GET /credentials/{credential_id}/public-key/` [org]

Get the public key for an SSH key credential.

**Response 200:**
```json
{"public_key": "ssh-ed25519 AAAA..."}
```

---

#### `DELETE /credentials/{credential_id}/` [org]

Delete a stored credential.

**Response 204:** No content.

---

### Skills

#### `GET /skills/` [org]

List available skills.

---

#### `POST /skills/` [org]

Create a new skill.

---

#### `PATCH /skills/{skill_id}/` [org]

Update a skill.

---

#### `DELETE /skills/{skill_id}/` [org]

Delete a skill.

**Response 204:** No content.

---

## n8n Integration Guide

### Prerequisites

1. A running opencuria backend accessible from your n8n instance.
2. An API key created via `POST /auth/api-keys/`.
3. A running workspace ID.

### HTTP Request node configuration

| Field | Value |
|-------|-------|
| Method | `POST` |
| URL | `https://your-opencuria.example.com/api/v1/workspaces/{workspace_id}/prompt/` |
| Authentication | **Header Auth** |
| Header name | `X-API-Key` |
| Header value | `kai_xxxxxxxxxxxxxxxx` |

Add a second header:

| Header name | Header value |
|-------------|-------------|
| `X-Organization-Id` | `your-org-uuid` |

**Body (JSON):**
```json
{
  "prompt": "{{ $json.prompt }}"
}
```

### Example: Trigger agent on GitHub issue

1. **GitHub Trigger** node → listens for new issues
2. **Set** node → extracts `issue.body` as `prompt`
3. **HTTP Request** node → `POST /api/v1/workspaces/{workspace_id}/prompt/` with `X-API-Key` and `X-Organization-Id` headers
4. **WebSocket** node (optional) → subscribe to output stream

---

## Zapier Integration Guide

Use Zapier's **Webhooks by Zapier** action with **POST** and set:

- **URL**: `https://your-opencuria.example.com/api/v1/workspaces/{workspace_id}/prompt/`
- **Headers**:
  - `X-API-Key: kai_xxxxxxxxxxxxxxxx`
  - `X-Organization-Id: your-org-uuid`
- **Data**: JSON body as above
