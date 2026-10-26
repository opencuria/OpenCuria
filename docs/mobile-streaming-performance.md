# Mobile streaming: share unchanged immutable parts

## Selection and scope

Baseline: `d89987e4b33093f433102e9cac912aea8e1e8864` (the fetched `origin/dev` on 2026-10-26; full revision is
recorded with the benchmark results). The working tree was clean. Local and
remote branches were checked before inspection; prior request-lifecycle,
web-resource and lazy-timeline optimizations were already merged.

The survey covered backend, runner, webapp, mobile and both shared packages.
Candidates were ranked by measured impact, path frequency, confidence, complexity,
regression risk and architectural fit. The initial probes used synthetic local
fixtures and establish opportunities, rather than production latency claims.

| Rank | Candidate | Evidence | Impact / risk |
| --- | --- | --- | --- |
| 1 | Backend scheduled-task history overfetch | 100-run authenticated REST: 35.16 ms p50, 39.51 ms p95; 14.98 MB peak Python allocations for a 36.2 KB response. Full assistant content and unused sessions are joined. | Low-risk repository projection; affects history opening/refresh. |
| 2 | Runner upload archive conversion | 2,000 files: 221.62 ms median synchronous conversion; 247.68 ms maximum event-loop gap. Archive metadata is scanned before files are extracted. | Affects all concurrent RPC responsiveness during an upload; cancellation/fencing needs care. |
| 3 | Mobile immutable stream copies | Actual bundled controller handler: 3.49 ms p50 per event at 2,000 parts in the scout. Every part and its nested display/input/meta objects are copied on every chunk. | Every streamed token; larger gains on long turns, moderate snapshot-integrity risk. |
| 4 | Shared streaming reducer scans | Vue-reactive 2,000-part reducer: 0.83 ms median per delta. Part lookup and stream-closing scan repeatedly. | Frequent, but smaller benefit relative to semantic risk. |
| 5 | Backend conversation-state overfetch | Feed loads resumable engine state and summary that its projection does not use. Current engine state is normally small. | Low-risk projection; meaningful impact not established. |
| 6 | Web dashboard feed sorting | Already ordered feed is sorted again with repeated date parsing. Request deduplication and Markdown render coalescing are already present. | Low complexity, probably small normal-workload benefit. |

The mobile/reducer scout times are medians of per-batch mean event costs;
the dedicated benchmark below measures individual events.

An actual `secrets.choice(top_three)` draw using OS randomness selected **rank 3,
mobile immutable stream copies**. No redraw was needed. Other candidates remain
unchanged.

## Change and architectural boundaries

`ChatController.onPartUpdated` previously cloned the entire target message,
including every completed part's nested metadata, then recursively froze the new
objects. This work grows with the whole turn for every incoming chunk, competing
with React Native rendering on the JS thread.

`packages/chat-core/src/harnessReducer.ts` now provides
`applyPartDeltaImmutable`. It prepares a new destination message and copies only
the addressed part and open text/reasoning streams that a boundary can close.
Completed unrelated parts and their hydrated details retain their identity. It
then delegates to the existing mutable reducer, keeping one implementation of
protocol transitions and ordering. The mobile controller uses this shared entry
point and retains its ownership checks, snapshot freezing and publication.

There is no cache, dependency, network/protocol change, asynchronous task or new
mutable state. Backend agent logic and the dumb runner are unaffected. Unchanged
parts are already deeply frozen by the controller; new rows are frozen before
publication. Snapshot consumers cannot change previous turns or nested details.
The remaining traversal is linear; this change eliminates the costly object and
nested-array copies rather than maintaining another index or cache.

## Measurement

Run from the repository root, with dependency installation already complete:

```bash
node --expose-gc scripts/benchmark_mobile_stream.mjs --baseline-ref d89987e --output before.json
node --expose-gc scripts/benchmark_mobile_stream.mjs --output after.json
```

The script bundles the actual controller and shared modules with the existing
esbuild installation. `--baseline-ref` reads tracked source from Git without
changing the working tree. Each sample opens the controller through its public
workspace/session methods using deterministic REST responses and then emits 100
ordered realtime chunks through registered event listeners. The fixture contains
50 prior messages and one live turn with 100, 500 or 2,000 parts, including bounded
nested tool projections. Each size has three warmups and 31 fresh samples, with
GC before each timed sample. An attached subscriber observes every publication.

Timings include ownership checks, reducer execution, immutable snapshot freezing,
publication and the subscriber. They exclude fixture creation/hydration, real
network/provider latency and native rendering. The final benchmark records the
individual duration of all 3,100 measured events per size; p50/p95 are event
percentiles. This is a Node 22 Linux CPU measurement, not a handset frame-rate or
production SLO claim. Benchmark records and transcript hashes accompany this
report under `docs/benchmarks/mobile-streaming/`.

The benchmark additionally checks ordered complete text, unchanged previous
snapshots, frozen output, notification count and unrelated message identity.
A deterministic identity count records replaced part objects, avoiding flaky
wall-clock thresholds in regression tests.

| Parts in live turn | Event p50 ms, before → after | Event p95 ms, before → after | Replaced part objects |
| --- | --- | --- | --- |
| 100 | 0.208 → 0.030 | 0.308 → 0.089 | 100 → 1 |
| 500 | 0.967 → 0.057 | 1.431 → 0.119 | 500 → 1 |
| 2,000 | 3.750 → 0.186 | 4.927 → 0.288 | 2,000 → 1 |

At 2,000 parts, p50 decreased **95.0%** and p95 **94.2%**. All three complete-transcript SHA-256 hashes match. Only one part object is replaced per text chunk at every size, eliminating 1,999 unnecessary part copies (plus nested object/array copies) in the largest fixture. This deterministic count measures object churn, not peak RSS or total bytes allocated.

## Verification

- Before implementation: shared chat/client, mobile controller/platform and native
  component/session suites all passed (191 tests); no pre-existing failure was
  observed in those relevant suites.
- Final `npm run test`: **2,351 passed** — 80 shared chat-core, 27 shared client,
  67 mobile logic, 35 native component/session, 2,142 webapp tests. Existing Vue
  Pinia and jsdom navigation warnings remain visible.
- `npm run type-check`: passed for webapp, mobile and shared client.
- `npm run export -w mobile`: Android and iOS Hermes JavaScript exports passed.
- Eighteen new regression/characterization cases cover frozen snapshots, parity
  with mutable transitions, text/reasoning boundaries, queued/running/completed/
  failed tools, detail preservation, step/agent/patch/compaction/steer events,
  ordering changes, replay, missing-message routing, late completed-turn events,
  workspace rejection and 2,000 unchanged part identities.
- Android emulator: the existing development APK loaded current source through
  Metro and authenticated against an isolated synthetic Django/Socket.IO server.
  A 2,000-tool-part chat received all 40 text chunks at 5 Hz while a draft was
  edited and retained. The busy-to-idle event restored the send control; work
  details could be expanded. Foreground/reconnect refreshed
  REST history. No external provider, runner or production service was used.
- Native stress limitation: an initial 25 Hz paragraph-heavy run, also exercising a
  2,000-row tool-group expansion, accumulated rendering backlog and disconnected
  in the memory-constrained emulator. `WorkedGroup` currently mounts every tool
  row on expansion; virtualizing that separate path is a further candidate. The
  emulator also required a smaller RAM allocation and temporary swap to boot.
  There is no comparable native baseline, so no handset FPS, high-rate native
  throughput or native p95 improvement is claimed. iOS native/device checks were
  unavailable; both platform JS bundles were exported.
- Formatting and `git diff --check` passed. Final review keeps ORM/services,
  presentation boundaries, event ordering and existing auth/cancellation flows
  intact. No backend/runner production code was changed.
