# Provider history: eliminate the full-transcript compaction scan

## Scope and selection

Baseline: `ccbcd36726866831916f791f23461f5eced95a3d` on `origin/dev`.
Read both AGENTS files, checked all local/remote branches, and ran
`git fetch origin && git switch -C dev origin/dev` before inspecting code.
The worktree was clean, so there was nothing to stash. Existing request-lifecycle,
web-resource, lazy-timeline, Git lazy-loading and conversation-feed improvements
were already merged and were excluded.

Investigation covered backend, runner, webapp, mobile and shared packages. Ranked
by measured impact, frequency, evidence, implementation risk and architectural fit:

| Rank | Candidate | Evidence / impact | Risk |
| --- | --- | --- | --- |
| 1 | Backend history reconstruction | Every native follow-up; 50 assistant turns produced 101 queries, or 56 even with only five retained turns | Moderate: checkpoint selection, replay order, memory |
| 2 | Runner Docker observations | Actual Docker SDK with intercepted low-level API: seven workspace identity lookups made 84 API calls / 49 container inspections; one inventory needed 12 calls / seven inspections | Moderate: journal proof completeness and concurrency |
| 3 | Backend plugin runtime snapshot | Real SQLite runtime/repositories: 1/5/20 effective plugins made 6/18/63 queries; p50 3.887/10.598/36.539 ms | Low, but smaller typical plugin counts |
| 4 | Mobile streaming snapshot identity | Every publish clones sessions; changed row props invalidate historical memoized rows | Immutable ownership and device verification |
| 5 | Shared Agent-S projection | Existing helper benchmark: 100/250/500/1,000 synthetic plans took about 0.44/2.38/9.39/43.64 ms | Legacy status semantics; less frequent large histories |
| 6 | Mobile completion reconciliation | Idle and conversation-change handlers both force timeline/todo refreshes; source-level duplicate-request evidence | Cross-device/reconnect invalidation |
| 7 | Backend stream persistence | Each text delta awaits a part SELECT/UPDATE plus accumulated message UPDATE | Cancellation, durability and flush semantics |
| 8 | Runner Git state probes | Existing fake-runtime snapshot made 19 guest execs; four marker tests could be one | Quoting/security; smaller scope of benefit |
| 9 | Streamed Markdown processing | Whole-document repair on every throttled update; large-input synthetic scans exceed frame budgets | Incremental syntax correctness |

The top three were fixed before an OS-random draw using Python
`secrets.randbelow(3)` at `2026-10-24T01:20:13.110403+00:00`. It returned index 0,
selecting backend history. No redraw was needed; other candidates remain unfixed.

## Change and architectural boundaries

`HarnessService._build_history` previously found the last completed compaction by
loading **all parts of every assistant message**, including large tool outputs,
then loaded retained parts again to replay them. With `A` stored assistant messages
and `R` retained assistants, this costs `1 + A + R` queries.

- `backend/apps/harness/repositories.py`: one targeted, ordered
  `get_latest_completed_compaction(message_ids)` query. Filter completed compaction
  rows, sort by the same message/part timeline keys as the old traversal, and load
  only the checkpoint's message ID, summary and metadata.
- `backend/apps/harness/harness_service.py`: pass assistant IDs from the already
  session-scoped, exclusion-filtered message list; reuse the existing slicing and
  provider replay. Remove the obsolete per-message discovery implementation.

The new cost is `2 + R` queries when assistants exist, or one message query for a
new/empty conversation. **Checkpoint discovery**, not all provider replay, is now
constant-query. No claim is made that total history loading is constant-query.

ORM stays in repositories; compaction/replay rules stay in the backend service.
REST, Socket.IO, MCP, permissions, providers, runner and frontends are unchanged.
There is no cache, dependency, migration, background task, write, additional state
or new consistency guarantee. Exclusions precede checkpoint selection, and only
assistant IDs belonging to that session are eligible. Blank summaries, invalid or
missing tail IDs, tool/steer ordering and legacy content retain existing behavior.

### Rejected broader variants

An all-at-once part load reduced reads further but increased retention of large
outputs that are subsequently truncated. Async cursor variants added memory or
scheduling overhead; moving the whole replay into one synchronous worker weakened
cooperative cancellation boundaries. Those experiments were removed. The shipped
change keeps the existing per-assistant replay reads and cancellation points,
removing only the independently expensive full-transcript discovery pass.

## Reproduce and measurements

From the repository root:

```bash
backend/.venv/bin/python scripts/benchmark_harness_history.py
backend/.venv/bin/python scripts/benchmark_harness_history.py --turns 50 --output-kib 512
# The same script can measure an archived baseline:
backend/.venv/bin/python scripts/benchmark_harness_history.py --repo /path/to/baseline
```

The benchmark migrates temporary file-backed SQLite and deletes it on exit. It
runs the real service/repositories, with no providers, runners or external calls.
Each turn has a user prompt, assistant message and five parts (step start, text,
tool, text, step finish). Tool output is 32 KiB normally and 512 KiB in the stress
case. Compacted fixtures retain the last five turns. IDs/content are deterministic.
Each scenario uses 31 warmed samples with `DEBUG=False`; query counting and Python
allocation tracing are separate from latency. SHA-256 covers the complete provider
history, including IDs, tool calls and outputs, and is checked for sample stability.

The same script/interpreter ran sequentially on archived `ccbcd36` and the final
source; all before/after history digests matched:

| Assistant turns | Compacted | Queries before → after | p50 ms before → after | p95 ms before → after |
| ---: | :---: | ---: | ---: | ---: |
| 1 | No | 3 → 3 | 3.794 → 4.303 | 4.149 → 4.699 |
| 10 | No | 21 → 12 | 21.335 → 18.727 | 43.863 → 22.994 |
| 50 | No | 101 → 52 | 99.511 → 59.352 | 101.180 → 76.868 |
| 1 | Yes | 3 → 3 | 5.023 → 4.182 | 5.597 → 6.018 |
| 10 | Yes | 16 → 7 | 16.410 → 9.972 | 18.174 → 11.467 |
| 50 | Yes | 56 → 7 | 53.844 → 9.204 | 57.961 → 9.991 |

At 50 compacted turns: **87.5% fewer queries, 82.9% lower p50, 82.8% lower p95**.
Without compaction: **48.5% fewer queries, 40.4% lower p50**. One-turn histories
eliminate no queries and have no demonstrated latency improvement.

A repeat normal-size run confirmed 50-turn p50 99.456 → 69.181 ms without compaction
and 53.171 → 10.414 ms with compaction. VM scheduling varies: ten-turn un-compacted
p50 was effectively unchanged on that repeat. Query counts and payload parity are
deterministic; these local timings are not production PostgreSQL SLOs.

For 50 turns with **512 KiB tool outputs**, the compacted path improved from
56 → 7 queries, p50 **65.192 → 16.608 ms**, p95 **104.897 → 22.224 ms**; repeat p50
65.321 → 18.408 ms. Python allocation peak was 1,408,720 → 1,390,485 bytes. Normal
50-turn peaks were 1,938,683 → 1,927,765 bytes (un-compacted), 301,811 → 281,980
(compacted). Memory is Python allocation peak, not RSS or a hard byte bound.

The oversized **un-compacted** stress workload remains dominated by loading and
truncating all retained outputs: p50 was 139.730 → 115.540 ms, but p95 rose from
149.477 → 293.727 ms; repeat p50 was 149.412 → 148.462 ms and p95 230.135 → 374.651 ms.
Those separate-process tail samples were inconsistent, so a stricter check
alternated the archived/current history methods in randomized order on identical
fixtures in one process (101 warmed samples each, no instrumentation during timing).
For the un-compacted stress case, p50 was **171.152 → 103.118 ms**, p95
**242.522 → 167.268 ms**; process-CPU p50 was 170.917 → 103.288 ms. The compacted
case measured p50 **71.079 → 13.021 ms**, p95 **100.299 → 19.275 ms**, with matching
digests. This does not indicate a systematic stress-case regression, but production
tail latency remains unproven. Replay and its resource boundaries are unchanged.

## Verification

- Before source edits: **94 existing** service/history/persistence tests passed.
  Extended baseline coverage: **258 passed**, no failures.
- Final relevant SQLite run: **369 passed** (352 existing + 17 new cases), covering
  compaction/overflow, tools/attachments/truncation, steering, cancellation/error
  paths, subagent lifecycle/output, edit/fork, REST and the full MCP test directory.
- New regression cases check query scaling at 1/10/50 turns with/without compaction,
  exact loaded part IDs (discarded payloads must not be read), checkpoint projection,
  empty histories, timeline ordering, pending/error checkpoint exclusion, unrelated
  sessions, all message-exclusion options, blank summaries, malformed/missing/excluded
  tail IDs and interleaved tool/steer/text replay. All six query/loading cases fail
  against the old source; 11 behavior cases already pass there.
- A separate, socket-only **disposable PostgreSQL 16** instance passed **111 focused
  tests**, including all new cases. Its baseline passed the same 94 existing tests.
  Both PostgreSQL runs warn that 19 existing test-worker sessions delay database
  teardown; the disposable cluster was stopped afterward. No existing cluster or
  external database was touched.
- Existing API-test coroutine/context cleanup warnings, staticfiles warnings and
  dependency deprecations reproduce on the archived SQLite baseline. Warning counts
  vary with collection timing; no execution-lifecycle code was changed.
- Ruff passes for changed modules/new files; Python compilation, Django system check
  and `git diff --check` pass. No unrelated formatting or implementation remains.

No live UI was exercised: this is a backend-only read optimization with real ORM
integration and provider-history parity coverage, and no visible presentation change.
