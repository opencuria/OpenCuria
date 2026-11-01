# Chat state ownership

`ChatTimelineController` owns the asynchronous transcript lifecycle used by web
and mobile: request deduplication and freshness, busy/idle snapshot reconciliation,
part detail loading, detail LRU and inactive transcript retention. Its constructor
ports provide transport, transcript storage, current session status and protected
session identities. It imports no framework or API client.

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
mutates a previously published native snapshot. Selection, live runs, pending gates,
lineage, outstanding requests and explicitly pinned details protect transcripts.
