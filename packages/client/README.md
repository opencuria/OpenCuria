# @opencuria/client

Framework-neutral REST, Socket.IO and workspace-file client for web and Expo. ESM
source only; no browser globals, Vue or storage access.

```ts
import { createClient } from '@opencuria/client'

const client = createClient({
  apiBaseUrl: 'https://curia.example/api/v1',
  wsBaseUrl: 'https://curia.example',
  getAccessToken: () => accessToken,
  getRefreshToken: () => refreshToken,
  setTokens: (tokens) => saveTokens(tokens),
  getOrganizationId: () => organizationId,
  onAuthenticationExpired: () => clearSession(),
})

const workspaces = await client.workspaces.list()
client.realtime.connect()
const release = client.realtime.subscribeWorkspace(workspaces[0].id)
```

- Groups: `api`, `auth`, `workspaces`, `conversations`, `harness`, `skills`, `files`,
  `desktop`; exact signatures in `src/index.ts`. The webapp builds its chat bindings
  with `createHarnessApi` from `@opencuria/client/harness`.
- Errors are `ApiRequestError` (`status`, `code`, `data`). A 401 refreshes once and
  retries; only a failed refresh expires the session.
- Requests time out after 30 s (`requestTimeoutMs`) and are never replayed.
  Absolute request paths are rejected before auth headers are attached.
- After switching auth, organization or API base, reacquire workspace subscriptions.
  Call `realtime.setActive()` on app lifecycle changes and `dispose()` at teardown.
