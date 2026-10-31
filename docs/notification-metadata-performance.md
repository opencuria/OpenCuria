# Notification activity metadata projection

Measured on 2026-10-31 against fetched `origin/dev` (`8659647`). The tree was
clean; existing performance branches were already merged. Only notification
activity materialization is optimized here.

## Investigation and selection

Inspected backend repositories/services, runner file/Git/metrics services,
web Markdown streaming, mobile snapshots and shared reducers/session traversal.

| Rank | Opportunity | Evidence / impact and risk |
| --- | --- | --- |
| 1 | Pending-question lookups scan retained history | No question session/status index. With 30,000 historical questions and 100 pending gates, attention service p50 29.865 ms / p95 39.970 ms; SQLite reports a table scan and temporary sort. Frequent sidebar path, high confidence, localized index/projection opportunity. |
| 2 | Notification repair visits historical answers | Once-per-minute `recent_completions()` has no completion-time index. With 30,000 old answers and no recent results, p50 54.693 ms / p95 74.937 ms; plan visits messages through each root's session index and sorts. High confidence, lower frequency, schema change needs write-cost assessment. |
| 3 | Notification activity reads unused bodies and wide models | Source reads load full assistant text, engine metadata and workspace/session fields; snapshots load wide session/workspace models. Every notification source and activity snapshot pays this cost. Strong allocation evidence, small repository-only fix with low regression risk. |
| 4 | Session list loads unused engine state | 100 sessions plus DTO mapping: p50 5.943 ms / p95 6.844 ms. Normal engine state is small; lower expected impact. |
| 5 | Shared descendant traversal repeatedly scans sessions | Potential quadratic behavior for reverse-ordered deep trees. Normal subagent depth limits reduce practical impact; no representative profile established. |
| 6 | Mobile snapshot metadata cloning | Sessions/todos are cloned and frozen on every publication, including streaming events. Plausible allocation cost; native profiling is still needed. |
| 7 | Web Markdown reparsing | Already coalesced at 50 ms; further changes risk Markdown semantics and require browser profiling. Runner file/Git paths also already batch and bound much of their I/O. |

The top three balance frequency, measured cost, confidence, regression risk and
architectural fit. `secrets.randbelow(3)` selected index **2**, metadata reads.
There was no redraw. Raw investigation and selection are in
[`benchmarks/notification-metadata/`](benchmarks/notification-metadata/).

## Change and boundaries

`ConversationActivityRepository` explicitly selects the fields consumed by the
activity DTOs in root, latest-assistant, completion-source and busy-root reads.
Answer bodies, engine state/metadata and unrelated workspace fields never cross
these SQL reads. The service still constructs the same immutable DTOs and owns
outcome interpretation. All existing consumers use the optimized repository.

Ownership/organization filters, latest-assistant position ordering, snapshot
ordering, error/cancellation interpretation and pending-gate semantics are
unchanged. No cache, dependency, migration, execution-loop or public-contract
change is introduced. Exact query-count tests prevent deferred-field N+1 reads.

## Measurements

Python 3.12.3, Django 5.2.17, SQLite 3.45.1, disposable file-backed databases,
DEBUG=False. The reproducible script uses 50 roots, 0/16/512 KiB answer bodies,
five warmups and 31 timed samples per workload. Real services, ORM reads and
activity DTO construction are included; push providers/network are excluded.
Query capture, output hashing and tracemalloc run separately from timings.

Sequential isolated baseline/current repetitions, with no concurrent test suite:

| Workload | p50 ms before → after | p95 ms before → after | Peak Python KiB before → after |
| --- | --- | --- | --- |
| 50-root activity read, 16 KiB answers | 7.917 → 6.178 | 13.100 → 9.825 | 280.6 → 143.0 |
| Single source, 16 KiB answer | 1.830 → 1.369 | 1.976 → 1.475 | 70.1 → 26.9 |
| 50 completion-source reads, 16 KiB answers | 79.044 → 58.278 | 95.163 → 62.721 | 234.3 → 179.4 |
| Single source, 512 KiB answer | 2.040 → 1.364 | 2.225 → 1.557 | 565.2 → 26.8 |
| 50 completion-source reads, 512 KiB answers | 101.554 → 67.202 | 133.561 → 82.256 | 734.5 → 179.9 |

Every output SHA-256 matches. Query counts remain 3 per activity list, 2 per
single source and 50 per 50-source burst. Long-answer single-source allocations
fall 95.3%; 50-source median latency falls 33.8%. Snapshot allocations roughly
halve even with ordinary 16 KiB answers. Initial sequential timings overlapped
tests and were noisy; the table uses the isolated repeat. Allocations were
consistent. These are warm local integration timings, not production SLOs;
tracemalloc peaks are temporary Python allocations, not RSS or database memory.

The existing **complete `NotificationService.snapshot()` benchmark** also ran
40 interleaved baseline/current samples against identical fixtures, including
membership/device checks and payload construction. All payload pairs match:

| Roots / child depth | p50 ms before → after | p95 ms before → after | Peak Python KiB before → after | Queries |
| --- | --- | --- | --- | --- |
| 1 / 0 | 6.088 → 5.970 | 10.695 → 9.572 | 84.3 → 55.1 | 6 → 6 |
| 20 / 0 | 6.410 → 5.409 | 6.562 → 5.679 | 149.6 → 92.8 | 6 → 6 |
| 20 / 1 | 8.174 → 7.078 | 8.730 → 8.231 | 149.0 → 102.5 | 8 → 8 |
| 100 / 3 | 20.027 → 15.942 | 26.205 → 21.280 | 487.5 → 400.2 | 10 → 10 |

The 100-root workload is a scale check, not typical user concurrency. One-root
snapshot latency is largely dominated by the unchanged authorization queries.

Reproduce source measurements (archive the baseline backend outside the repo):

```bash
backend/.venv/bin/python scripts/benchmark_notification_activity.py \
  --backend-root /path/to/baseline/backend
backend/.venv/bin/python scripts/benchmark_notification_activity.py
cd backend
ACTIVITY_BASELINE_REF=8659647 .venv/bin/pytest -q -s \
  apps/harness/tests/benchmark_activity_snapshot.py
```

## Verification

- Before implementation: 60 relevant existing tests passed. All six new SQL
  projection cases failed against the baseline, confirming regression coverage.
- After implementation: 170 relevant SQLite tests passed (notifications,
  conversation reads/feed/activity and adapter boundaries); four paired snapshot
  benchmark cases also passed.
- The 29 activity and notification integration tests also passed on local
  PostgreSQL using a dedicated disposable database/role. Pytest warned about an
  open test connection at teardown; the outer cleanup forcibly dropped that
  database and removed the role. PostgreSQL latency was not benchmarked.
- Regression tests cover empty/large private payloads, exact DTO values/query
  counts, fresh completion/error state and workspace organization reassignment.
  Existing tests cover gates, child exclusion, missing sessions, ordering,
  cancellation outcomes, delivery authorization and worker behavior.
- Ruff lint/format and `git diff --check` pass. UI/runner checks are unnecessary
  for this repository-only backend change.
