# Provider-history replay: bounded part batches

## Investigation and selection

Baseline: `2757f9ec17de71785cbfd9ea3ad4699e99f7ec4f`, latest `origin/dev` at
the start. Both AGENTS files were read, local/remote branches checked, and
`git fetch origin && git switch -C dev origin/dev` run before code inspection.
The worktree was clean. Existing streaming, plugin-catalogue, history-checkpoint,
conversation-feed and runner-probe optimizations were already merged.

Candidates were ranked by impact, execution frequency, measured evidence, risk,
and architectural simplicity after exploring all major components:

| Rank | Candidate | Evidence and assessment |
| --- | --- | --- |
| 1 | Backend provider-history part reads | One query/executor hop per retained assistant before every native follow-up. Initial 100-turn measurement: 102 queries, 155.620 ms p50. Local read-only optimization with ordering, compaction and memory risks. |
| 2 | Runner directory-upload conversion | Actual conversion of 4,000 one-KiB files took 292.569 ms p50 and delayed a concurrent five-ms heartbeat to 292.631 ms. Offloading pure conversion could improve responsiveness across workspaces; cancellation needs care. |
| 3 | Backend workspace authorization | Real access checks issue 3–4 queries including credential payload columns; p50 3.265–5.105 ms. Common Socket.IO/desktop path; a minimal access projection could avoid unrelated reads while preserving service-owned permission decisions. |
| 4 | Mobile streaming snapshots | Each delta clones current-message parts and republishes cloned/frozen sessions and lineage. Device profiling and immutable ownership work needed. |
| 5 | Runner file-mention search | Sorts all matches before applying the result limit. Real cost unmeasured; depth-order and truncation behavior complicate bounded traversal. |
| 6 | Shared Agent-S timeline scans | Repeated full-array scans: helper p50 0.565/2.434/14.529 ms at 50/200/500 steps. Default is only 15 steps, so normal impact is less compelling. |
| 7 | Web streamed Markdown parsing | Full-document parse/sanitize/traversal, but existing 50-ms coalescing already bounds frequency. Browser evidence needed before risking incremental syntax handling. |

The first three were shortlisted before a single successful OS-random
`secrets.choice` draw at `2026-10-25T09:05:15.278755+00:00` selected
**history-parts-batching**. No redraw occurred. Ranking, selection and raw
measurements are in [the benchmark record](benchmarks/harness-history-batching.json).

## Implementation

`HarnessService._build_history` still performs session-scoped message loading,
exclusions, checkpoint selection and compaction slicing first. It then requests
parts for at most **eight retained assistants** at a time, removes each group
after replay, and keeps a cancellation/scheduling point between assistants.
Empty groups are retained so legacy assistants without parts cannot advance the
batch cursor incorrectly.

`HarnessPartRepository.history_parts_by_message` owns the batched ORM read and
grouping. It fetches text, steer and tool parts in per-message timeline order,
omitting other part types and unused display/timestamp/position columns. Existing
tool eligibility, truncation, attachment handling and conversational replay stay
in the service. The full-record repository methods remain for their other callers.

For `R` retained assistants, SQL reads fall from `2 + R` to
`2 + ceil(R / 8)`. Empty/user-only histories still issue one message query.
There is no cache, migration, dependency, parallel query execution, extra write or
REST/Socket.IO/MCP change. Other components are unchanged.

## Measurements

The existing `scripts/benchmark_harness_history.py` now supports paired runs:

```bash
mkdir -p /tmp/history-baseline
git archive 2757f9e backend | tar -x -C /tmp/history-baseline
backend/.venv/bin/python scripts/benchmark_harness_history.py \
  --baseline-repo /tmp/history-baseline --turns 1 10 50 100 --samples 51
backend/.venv/bin/python scripts/benchmark_harness_history.py \
  --baseline-repo /tmp/history-baseline --turns 10 50 --samples 51 --output-kib 512
```

It loads archived service/repository modules under private names against the same
unchanged models and disposable file-backed SQLite database. Five warmups precede
51 samples per version/scenario, alternating version order on every pair. Each
turn has a user prompt, assistant and five parts (start, text, tool, text, finish).
Compacted cases retain five turns. Complete provider-history SHA-256 hashes must
match across every sample/version. SQL capture and allocation tracing run outside
latency timing. Python 3.12.3, Django 5.2.17, SQLite 3.45.1; `DEBUG=False`.

Normal tool output is 32 KiB per turn:

| Turns | Compacted | Queries before → after | p50 ms before → after | p95 ms before → after |
| ---: | :---: | ---: | ---: | ---: |
| 1 | No | 3 → 3 | 3.472 → 3.582 | 5.701 → 5.966 |
| 10 | No | 12 → 4 | 21.509 → 9.431 | 27.228 → 12.374 |
| 50 | No | 52 → 9 | 76.576 → 25.588 | 146.185 → 46.011 |
| 100 | No | 102 → 15 | 157.513 → 49.262 | 250.845 → 83.654 |
| 1 | Yes | 3 → 3 | 3.111 → 3.211 | 4.557 → 4.753 |
| 10 | Yes | 7 → 3 | 8.288 → 4.595 | 12.358 → 8.583 |
| 50 | Yes | 7 → 3 | 9.521 → 5.991 | 11.449 → 6.958 |
| 100 | Yes | 7 → 3 | 13.278 → 9.332 | 14.844 → 10.796 |

At 100 un-compacted turns: **85.3% fewer queries, 68.7% lower p50 and 66.7%
lower p95**. At 50 turns p50 improves 66.6%. Single-turn histories save no
queries and add approximately 0.1 ms median overhead; no speedup is claimed there.
All replay hashes match. These are local pre-provider integration measurements,
not production network/provider latency or PostgreSQL performance claims.

Normal-workload Python allocation peaks are essentially unchanged: at 100 turns,
3,793,477 → 3,775,440 bytes; compacted, 349,806 → 349,922 bytes.

Oversized **512-KiB tool outputs** expose the batching tradeoff. At 50 turns,
p50 improves 122.980 → 64.493 ms and p95 367.226 → 178.572 ms, while peak Python
allocations rise **3,874,847 → 7,077,311 bytes** (about 3.2 MB extra). Compacted:
p50 29.209 → 21.917 ms; allocations 1,389,923 → 3,278,418 bytes. Raw future parts
are bounded by eight assistant messages, not an absolute byte cap. The native
tool executor normally applies its 50-KiB/2,000-line output cap before persistence;
the stress fixture deliberately inserts larger legacy-style results. This change
does not claim zero memory regression for oversized outputs. Allocation peaks
are not process RSS.

## Verification

- Before edits, **153 relevant existing SQLite tests passed**, with no failures.
- Final broad SQLite selection: **357 passed**, covering architecture boundaries,
  history/compaction/overflow, tools/attachments/truncation, persistence, steering,
  cancellation/subagents, edit/fork, REST, Socket.IO and harness MCP behavior.
  API/staticfiles/coroutine-cleanup warnings reproduce in an archived baseline
  API selection (**65 passed**, 177 warnings versus 176 in the broad final run).
- Focused history suite: **25 passed**, including fixed query budgets at batch
  boundaries, retained/foreign/excluded part identities, deferred display fields,
  empty part groups, cancellation between assistants and later-batch read errors.
  Eight query/projection cases fail against the archived baseline.
- Disposable PostgreSQL 16: **129 passed**; the final 25 history cases also pass.
  Archived baseline passes its corresponding **121 tests**. Both versions warn
  about open test-worker connections during database teardown (26 baseline,
  31 optimized); the isolated server is stopped after verification.
- Ruff lint, formatting of the test/benchmark files, Python compilation, Django
  system check and `git diff --check` pass. Existing formatting outside changed
  blocks is left untouched. No frontend code changed, so no UI build/device or
  browser responsiveness result is claimed.

Detailed test logs and exploratory probes are under
`/workspace/reports/perf-history-batching-20261025/`.
