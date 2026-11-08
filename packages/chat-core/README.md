# Chat state ownership

`ChatTimelineController` owns the asynchronous transcript lifecycle used by web
and mobile: request deduplication and freshness, busy/idle snapshot reconciliation,
part detail loading, detail LRU and inactive transcript retention. Its constructor
ports provide transport, transcript storage, the open scope's sessions (status and
lineage) and the sessions the adapter still shows or waits on. It imports no
framework or API client.

The Vue store supplies reactive records; the native adapter supplies immutable
snapshots. Both adapters retain navigation, authenticated session actions and UI
notifications, and feed the shared reducers for realtime message events. They do
not reconstruct fetched messages or implement a second transcript/cache policy.

A request context identifies navigation/auth ownership. `clear`, `forget`, and
invalidation revoke outstanding requests without relying on transport cancellation.
A busy snapshot arriving after idle preserves the final live answer until the
post-idle refresh completes. Request-scoped metadata is captured by `prepare` when
the request actually starts, including refreshes waiting behind an older flight.

Detail hydration checks ownership before and after transport and refuses unfinished
parts. Cache eviction writes a new transcript through the storage port; it never
mutates a previously published native snapshot. The controller keeps the lineage
family of every retained (selection, pending gates, todo fetches), busy, loading or
detail-pinned session; adapters report those sessions but never compute the
protected set.

# Desktop viewer

`createDesktopViewerCoordinator` (`desktopViewer.ts`) owns a client's KasmVNC
viewer intents per workspace: start with a new `intent_revision`, bounded
startup confirmation, renewal of a held lease, recovery from expired/released
leases or a new runner epoch, a release grace period for remounts, and the
final stop tombstone. It talks to the server through a transport port and
imports no framework or API client. The webapp wraps its session records in
Vue's `reactive` (`track`); the native app reads immutable `snapshot`s through
`subscribe`. `desktopSurface.ts` holds the viewer URL, framebuffer defaults,
connection-status parsing and the bounded reconnect backoff for both clients.

