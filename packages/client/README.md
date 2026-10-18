# @opencuria/client

Framework-neutral OpenCuria REST, Socket.IO realtime, and workspace-file client for Expo and web. ESM source exports only; no browser globals, Vue, or storage access.

```ts
import { createClient } from '@opencuria/client'

const client = createClient({
  apiBaseUrl: 'https://curia.example/api/v1',
  wsBaseUrl: 'https://curia.example',
  getAccessToken: () => accessToken,
  getRefreshToken: () => refreshToken,
  setTokens: async (tokens) => saveTokens(tokens),
  getOrganizationId: () => organizationId,
  onAuthenticationExpired: () => clearSession(),
})

const user = await client.auth.me()
const workspaces = await client.workspaces.list()
const sessions = await client.harness.listSessions(workspaces[0].id)
client.realtime.connect()
const release = client.realtime.subscribeWorkspace(workspaces[0].id)
const stop = client.realtime.on('harness.part_updated', (event) => {})
```

`client.api` exposes `request<T>(method, path, body?, signal?)`, `get`, `post`, `patch`, and `delete`. `ApiRequestError` preserves `status`, `code`, and raw `data`. Requests omit ambient credentials; non-auth 401s share a context-safe refresh and retry once. Auth expiry only follows a refresh 401/403; network and server failures leave the current session intact. Absolute and protocol-relative request paths are rejected before auth headers are attached. Web integration may pass `options.fetch`; callers can preserve `ApiRequestError` identity by re-exporting that class and these request functions from the same client instance. The desktop clipboard-gesture sync exception is deliberately outside this native client.

Additional groups: `auth` (login/me), `workspaces`, `conversations`, `harness` (sessions, messages, timeline/details, gates, session actions, engines/models/config/recent usage), `skills`, and `files` (`findFiles`, `listFiles`, `readFile`, `downloadFile`, `uploadFile`). Wire find paths (`{name,path}`) are normalized to `/workspace/...` strings in `findFiles().paths`; out-of-root or pathless entries are discarded rather than reconstructing paths from untrusted names. Realtime maintains logical event/workspace subscriptions through ordinary transport reconnects. An auth/org/API-base change clears old workspace ownership and event bindings; callers should reacquire workspace subscription handles after switching context. Call `setActive(false|true)` from native app lifecycle handling; `dispose()` permanently clears listeners/subscriptions and rejects pending file transfers. `files` never queues writes while disconnected. If external token state is replaced outside login/refresh, call `client.api.notifyTokensChanged()` to bump the auth generation and reset realtime. See the typed exports in `src/index.ts` for exact signatures.
