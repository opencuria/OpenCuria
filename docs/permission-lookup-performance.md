# Pending permission lookup index

Started from clean `origin/dev` at `b52ad4b863cead2cb7b13493c9c816d87b72bb92`,
after reading both AGENTS files, checking `git branch -a`, attempting an
include-untracked stash (nothing to save), fetching and resetting `dev`.
Existing performance and lazy-timeline branches were already merged.

## Investigation and random selection

The survey covered backend repositories/services and adapters, runner Git/file
execution and metrics, Vue chat containers/stores, native chat publication/list
rendering, and shared reducers, subtask traversal and file transport. Candidates
were ranked by demonstrated impact, path frequency, confidence, implementation
complexity, regression risk and architectural fit:

| Rank | Candidate | Evidence / impact | Scope and risk |
| --- | --- | --- | --- |
| 1 | Backend pending permission lookups | A 100,001-row scout measured 11.7 ms p50 for one session and 24.5 ms for 50 session IDs. SQLite plans showed a full table scan plus ordering sort. Timeline loads, feed attention and permission cleanup all use these reads. | One additive index; no query, policy or protocol change. High confidence and frequent path. |
| 2 | Runner untracked Git diffs | The complete `working_diff` RPC with 60 new files issued 65 guest commands and took 133.1 ms p50 across 10 warmed samples using real local Git, without SSH delay. `_git_working_diff` awaits one `_git_untracked_entry` per file. | Potentially substantial latency reduction; batching must preserve containment, output bounds, failures and cancellation. Moderate risk. |
| 3 | Backend credential deletion guards | `PluginService.credential_removal_gaps` with 40 effective plugins issued 43 SQL queries and took 33.9 ms p50 across 31 warmed samples. The caller repeats it for attached workspaces. | Reuse bulk requirement reads; low implementation risk, less frequent settings operation. |
| 4 | Mobile snapshot publication | `ChatController.buildSnapshot` copies/freeze-checks sessions, lineage, todos and gates on every stream publication. Selective part copying and list virtualization are already present. | Potential React prop churn, but no new device profile; preserve immutable snapshots and avoid duplicating shared rules. |
| 5 | Shared descendant traversal | `collectDescendantSessionIds` repeatedly scans sessions until it reaches a fixed point; reverse-ordered deep trees need repeated passes. | Source evidence only; ordinary shallow trees are likely inexpensive. Preserve ordering and cycle safety. |
| 6 | Web streamed Markdown / runner file parsing | The web already coalesces Markdown work; complete-source processing remains. Runner file reads split and rejoin the complete base64 body. | Source evidence only in this run; requires end-to-end profiling before selecting either. |

The fixed top three above were passed to Python `secrets.randbelow(3)` using OS
randomness. At `2026-10-27T10:04:11Z` it selected index **0**, pending permission
lookups. No redraw occurred. The original draw and scout evidence are preserved in
[`selection.json`](benchmarks/permission-lookup-investigation/selection.json).
The scouts are investigation evidence, not the comparable measurements below.
Only the selected candidate was changed.

## Change and architectural boundaries

`PermissionRequest.session_id` is deliberately a plain UUID, so it does not get
the automatic index of a Django foreign key. The pending repositories filter by
session ID(s) and `status`, optionally by tool, and order by `created_at`. With
only the primary-key index, those reads scan retained permission history from
all scopes, including resolved requests.

`backend/apps/harness/permissions/models.py` and migration
`0041_permission_request_lookup_index` add one B-tree on
`(session_id, status, created_at)`. Equality filters use its leading columns;
single-session reads also get chronological order directly. Multi-session reads
still sort their small matching result set. The full composite index works with
parameterized status predicates without relying on partial-index planning.

Existing repository queries serve REST, MCP and interaction services unchanged.
No business logic moves, no dependency or cache is added, and there is no new
application state, task, concurrency or invalidation mechanism. Auth/ownership,
redaction, gate lifecycle, event order, cancellation and response contracts stay
with their existing owners. The normal index adds storage and write maintenance;
deployment must apply migration 0041. Regular index creation holds a write lock
while it builds, so deployment scheduling should account for table size.

## Measurements

The benchmark uses disposable file-backed SQLite and a separate local PostgreSQL
16 cluster, with Python 3.12. It creates 100,000 approved/rejected historical
requests, 54 pending gates, 50 root conversations, four children of the selected
root and a 20-message transcript. Historical scopes include current sessions and
other sessions. Optional tool filtering and descendant reads are measured too.

Each path has three warmups and 51 samples, with `DEBUG=False`. HTTP measurements
use the authenticated Django test client, including routing, ownership checks,
ORM, service projection and JSON serialization. SQL counts include adapter work
on other threads. Counts, hashes, EXPLAIN and tracemalloc are captured separately
from timing. No provider, runner, external service or native UI is involved.

An initial before/after pair was followed by serial repeats against an archived
baseline and the changed checkout, without concurrent tests. All corresponding
response SHA-256 hashes match, including the small-history control. SQL counts
remain 26 per timeline, 12 per feed and one per direct lookup.

The serial repeats are the primary results:

| Engine / path | p50 ms before → after | p95 ms before → after | p50 reduction |
| --- | ---: | ---: | ---: |
| PostgreSQL authenticated timeline | 55.394 → 30.333 | 77.735 → 38.686 | 45.2% |
| PostgreSQL authenticated conversation feed | 42.282 → 25.543 | 59.402 → 36.673 | 39.6% |
| PostgreSQL one-session pending lookup | 11.300 → 0.697 | 12.392 → 0.764 | 93.8% |
| PostgreSQL descendant lookup with tool filter | 12.898 → 0.835 | 18.115 → 0.943 | 93.5% |
| PostgreSQL feed-scope pending lookup | 12.986 → 2.080 | 16.335 → 2.599 | 84.0% |
| SQLite authenticated timeline | 66.414 → 25.570 | 78.884 → 36.092 | 61.5% |
| SQLite authenticated conversation feed | 55.106 → 27.033 | 73.152 → 34.779 | 50.9% |
| SQLite one-session pending lookup | 13.242 → 0.478 | 16.127 → 0.526 | 96.4% |

PostgreSQL's one-session plan changed from a sequential scan plus sort to an
index scan: **2,043 → 4 shared buffer hits**, with one returned row. SQLite changed
from `SCAN` plus a temporary sorting B-tree to `SEARCH ... USING INDEX`, without
that sort for a single session. Query-plan regression tests exercise the actual
repository SQL on both engines, rather than imposing flaky latency thresholds.

The initial PostgreSQL pair also improved timeline p50 55.024 → 31.882 ms and
feed p50 44.153 → 29.393 ms. Python allocation peaks remained comparable; no RSS
or memory-saving claim is made. Permission create-and-resolve p50 in the serial
PostgreSQL repeat was 1.605 → 1.522 ms, with p95 1.912 → 1.730 ms. SQLite write
timings varied in both directions between runs (initial p50 4.546 → 5.952 ms;
repeat 6.888 → 4.535 ms), so neither a write-speed gain nor a precise overhead
bound is claimed.

With no resolved history and just 54 pending requests, PostgreSQL timeline p50
was 26.019 → 28.732 ms and feed p50 24.203 → 25.755 ms. There is no small-table
benefit in this control; the improvement addresses growth in retained history.
Warm local timings are not production latency guarantees, and database index
storage was not measured.

Reproduce from the repository root:

```bash
backend/.venv/bin/python scripts/benchmark_permission_lookups.py
backend/.venv/bin/python scripts/benchmark_permission_lookups.py --history 0
# Use a local PostgreSQL Unix socket; a generated database is created and dropped.
backend/.venv/bin/python scripts/benchmark_permission_lookups.py \
  --postgres-socket /path/to/local/socket --postgres-port 55437
```

For baseline comparison, archive commit `b52ad4b`'s `backend/` to a temporary
directory and add `--backend-root /path/to/archive/backend`. No checkout change
is needed. Raw samples, plans, hashes, counts and allocation peaks are in
[`benchmarks/permission-lookup-investigation/`](benchmarks/permission-lookup-investigation/).

## Verification

- Before edits, the relevant permission/conversation/interaction/evaluator
  suites passed **79 tests**. Scouts also passed 138 runner Git tests plus 14
  subtests, and 36 backend plugin/credential tests. No pre-existing test failure
  was observed in these runs.
- Before adding the index, the new cases passed nine behavioral tests and failed
  all three query-plan checks with full scans. Afterward, **92 targeted SQLite
  tests passed**, including all 13 new cases.
- **62 PostgreSQL tests passed**, covering indexed query plans, pending scopes,
  optional tool filtering, ordering, resolution, conversation attention,
  interaction cancellation, and existing permission boundaries. Pytest reported
  an open test-database connection during teardown after the message-edit waiter
  test; the entire disposable cluster is removed after verification.
- The populated SQLite migration test applies, rolls back and reapplies 0041,
  checking every persisted permission column and the index's presence/columns.
- Another **132 SQLite tests passed** for the harness API, lightweight timeline,
  fork/edit API, Claude permission bridge and MCP chat tools: **224 SQLite tests
  total**, plus the 62 PostgreSQL tests above.
- Existing Django deprecation and missing-staticfiles warnings remain. The API
  suites also emit coroutine/background-task cleanup warnings; the archived
  baseline API/fork suites passed 65 tests and reproduced those warnings.
- Django system checks, migration drift checks, Ruff lint/format on changed
  Python files and `git diff --check` passed. Final diff review confirms only
  the selected index, its migration, tests and measurement artifacts are added.
  No frontend code changed, so UI/device checks are not applicable.
