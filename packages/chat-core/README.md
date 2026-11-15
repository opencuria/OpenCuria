# @opencuria/chat-core

Framework-neutral chat rules shared by web and mobile. Import modules directly
(`@opencuria/chat-core/<module>`); clients never re-export them
(`test/clientBoundaries.test.ts`). No framework or API client imports.

## Chat timeline

`ChatTimelineController` owns the transcript lifecycle: request deduplication and
freshness, busy/idle reconciliation, part details, detail LRU and retention of
inactive transcripts. Transport, storage and session data come in through
constructor ports. The Vue store supplies reactive records, the native adapter
immutable snapshots; adapters never implement a second cache policy.

- A request context identifies navigation/auth ownership; `clear`, `forget` and
  invalidation revoke outstanding requests.
- A busy snapshot arriving after idle keeps the final answer until the refresh.
- Eviction writes a new transcript; published native snapshots are never mutated.

## Desktop viewer

`createDesktopViewerCoordinator` (`desktopViewer.ts`) owns a client's KasmVNC viewer
lease per workspace: start, confirmation, renewal, recovery and release.
`desktopSurface.ts` holds the viewer URL, framebuffer defaults and reconnect backoff.
