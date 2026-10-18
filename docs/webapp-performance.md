# Webapp performance investigation

## Scope

This investigation compared the original and optimized production builds in
Chromium on the same machine. It recorded JavaScript response-body sizes and V8
heap after garbage collection; these measurements are not compressed network
sizes, total browser/process RSS, or a universal performance guarantee.

## Findings and changes

1. **Eager feature loading.** The synchronous application shell included all settings panels. Shared workspace components pulled in xterm, Git and file tools even with the side panel closed. The shell, settings panels and heavy tool tabs/viewers now load on demand. The settings event listener and persistent desktop host remain mounted; activated tabs still use `v-show` so switching/collapsing does not restart their sessions.
2. **ResizeObserver feedback.** The virtual chat repeatedly disconnected/reobserved unchanged rows, and a measurement counter retriggered observation. It now observes only added nodes, unobserves removed nodes and ignores unchanged measurements. Cached height prefixes and binary search replace whole-transcript scans on scroll. Spacer heights, focus pins, scroll anchors and bottom following remain intact; shrinking transcripts are clamped synchronously.
3. **Streaming invalidation.** Every delta sorted/replaced the parts array, invalidating subtask lookups even for ordinary text. Sorting is now conditional on membership/order changes. Streaming Markdown updates are coalesced into 50 ms windows, with an immediate final flush. Completed messages still render immediately and full-document sanitization/reference-link semantics are unchanged.
4. **Transcript retention.** Previously every opened transcript remained cached. Unprotected inactive transcripts now have a four-session LRU and a conservative estimated 16 MiB budget, recalculated at fetch/selection/idle rather than per delta. Active/viewing/busy sessions, relevant lineage, pending gates, pinned details and in-flight reads are protected. Eviction drops associated UI/detail data, not server history; reopening still performs the normal awaited/coalesced REST fetch.
5. **Media retention and request ownership.** A 32 MiB media-cache budget evicts least-recently-used unpinned entries. Visible previews/lightboxes and optimistic uploads hold reference-counted pins; URLs are revoked only when safe. Pin releases are batched; cache getters do not sweep the cache. Reset generations prevent stale components releasing new owners' pins. Unknown/late media results cannot release another request's concurrency slot, and timeout tombstones no longer accumulate.
6. **Unmount races.** Polling cannot restart after its owner is disposed. An asynchronous Home mount cannot subscribe to file-search events after navigation away.

The budgets are UI-cache policies, **not hard total-memory limits**. Active/busy/pinned data may exceed them to preserve functionality. No dependencies were added.

## Final comparison

| Metric | Before | After | Interpretation |
| --- | ---: | ---: | --- |
| Login JS response bodies | 949,435 B | 301,120 B | 68.3% less startup JS |
| Authenticated Home JS response bodies | 1,682,574 B | 946,385 B | 43.8% less startup JS |
| Home JS heap after GC | 9,776,804 B | 8,292,280 B | 15.2% lower in the measurement |
| Heap growth from first to twelfth chat | 3,054,748 B | 1,584,748 B | 48.1% less growth; retention levels off after the cache fills |
| Idle chat task duration over 1 s | 31.183 ms | 11.899 ms | Diagnostic, timing varies by machine/run |
| Idle chat script duration over 1 s | 5.597 ms | 0 ms | No recurring observer work in this sample |
| Stream delivery-to-render wall time | 5,714 ms | 2,370 ms | Diagnostic, not a general speedup guarantee |
| Maximum sampled mounted message rows during stream | 8 | 8 | Existing DOM bound preserved |

Streaming CPU and wall time are sensitive to scheduling. The optimized build did not
fetch terminal code until its tab was activated. Deferred code still exists and is
loaded when requested; this is not a reduction in total application functionality.

## Verification

At the time of this investigation, **1,844 Vitest tests across 191 files** passed;
the production build/type-check passed; ESLint, Oxlint and Prettier checks passed
for changed webapp files.

## Remaining scaling limits

- A single enormous active transcript still comes from an all-messages timeline API and remains in memory. Backend pagination/incremental history loading would address this separately.
- Base64 media transport still causes peak allocations while assembling/decoding large files. Cache eviction reduces retained memory, not those transfer peaks or video/image decoder memory.
- Expanded file trees are not row-virtualized, and mention indexing can traverse large loaded trees. These are follow-up candidates; no speculative rewrite was made here.
- Real desktop streams, terminals, large Git histories, browser extensions and production data can dominate total browser RSS; these observations do not claim a universal percentage reduction for those workloads.
