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
- A sent prompt shows at once as a `local-user-*` row (`optimisticPrompts.ts`).
  Every snapshot keeps it until the server's copy arrives, which inherits its
  `client_key`; lists key rows by `messageRenderKey`, so the bubble stays.
- `edit` sends an edited prompt the same way, in the bubble it replaces. The
  replaced rows never return from snapshots or late events (`isDiscarded`); a
  rejected edit restores them.
- `tailLimit` opts in to ranged loading: a chat opens with its newest messages,
  `backfill` adds older pages in front, and a reload asks only for messages from
  the last two held ones on. Held messages must match the server's `older_count`,
  otherwise the controller reloads everything. Without it, or on a backend that
  ignores ranges, every load is complete.

## Dictation

`voiceInput.ts` holds the rules both composers share (limits, waveform levels,
checks before upload, failure kinds, where the text goes in the draft).
`runVoiceTranscription` retries lost connections until they return and a busy
service a few times; `VoiceTranscriptionQueue` keeps recordings in client
storage until their text was delivered or the user discarded them, so nothing
has to be recorded twice.

## Desktop viewer

`createDesktopViewerCoordinator` (`desktopViewer.ts`) owns a client's KasmVNC viewer
lease per workspace: start, confirmation, renewal, recovery and release.
`desktopSurface.ts` holds the viewer URL, framebuffer defaults and reconnect backoff.
