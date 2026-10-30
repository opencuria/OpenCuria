# Bounded file mention ranking

Baseline: `c5d57cb` on `origin/dev`, 2026-10-30. Both AGENTS files were read,
branches checked, and the clean worktree fetched/reset to `origin/dev` before
inspection. No local changes needed stashing. Existing performance branches and
recent Git, transcript, metadata and mobile snapshot optimizations were reviewed.

## Investigation and selection

Backend services/repositories, runner file/metrics operations, Vue chat/composer
rendering, mobile chat snapshots and shared client/reducers were inspected.
Candidates were ranked by impact, frequency, confidence, implementation risk and
architectural fit. Exploratory measurements below are separate from the final
comparison; they establish costs, not promised speedups.

| Rank | Candidate                        | Evidence and tradeoff                                                                                                                                                                                                                                                                                                                                          |
| ---- | -------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1    | Backend unread completion lookup | 100 chats × 200 messages, 4 KiB bodies: 25.0 ms p50 / 25.5 ms p95 over 20 samples on disposable SQLite. Query plan uses the session index and scans message history for the aggregate. Frequent feed refresh; a suitable completion index/query needs migration and write-cost validation.                                                                     |
| 2    | Shared file mention ranking      | 10,000 loaded explorer paths, all matching `file:util`: 104.8 / 113.6 ms in Node, 30 samples. Full sorting repeatedly splits/lowercases paths to return eight files. Runs on the UI thread as the mention changes; localized, stateless improvement with direct behavior comparison.                                                                           |
| 3    | Runner file response parsing     | Actual `FileManager.read_file` with prebuffered SSH responses: 5 MiB costs 18.3 / 22.7 ms and 18.5 MiB peak traced allocations; 20 MiB costs 69.9 / 76.2 ms and 74.0 MiB. Twenty samples, three warmups; allocation tracing separate. Splitting every base64 line stalls the event loop. Transfer/network time excluded; less frequent than typing/feed reads. |
| 4    | Backend attention projection     | 100 sessions with 100 permissions and 100 questions, 4 KiB gate bodies: 6.45 / 6.61 ms, three queries. Hydrates complete gates to obtain session IDs; measurable but smaller than the shortlist.                                                                                                                                                               |
| 5    | Mobile snapshot publication      | Every publication clones/freezes session metadata and rebuilds ancestry/gate projections. Text deltas trigger it; message/part sharing is already optimized. Needs native profiling and careful immutable snapshot validation; source evidence only.                                                                                                           |
| 6    | Session-feed payload reads       | Root-session reads include engine state not consumed by the feed. Current checkpoints live separately; realistic payload benefit was less certain than the measured candidates.                                                                                                                                                                                |

Sequential VM metric probes were considered and ruled out: the registry already
uses concurrent collection. The top three above formed the shortlist before one
OS-random draw, `secrets.randbelow(3)`, returned **1** (zero-based), selecting
**shared file mention ranking**. No redraw occurred.

## Change

`packages/chat-core/src/harnessMentions.ts` scores each path once and keeps an
ordered window of eight files. With fixed cap k=8, ranking takes O(nk) work and
O(k) retained entries instead of sorting/filtering all matches. Explorer
flattening and merging still allocate O(n); this does not claim constant memory
for the complete composer pipeline.

Basename prefix, basename substring and full-path matches retain their priority.
Labels retain `localeCompare` ordering; ties retain input order, including at the
cap. Prefix handling, agent precedence, limits, insertion tokens and input-array
immutability are preserved. All state belongs to the function call. There is no
cache, dependency, asynchronous work or protocol change. Vue and mobile retain
their existing shared implementation; no logic moves into either frontend.
Mobile currently supplies no local file paths to this helper, so no native
device speedup is claimed.

## Measurements

Reproduce the shared pipeline (flatten explorer → merge search results → rank):

```bash
node scripts/benchmark_mention_ranking.mjs --baseline-ref c5d57cb
```

The script compiles baseline source from Git and current source with the existing
esbuild dependency. It compares 1,000 deterministic differential cases, including
Unicode, duplicates, collation ties, path scopes, missing and agent-only queries.
Each workload uses five warmups and 31 timed calls per implementation, with
interleaved execution order. p95 is nearest rank. Synthetic paths follow a fixed
permutation; setup, compilation, network and rendering are excluded.

Node 22.23.2 on this Linux workspace, final run with no build/test suite running:

| Loaded files / query    |   Baseline p50 / p95 | Optimized p50 / p95 |
| ----------------------- | -------------------: | ------------------: |
| 50 / `file:util`        |     0.225 / 0.431 ms |    0.031 / 0.088 ms |
| 1,000 / `file:util`     |    9.079 / 10.083 ms |    0.325 / 0.576 ms |
| 10,000 / `file:util`    | 126.017 / 149.101 ms |    3.580 / 4.279 ms |
| 10,000 / `file:`        |   17.240 / 18.250 ms |    2.382 / 2.897 ms |
| 10,000 / `file:util-9`  |   17.397 / 17.959 ms |    3.211 / 3.774 ms |
| 10,000 / `file:missing` |    7.662 / 14.060 ms |    2.920 / 3.814 ms |
| 10,000 / `agent:pl`     |     1.391 / 2.450 ms |    1.377 / 1.503 ms |

The broad 10,000-file query is 35.2× faster at p50. The agent-only branch is
unchanged; those timings include flattening/merging, and differences are noise.
At 50 files the no-match p95 varied from 0.105 to 0.264 ms despite a lower p50;
sub-millisecond timings are sensitive to scheduling and garbage collection.

### Running Vue components

Chromium, local Vite dev server: actual `HarnessChatInput` and
`HarnessChatContainer` components mounted together with Pinia, 10,000 loaded
files and mocked API/search responses. Each sample changes the mention among
`@file:u`, `@file:ut`, and `@file:util`, and appends assistant text in the same
update. Timing covers input-event dispatch through Vue's next tick and a layout
read, not physical input-to-paint or provider latency. Five warmups, 30 samples
per version, identical candidate lists and streamed text length:

| Metric        |  Baseline | Optimized | Reduction |
| ------------- | --------: | --------: | --------: |
| UI update p50 | 195.10 ms |   6.95 ms |     96.4% |
| UI update p95 | 212.60 ms |  17.50 ms |     91.8% |

Real keyboard typing with 20 ms simulated stream ticks, ArrowDown/Enter insertion,
mouse insertion, agent-only queries, no-match queries and Escape dismissal were
also exercised. This is component integration with simulated I/O, not an
authenticated deployment, runner transfer or native-device benchmark.

## Verification

- Before implementation: 125 shared tests and 11 existing mention tests passed;
  exploratory backend (57) and runner (66) tests also passed.
- Seven new characterization/work-budget tests. The budget test failed on the
  original's 119,910 locale comparisons for 10,000 files and passes after the
  change; it bounds comparisons/normalizations without a flaky latency limit.
- After implementation: 132 shared, 168 relevant Vue, 87 mobile logic and 93
  mobile UI tests passed. Workspace type-check and changed-file formatting pass.
- Before pushing, `origin/dev` advanced to `db89c1a` with a mobile chat update.
  The commit rebased cleanly; mention-ranking source had no overlap. On the
  rebased result, 142 shared, 87 mobile logic, 97 mobile UI and 23 Vue
  mention/container tests passed, as did the full workspace type-check.
- Production bundling could not finish in this 4 GiB VM: the first attempt ended
  with SIGTERM; two isolated retries exited 137. The final retry closed the
  browser/dev server and capped Node's old heap at 1,536 MiB; it transformed
  4,544 modules before termination. Kernel logs confirm global OOM kills of the
  Node build processes. No successful production build is claimed; the separate
  complete workspace type-check passed. Existing Vite glob-deprecation and Vue
  test-injection warnings were also emitted.

Remaining opportunities: unread lookup indexing/query shape, runner base64
response allocations, and mobile snapshot metadata publication. None were
changed in this optimization.
