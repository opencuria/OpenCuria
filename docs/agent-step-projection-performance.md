# Agent-S timeline projection performance

## Investigation and selection

Started from `origin/dev` at `30b1542` after reading both AGENTS.md files,
checking all branches and fetching origin. The working tree was clean; no
stash was necessary. Existing performance branches had no unmerged commits
relative to dev, and their completed work was excluded.

The investigation covered backend services/repositories, runner file/Git/process
operations, web chat rendering, mobile snapshots/lists and shared chat helpers.
The shortlist was fixed before an OS-random `secrets.randbelow(3)` draw:

| Rank | Candidate | Current evidence | Impact and risk |
| --- | --- | --- | --- |
| 1 | Backend workspace attachment reads | Authenticated local REST lists with 1/10/50 workspaces performed 10/28/108 queries; 50-workspace p50 was 112.9 ms. Adapter credential reads discard existing prefetches; plugin IDs are read per workspace. | Frequent dashboard reads; repository/service batching must preserve filtering, order and ownership. Low/medium risk. |
| 2 | Shared Agent-S step projection | For each plan, `agentStepStatus` scanned the entire parts list repeatedly and rebuilt all agent step numbers. A plain 500-step projection took 17.4 ms p50; a real Vue streaming update took 330 ms. Running-sequence detection also rescanned completed steps. | Repeated during computer-use streaming and history rendering. Local algorithm change with legacy-status tests. Low/medium risk. |
| 3 | Runner untracked Git diffs | Real local Git subprocesses through the existing runtime fixture: 1/10/50 untracked paths required 6/15/55 guest executions; 50 paths took 157.6 ms p50. Each untracked path is awaited sequentially. | New-project diff panel latency; bounded concurrency needs SSH-channel, cancellation and memory checks. Medium risk. |
| 4 | Mobile streaming snapshots | `prepareEventMessages` clones every part of the active message; publications build session/lineage snapshots. Source evidence only. | Frequent path; immutable snapshot ownership and native profiling needed. |
| 5 | Web streamed Markdown | `HarnessMarkdown.vue` parses/sanitizes whole revisions after existing 50-ms coalescing. Source evidence only. | Frequent path; incremental parsing has reference-link and sanitization risks. |
| 6 | Backend image-version enrichment | Workspace serialization calls `ImageVersionService.version_ref` per base/pending image, with line/latest queries. Source evidence only; attachment probe used no images. | Dashboard reads; batching must preserve line activation and latest-version rules. |

The draw at `2026-10-25T19:03:34.205976+00:00` returned index **1**, selecting
Agent-S projection. No redraw occurred. The original selection and investigation
measurements are in [the benchmark record](benchmarks/agent-step-projection/).

## Change and boundaries

`packages/chat-core/src/harnessAgentSteps.ts` now collects step starts, finishes,
tool errors, active sequence steps and the maximum finite agent step in one pass.
`buildAgentStepViews` reuses those facts for every plan; `hasRunningAgentSequence`
uses the same collector. The existing single-step public helper shares the same
status implementation. Empty/no-agent view projections avoid building the index.

This removes quadratic scans and per-plan temporary arrays. Each invocation has
bounded temporary sets proportional to the input; there is no retained cache,
invalidation policy, dependency or asynchronous work. All rules remain in the
framework-neutral shared package. Webapp, mobile, backend and runner call sites
and network contracts are unchanged.

Behavior remains intentionally unchanged for missing/duplicate/out-of-order
markers, numbered and legacy plans, display-versus-meta precedence, nonfinite
steps, tool errors, final failures/aborts and stale final errors while streaming.
NaN is excluded from the index because Set equality would otherwise differ from
the original strict marker comparisons. Input arrays/parts are never mutated;
returned views retain the original part references.

## Measurement

The reproducible paired benchmark runs the original source directly from Git
and the current source with the same interpreter and identical fixtures:

```bash
node --expose-gc scripts/benchmark_agent_steps.mjs --baseline-ref 30b1542
```

Fixtures have a start, reasoning, plan, tool and finish per step, with the latest
step unfinished. Both complete view projection and running-sequence detection
are measured, on plain objects and real Vue reactive proxies. Version order is
randomized within each pair; timing excludes compilation, parity assertions,
read-count instrumentation and garbage collection. Small inputs are batched.
Full output equality and stable response digests are checked separately.

At 500 steps / 2,499 parts, combined part-type inspections fell from
**4,391,233 to 7,497**. The paired Node 22.23.2 run measured the forced complete
projection on Vue proxies at **1,630.6 → 6.1 ms p50**, **1,828.1 → 7.8 ms p95**.
This benchmark forces both derivations every time; the running UI can reuse
individual computed values, so these are not event-to-DOM times. Helper and
browser measurements shared the workspace VM, and scheduling/GC affect absolute
latency. The separate work counts and running-app comparison are stronger than
interpreting a synthetic helper speedup as an application-wide improvement.

A separate 20-call retained-heap probe after GC recorded +520 bytes for the
optimized helper versus +6,704 bytes for baseline. These small differences are
GC-sensitive; peak allocation and process RSS were not measured. The production
implementation retains no index between calls and uses O(distinct steps)
temporary sets.

Running-app measurements use Chromium, the actual Vite development application,
and an isolated local Django backend with a 500-step persisted synthetic chat.
They time the existing store's `handlePartUpdated` through Vue `nextTick`, with
three warmups, 21 samples and a 50-ms pause before each event. These include
normal reactive invalidation, derived view construction and DOM updates. They
exclude network/provider latency and the browser's later paint. The initial
comparison was **330.0 → 44.1 ms p50**, **357.7 → 68.7 ms p95**, with 20 mounted
agent cards in both cases. Two further paired reload comparisons served the
original helper directly from the archived source for baseline module requests:

| Browser run | p50 ms before → after | p95 ms before → after |
| --- | ---: | ---: |
| Pair 1 | 356.4 → 37.1 | 395.5 → 47.6 |
| Pair 2 | 361.3 → 34.0 | 413.1 → 46.5 |
| Pooled 42 samples per version | **359.4 → 35.7** | **413.1 → 47.5** |

The pooled median fell **90.1%**, p95 **88.5%**. Each version used the same
fixture, event sequence, browser and viewport; execution order was randomized
within each pair. There were still 20 mounted agent cards and the last step
remained in progress in every run. This is a local long-transcript result, not a
production SLO or a claim that every update meets a 16-ms frame budget.

## Verification

Before source edits, all 19 existing Agent-S helper tests passed. The new shared
characterization suite passed 11 behavior cases on the original source; its two
linear-work checks failed there and pass after optimization. The checks count
part inspections rather than asserting noisy wall-clock deadlines.

A separate deterministic comparison of 3,000 mixed transcripts across five
final/streaming outcomes found exact view, running-sequence and single-step
status parity with the original source. Cases include malformed metadata,
legacy payloads, errors, duplicates and nonfinite steps.

Final checks:

- `npm test`: **2,321 passed** (55 chat-core, 25 client, 66 mobile logic,
  35 mobile UI, 2,140 webapp). No test failures.
- `npm run type-check`: webapp, mobile and shared client passed. The new shared
  test file also passed a separate TypeScript check.
- `npm run build-only -w webapp`: production build passed. Existing Vite glob
  option deprecation remains.
- Oxlint, Prettier, Node syntax validation and `git diff --check` passed for
  changed code/benchmark files. Existing Vue test warnings are recorded in
  `/workspace/reports/performance-workspace-20261025/npm-test.log`.
- Initial investigation checks also passed: 46 existing backend workspace/API
  tests and 123 runner Git tests plus 14 subtests. Neither component was changed.

The running application was exercised for continued streaming, pagination while
reading older steps, detail expansion through the real REST endpoint, a new step,
its finish marker, a final abort and a late event after message completion.
The final step changed from in-progress to completed to failed; the previous
step stayed completed, and the late event was ignored. No provider, runner or
external service was invoked. Screenshot: `/workspace/screenshots/agent-step-projection-verified.png`.

Unselected candidates remain unchanged. Production traffic and native-device
performance were not measured; the mobile screen does not currently use this
Agent-S helper, so no native rendering improvement is claimed.
