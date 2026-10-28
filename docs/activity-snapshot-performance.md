# Notification activity snapshot performance

Measured on 2026-10-28 against `ecd6666` (the fetched `origin/dev`).

## Investigation and selection

Reviewed all major components, including existing performance branches and
already merged optimizations. These six opportunities remained plausible:

| Rank | Candidate | Evidence and impact/risk assessment |
| --- | --- | --- |
| 1 | Harness activity query fan-out | `ConversationActivityService.active_rows()` independently reads each root's assistant and walks every pending gate's ancestors. Twenty busy roots with one child question each cost 63 queries, p50 33.489 ms / p95 34.652 ms, and read 1.25 MiB of unused response text. Repeated in native activity snapshots and live-activity delivery; localized repository/service change. |
| 2 | Message-edit full-history reads | `HarnessService.edit_user_message()` and edit admission each load every message to find a target/suffix. Two reads of 200 messages containing 64 KiB each cost 25 MiB of content, p50 26.582 ms / p95 30.830 ms. Significant for long-chat editing, but less frequent and touches admission semantics. |
| 3 | Runner regular-file download commands | `FileManager.download_file()` makes four sequential guest exec calls: containment, directory check, size, content. A real local 64 KiB download measured p50 8.892 ms / p95 12.437 ms, excluding SSH latency. Round-trip reduction could help remote runners; containment, limits and error handling make this riskier. |
| 4 | Runner file search sorting | `build_find_files_command()` sorts every match before applying the 50-result limit. Plausible large-tree mention-search cost; no representative large-tree profile collected. |
| 5 | Mobile snapshot metadata cloning | `ChatController.buildSnapshot()` clones/freezes all sessions and todos for every publication, including streaming tokens. Plausible CPU/allocation cost after prior part-sharing improvements; native profiling is still needed. |
| 6 | Web Markdown parsing | `HarnessMarkdown.vue` parses growing streamed responses. Plausible repeated work for long answers, but realistic browser profiling and parsing-semantics verification are needed. Shared reducers/render rules were also inspected. |

The top three balanced measured cost, affected-path importance, confidence,
implementation risk and architectural fit. An actual `secrets.choice()` draw
using OS randomness selected **harness activity query fan-out**. No redraw or
other candidate implementation was needed.

## Change

`conversation_activity_repository.py` now selects only the latest assistant's
completion timestamp through a correlated subquery in the existing owned-root
read. It uses the existing unique `(session_id, position)` index; SQLite's query
plan confirmed indexed lookup. It does not transfer assistant text.

The service reads the union of permission/question ancestry frontiers in batches
and resolves shared paths once per call. The single-root event path uses the
same resolver. Queries scale with ancestry depth rather than chat/gate count.
Maps live only within the call; there is no cross-request state or invalidation
policy. Missing links and cycles resolve to no root, as before.

ORM access remains in the harness repository, activity interpretation in its
service, and notification serialization/delivery in the notifications context.
Ownership/organization filters, latest-turn ordering, root ordering, permission
priority, pending-state checks and public contracts are preserved. There are no
schema changes, new dependencies, runner changes or frontend changes.

## Comparable full-service measurements

The opt-in benchmark calls `NotificationService.snapshot()` including organization
membership, device ownership, activity derivation and payload construction. Each
busy root has its own owned workspace and 11 messages; the latest assistant holds
64 KiB, the other messages 4 KiB each. Gate workloads have a question on each
deepest child and a permission on half of those same children.

Both original modules are loaded directly from Git and injected into the
unchanged notification service. Each comparison uses identical disposable pytest
data, three warmups and 40 interleaved samples in seeded shuffled order. Every
pair must return identical payloads, excluding only `updatedAt`. Query counting
and one-call `tracemalloc` peak allocation are measured separately from latency.
The original/current comparison was also run before implementation to establish
the baseline and validate the harness.

PostgreSQL 16, isolated local cluster over a Unix socket:

| Busy roots / child depth | Queries before → after | p50 ms before → after | p95 ms before → after | Peak Python KiB before → after |
| --- | --- | --- | --- | --- |
| 1 / no gates | 7 → 6 | 7.503 → 7.284 | 8.558 → 8.404 | 132.3 → 90.6 |
| 20 / no gates | 26 → 6 | 27.923 → 9.478 | 37.406 → 11.395 | 260.9 → 131.0 |
| 20 / 1 | 86 → 8 | 60.488 → 10.765 | 64.257 → 11.697 | 285.7 → 132.7 |
| 100 / 3 | 706 → 10 | 437.440 → 28.156 | 470.621 → 38.025 | 629.9 → 486.3 |

SQLite, file-backed development/test database:

| Busy roots / child depth | Queries before → after | p50 ms before → after | p95 ms before → after | Peak Python KiB before → after |
| --- | --- | --- | --- | --- |
| 1 / no gates | 7 → 6 | 5.339 → 5.211 | 5.647 → 5.497 | 132.4 → 84.3 |
| 20 / no gates | 26 → 6 | 18.846 → 6.864 | 20.061 → 7.534 | 255.1 → 150.2 |
| 20 / 1 | 86 → 8 | 44.122 → 8.715 | 54.962 → 12.524 | 276.4 → 149.8 |
| 100 / 3 | 706 → 10 | 347.747 → 20.549 | 480.285 → 33.166 | 604.8 → 488.7 |

The 20-chat PostgreSQL gate workload improves median latency by 82.2% and SQL
query count by 90.7%. The 100-chat case is a concurrency stress workload, not a
claim about typical user concurrency. These are local service integration
measurements, excluding HTTP, provider/network delay and production contention.
Peak allocation is Python memory during one call, not process RSS or DB memory.

Reproduce the SQLite comparison from `backend/`:

```bash
ACTIVITY_BASELINE_REF=ecd6666 .venv/bin/pytest -q -s \
  apps/harness/tests/benchmark_activity_snapshot.py
```

For PostgreSQL, invoke pytest after configuring Django's `DATABASES` for a
disposable local PostgreSQL server. The test suite creates its own test database;
no application database is used.

## Verification

- Before production edits: notification and edit suites yielded 103 passed and
  one pre-existing failure, `test_edit_preserves_auto_model_and_effort`
  (`KeyError` when looking up an already-finished background task). No edit code
  was changed. Runner download/chunking/path suites: 58 passed.
- The new query-growth regression was run against the original implementation:
  all four fan-out/depth cases failed as expected (8/123/12/213 queries).
- Optimized SQLite activity/notification/boundary checks: 103 passed. Additional
  conversation/socket/route/boundary checks: 113 passed (overlapping suites).
- PostgreSQL activity/notification/boundary tests and all four benchmark cases:
  105 passed. Teardown reported one open test-database connection; the isolated
  cluster was stopped after verification.
- Final regression checks: 7 passed on SQLite; 8 passed on PostgreSQL, including
  the benchmark after its module cleanup was added (20-chat p50 57.695 →
  9.957 ms, p95 64.288 → 10.887 ms, still 86 → 8 queries).
- Ruff lint/format and `git diff --check` passed for the final change.
- Regression coverage checks fixed-depth query counts at widths 1 and 30,
  assistant-position ordering, missing assistants, completed turns, root order,
  immediate state changes between calls, cycle/missing-link handling, shared
  ancestry, ownership/organization isolation, and absence of message content
  reads. Existing notification tests exercise gate resolution, delivery,
  revocation and REST authorization/error contracts.
- Both databases' benchmarks compare the entire returned snapshot on every run.
  Query state is local to each call, so no new synchronization or background
  resources are introduced.
