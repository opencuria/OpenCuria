# Provider history: read only retained message bodies

## Investigation and selection

Baseline: `4ac6c1c`, latest `origin/dev` when this run started on 2026-10-25.
Both AGENTS files were read and local/remote branches checked before inspecting
code. The required stash command found a clean worktree, then fetch and
`git switch -C dev origin/dev` completed. Existing stashes and reference
repositories were left intact. Earlier history-part batching, timeline loading,
plugin-catalogue and conversation-feed optimizations were already merged.

Candidates were ranked by user impact, frequency, evidence, implementation risk
and architectural simplicity after inspecting all five major components:

| Rank | Candidate | Evidence and assessment |
| --- | --- | --- |
| 1 | Runner archive-upload conversion | Real conversion inside `FileManager.upload_file` blocked a concurrent 1-ms heartbeat for about 284 ms with 4,000 small files. Affects other streams sharing the runner loop. Pure conversion appeared suitable for offloading. |
| 2 | Backend compacted provider history | A 400-turn integration probe retained five turns but materialized 800 message objects and 6.96 MB of message text. Runs before every native follow-up; targeted reads can eliminate discarded data without a cache. |
| 3 | Runner Git snapshot round trips | `_git_repo_snapshot_no_log` made seven sequential guest executions. An explicitly simulated 10-ms latency per execution produced 71-ms median latency. Real deployed SSH latency was not measured; concurrency/channel pressure needs care. |
| 4 | Mobile streaming snapshots | `ChatController.buildSnapshot` clones/freezes every session and lineage on publication; `prepareEventMessages` copies all parts of the active message. Source evidence; native profiling and immutable ownership verification needed. |
| 5 | Web streamed Markdown | `HarnessMarkdown.vue` reparses/sanitizes complete revisions, with existing coalescing. Source evidence; incremental parsing needs browser evidence and careful syntax/security testing. |
| 6 | Shared descendant traversal | `collectDescendantSessionIds` rescans sessions until no descendants are added. Reversed/deep trees increase scans; normal depth/fan-out and actual consumer cost need measurement. |

An actual `secrets.choice` over the top three selected archive upload. That
experiment was rejected: normal gzip iteration in a worker still produced long
heartbeat gaps, while tarfile's streaming reader reduced p95 gaps to
16.3 ms but increased complete upload p50 from 468.3 to 681.4 ms and runner CPU
from 311.9 to 520.7 ms. Buffer tuning was workload-sensitive. All runner edits,
tests and benchmark additions from that experiment were removed.

A second `secrets.choice`, over only the two remaining candidates, selected
**backend compacted provider history**. The ranked shortlist, both draws and
rejection evidence are preserved in
[selection.json](benchmarks/history-message-projection/selection.json).

## Implementation and boundaries

`HarnessService._build_history` now selects the latest eligible completed
assistant checkpoint before reading message bodies. The service still chooses
the checkpoint summary, parses `tail_start_id`, and specifies the fallback to
the checkpoint message's position. The old full-list slicing helper is removed.

`HarnessMessageRepository.list_for_history` resolves the optional start ID in a
scalar subquery and applies the caller's fallback with `Coalesce`. Both the
anchor and body query are session-scoped and exclude the supplied message IDs.
It reads only `id`, `role` and `content` from the retained suffix. The existing
unique `(session, position)` index makes this equivalent to slicing the ordered
message list, including position gaps and a start position of zero.

Checkpoint discovery no longer needs all assistant IDs in a large SQL `IN`
clause. It retains the same assistant-role, completed-state, exclusion and
message/part ordering rules. Blank summaries, missing/malformed/excluded/foreign
start IDs and legacy flattened assistant content retain their behavior.

Compaction/replay policy remains in the backend service; all ORM remains in
repositories. Part batching, tool/steer replay and cancellation points remain
intact. There are no writes, migrations, dependencies, caches or background
workers. REST/Socket.IO/MCP contracts and the runner/frontends are unchanged.
History reconstruction remains a multi-query read under existing run admission;
no new transaction/snapshot consistency guarantee is claimed.

## Reproduction and measurement

The existing `scripts/benchmark_harness_history.py` now supports realistic
assistant text sizes and records message rows/content bytes separately from
latency and allocation tracing. For example, from the repository root:

```bash
mkdir -p /tmp/opencuria-history-baseline
git archive 4ac6c1c backend | tar -xf - -C /tmp/opencuria-history-baseline
backend/.venv/bin/python scripts/benchmark_harness_history.py --turns 0 1 10 400 --samples 31 --message-kib 16 --baseline-repo /tmp/opencuria-history-baseline
backend/.venv/bin/python scripts/benchmark_harness_history.py --turns 0 1 10 400 --samples 31 --baseline-repo /tmp/opencuria-history-baseline
backend/.venv/bin/python scripts/benchmark_harness_history.py --turns 50 --samples 31 --message-kib 4 --baseline-repo /tmp/opencuria-history-baseline
```

The real service and repositories use one disposable file-backed SQLite database
per run. Each nonempty turn has a user prompt, assistant and five ordered parts,
including a 32-KiB tool output. Compacted cases retain the last five turns.
Five warmups precede 31 samples per version, alternating version order against
identical data in one process. No tests run concurrently with the final timing
runs. Complete provider-history SHA-256 digests must match across versions and
samples. Query counting, loaded-body instrumentation and tracemalloc peaks use
separate runs so they do not distort latency.

Raw results: [initial baseline](benchmarks/history-message-projection/baseline.json),
[16-KiB paired](benchmarks/history-message-projection/paired.json),
[short-text paired](benchmarks/history-message-projection/short-text-paired.json),
[4-KiB medium paired](benchmarks/history-message-projection/medium-paired.json).

For **400 turns with 16-KiB assistant text and five retained turns**:

| Metric | Before | After |
| --- | ---: | ---: |
| p50 latency (ms) | 27.905 | 4.529 |
| p95 latency (ms) | 103.349 | 4.899 |
| Peak Python allocations (bytes) | 7,860,162 | 434,976 |
| Message rows materialized | 800 | 10 |
| Message content loaded (bytes) | 6,559,090 | 81,990 |
| SQL queries | 3 | 3 |

This reduces p50 by **83.8%**, p95 by **95.3%** and peak Python allocations by **94.5%**. Query count stays constant; the improvement comes from eliminating discarded reads and unused field construction.

Other workloads, including counterexamples where little data can be discarded:

| Turns / assistant text | Compacted | Queries before → after | p50 ms before → after | p95 ms before → after |
| --- | :---: | ---: | ---: | ---: |
| 0 / 16 KiB | No | 1 → 2 | 1.967 → 2.485 | 2.407 → 2.766 |
| 1 / 16 KiB | No | 3 → 3 | 3.525 → 3.806 | 4.712 → 4.942 |
| 1 / 16 KiB | Yes | 3 → 3 | 3.475 → 4.028 | 4.637 → 5.622 |
| 10 / 16 KiB | No | 4 → 4 | 10.351 → 10.256 | 16.081 → 14.710 |
| 10 / 16 KiB | Yes | 3 → 3 | 5.139 → 5.371 | 8.038 → 8.332 |
| 400 / 16 KiB | No | 52 → 52 | 200.744 → 176.123 | 290.698 → 258.511 |
| 400 / 16 KiB | Yes | 3 → 3 | 27.905 → 4.529 | 103.349 → 4.899 |
| 50 / 4 KiB | No | 9 → 9 | 19.977 → 18.433 | 81.268 → 78.431 |
| 50 / 4 KiB | Yes | 3 → 3 | 10.709 → 7.606 | 11.672 → 8.346 |
| 400 / short | Yes | 3 → 3 | 22.970 → 4.337 | 100.615 → 5.065 |

An empty history now performs a checkpoint lookup before the empty message read: **two queries instead of one**, about **0.4–0.5 ms extra median latency** in the paired runs. One-turn cases also show a small (under 0.7 ms median) overhead and no useful saving. Short transcripts are not claimed to be faster. The large compacted cases remove 790 unnecessary message objects regardless of text size; at 50 turns the medium fixture also demonstrates the benefit before very long transcripts accumulate.

All paired provider-history digests match, including empty and uncompacted cases. No cache or retained background work is introduced.

These are warmed local integration measurements. PostgreSQL, deployed network
latency, provider response time and process RSS were not measured.

## Verification

Before production edits, all 35 existing history/compaction/persistence tests
passed. The final focused suite adds checks for loading only retained message
bodies, skipping unused engine metadata, foreign/excluded anchors, position zero
and gaps, and range-query failure propagation. Existing tests cover checkpoint
ordering/exclusions, blank summaries, legacy content, tool/steer ordering,
bounded part batches, cancellation and later-batch failures.

Final verification: **366 tests passed** across history, compaction/overflow,
persistence, harness services/API, workspace access, steering, edit/fork, subagent
lifecycle, robustness and the full MCP app tests. Ruff passes for every changed
Python file; format checks and `git diff --check` pass. Django system checks
report no issues. Frontend checks are unnecessary because no client code changes.

Baseline API/fork tests also pass (65 tests) and reproduce the unawaited test
coroutine and `ThreadSensitiveContext` teardown warnings seen in the broader
run. No baseline test failures were found. No frontend or runner code ships in
this change, so UI/device/build checks do not apply.
