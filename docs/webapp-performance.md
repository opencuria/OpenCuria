# Webapp performance investigation

## Scope and method

Compared the unchanged production build saved before this patch with the optimized production build in isolated Chromium contexts. Vite preview served both builds on the same machine. Playwright exercised the actual compiled Vue components, navigation, REST requests and Socket.IO handlers; deterministic HTTP/WebSocket fixtures replaced the backend, provider and runner. No production state or credentials were used.

The benchmark measures uncompressed JavaScript response bodies and V8 heap after explicit garbage collection. These are **not** compressed network transfer sizes or total browser/process RSS. It covers:

- cold unauthenticated login and authenticated Home;
- settings deep links and terminal activation;
- an 80-message virtualized transcript and a 25.5 kB Markdown answer delivered in 200 updates;
- sequential SPA navigation through 12 distinct chats (20 messages, approximately 64 KiB of text per chat), reopening an old chat, editing a message and busy/idle events;
- idle chat CPU, sampled message DOM counts and uncaught errors.

## Findings and changes

1. **Eager feature loading.** The synchronous application shell included all settings panels. Shared workspace components pulled in xterm, Git and file tools even with the side panel closed. The shell, settings panels and heavy tool tabs/viewers now load on demand. The settings event listener and persistent desktop host remain mounted; activated tabs still use `v-show` so switching/collapsing does not restart their sessions.
2. **ResizeObserver feedback.** The virtual chat repeatedly disconnected/reobserved unchanged rows, and a measurement counter retriggered observation. It now observes only added nodes, unobserves removed nodes and ignores unchanged measurements. Cached height prefixes and binary search replace whole-transcript scans on scroll. Spacer heights, focus pins, scroll anchors and bottom following remain intact; shrinking transcripts are clamped synchronously.
3. **Streaming invalidation.** Every delta sorted/replaced the parts array, invalidating subtask lookups even for ordinary text. Sorting is now conditional on membership/order changes. Streaming Markdown updates are coalesced into 50 ms windows, with an immediate final flush. Completed messages still render immediately and full-document sanitization/reference-link semantics are unchanged.
4. **Transcript retention.** Previously every opened transcript remained cached. Unprotected inactive transcripts now have a four-session LRU and a conservative estimated 16 MiB budget, recalculated at fetch/selection/idle rather than per delta. Active/viewing/busy sessions, relevant lineage, pending gates, pinned details and in-flight reads are protected. Eviction drops associated UI/detail data, not server history; reopening still performs the normal awaited/coalesced REST fetch.
5. **Media retention and request ownership.** A 32 MiB media-cache budget evicts least-recently-used unpinned entries. Visible previews/lightboxes and optimistic uploads hold reference-counted pins; URLs are revoked only when safe. Pin releases are batched; cache getters do not sweep the cache. Reset generations prevent stale components releasing new owners' pins. Unknown/late media results cannot release another request's concurrency slot, and timeout tombstones no longer accumulate.
6. **Unmount races.** Polling cannot restart after its owner is disposed. An asynchronous Home mount cannot subscribe to file-search events after navigation away.

The budgets are UI-cache policies, **not hard total-memory limits**. Active/busy/pinned data may exceed them to preserve functionality. No dependencies were added.

## Final comparison

Final sequential runs on the same workspace, with no test/build process competing for CPU:

| Metric | Before | After | Interpretation |
| --- | ---: | ---: | --- |
| Login JS response bodies | 949,435 B | 301,120 B | 68.3% less startup JS |
| Authenticated Home JS response bodies | 1,682,574 B | 946,385 B | 43.8% less startup JS |
| Home JS heap after GC | 9,776,804 B | 8,292,280 B | 15.2% lower in this fixture |
| Heap growth from first to twelfth chat | 3,054,748 B | 1,584,748 B | 48.1% less growth; retention levels off after the cache fills |
| Idle chat task duration over 1 s | 31.183 ms | 11.899 ms | Diagnostic, timing varies by machine/run |
| Idle chat script duration over 1 s | 5.597 ms | 0 ms | No recurring observer work in this sample |
| Stream delivery-to-render wall time | 5,714 ms | 2,370 ms | Diagnostic, not a general speedup guarantee |
| Maximum sampled mounted message rows during stream | 8 | 8 | Existing DOM bound preserved |

Streaming CPU and wall time are sensitive to scheduling and automation. Timing is recorded, not asserted as a percentage regression threshold. Both builds passed the functionality assertions with zero page errors and failed requests. The optimized build did not fetch terminal code until its tab was activated. Deferred code still exists and is loaded when requested; this is not a reduction in total application functionality.

Final raw reports and screenshots (workspace-local):

- `/workspace/.opencuria/playwright/performance/results/baseline-final-1792026040388.json`
- `/workspace/.opencuria/playwright/performance/results/optimized-final-1792026066167.json`
- `/workspace/.opencuria/playwright/performance/results/optimized-final-1792026066167-streamed-answer.png`

## Verification and reproduction

Final checks: all **1,844 Vitest tests across 191 files** passed; production build/type-check passed; ESLint, Oxlint and Prettier checks passed for changed webapp files. Twenty existing browser regressions passed, including mobile image previews, focus/keyboard controls, sidebar pagination, inbox gates and live status updates. Baseline and optimized production benchmark runs passed.

Build and serve the current app:

```bash
cd webapp
npm run build
npm run preview -- --host 127.0.0.1 --port 4174 --strictPort
```

In another shell:

```bash
cd e2e
E2E_PERF_BASE_URL=http://127.0.0.1:4174 E2E_PERF_LABEL=optimized \
  npx playwright test -c performance.config.ts
```

The benchmark defaults to system Chrome at `/usr/bin/google-chrome-stable`; override `E2E_CHROME_PATH` if necessary. It uses fixtures and does not require a running backend. Artifacts are written under `/workspace/.opencuria/playwright/performance/results/`.

For an actual before/after comparison, build the old revision **before applying changes**, preserve its `dist` directory, and serve it on a separate port. The current investigation retained it at `/tmp/opencuria-dist-before`, served with `npm run preview -- --outDir /tmp/opencuria-dist-before --port 4173 --strictPort`. Run the same test with `E2E_PERF_BASE_URL=http://127.0.0.1:4173 E2E_PERF_LABEL=baseline`. The temporary baseline is not a repository artifact.

## Remaining scaling limits

- A single enormous active transcript still comes from an all-messages timeline API and remains in memory. Backend pagination/incremental history loading would address this separately.
- Base64 media transport still causes peak allocations while assembling/decoding large files. Cache eviction reduces retained memory, not those transfer peaks or video/image decoder memory.
- Expanded file trees are not row-virtualized, and mention indexing can traverse large loaded trees. These are follow-up candidates; no speculative rewrite was made here.
- Real desktop streams, terminals, large Git histories, browser extensions and production data can dominate total browser RSS. The fixture results do not claim a universal percentage reduction for those workloads.
