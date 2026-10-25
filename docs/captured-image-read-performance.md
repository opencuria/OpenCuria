# Captured-image presentation query batching

Started from `origin/dev` at `40c4980912fc1b4a8647a455f36f6d5ac3b00e33`
after reading both AGENTS files, checking local/remote branches, attempting an
include-untracked stash (the checkout was clean), fetching and resetting `dev`.
Existing history, plugin, Agent-S and Git probe optimizations were excluded.

## Investigation and selection

The survey covered backend, runner, webapp, mobile and shared packages. Ranking
considered impact, frequency, confidence, implementation risk and architecture:

| Rank | Candidate | Evidence / importance | Risk |
| --- | --- | --- | --- |
| 1 | Backend captured-image presentation | Authenticated 30-image/150-version listing used 545 SQL statements. Version relations, latest selection and retention were repeatedly loaded. Used by image management and MCP workspace image selection. | Read-only, localized batching; lifecycle states need characterization. |
| 2 | Backend workspace base-image references | Authenticated 30-workspace list used 157 SQL statements, versus 12 for one workspace. The mapper resolves latest references separately for base/pending images. | Shared workspace serialization serves REST and events. |
| 3 | Runner untracked Git diffs | Existing production service with real temporary Git repositories: 1/10/30 files needed 6/15/35 guest-execution calls; local p50 6.898/29.061/81.798 ms, excluding QEMU/SSH latency. | Bounded concurrency needs cancellation, process-lifetime and ordering checks. |
| 4 | Backend heartbeat workspace refresh | Auto-stop re-fetches full workspace relations and credentials for each candidate, including disabled policies. Frequent periodic path. | Safety-critical freshness and lifecycle admission; not measured in isolation. |
| 5 | Mobile streaming snapshots | Each delta clones all parts of the current assistant message and republishes session/todo snapshots. | Needs native CPU/allocation profiling and immutable-snapshot checks. |
| 6 | Shared/web child-session matching | Fallback matching filters children for each subtask; Vue derives the map from all retained transcripts. | Potential quadratic fallback work; lower confidence in typical workload impact. |

One uniform `secrets.choice(top_three)` draw selected
**captured-image-presentation**. No redraw occurred. Exploratory measurements
and the selection record are in
[`benchmarks/captured-image-reads/selection.json`](benchmarks/captured-image-reads/selection.json).
Exploratory timings included other verification work and were used to find
scaling problems, not for the final before/after comparison.

## Change and boundaries

`ImageVersionService.list_captured_images` loads all visible lines' versions
together, groups them in their existing order, and assembles the same views as
detail/rename. One request-local facts object holds latest selections, retention
labels and workspace pins. There is no cache or cross-request state.

`ImageLineRepository` bulk-loads presentation relations, latest selections and
ready version IDs. Captured latest selection uses a correlated subquery over
the existing unique `(captured_image, generation)` index. Definition latest
selection still uses the build's published `current_generation`, which may be
older than another ready generation. Singleton repository operations use the
same selection/label implementations. The retention window helper is shared
with deletion eligibility, preserving the latest-plus-newest-ready rule.

Policies are read once per organization. Empty lists return after the visible
line query. Latest references remain independent of the version row snapshot:
a version published during loading can still be returned as latest, including
when the line was initially empty. Visibility, terminal-status exclusions,
retention labels, pin deduplication, version ordering and response schemas stay
the same. All ORM work stays in repositories; REST and MCP continue to call the
same service. No adapters, runner/client logic, dependencies or schema changed.

For a nonempty captured list the service plus schema mapping now needs six
queries regardless of line/version count. The complete authenticated REST path
needs ten, versus `5 + 18N` for the measured five-version fixture.

## Measurements

Python 3.12.3, Django 5.2.17, SQLite 3.45.1, Linux. Measurements use the full
authenticated Django HTTP test client, including middleware, services,
repositories, schema mapping and JSON serialization, against a disposable
file-backed database. Each image has three ready versions, a failed attempt,
a capturing attempt, and one workspace pinned to v1. Deterministic IDs and
timestamps permit complete response SHA-256 comparisons.

The final comparison alternated old/current request order on every pair in
one process and database. Original service/repository source came from the
starting Git revision under private benchmark module names. Selecting the
service factory happened outside the timer. Each endpoint/size had five warmup
pairs and 51 measured pairs. No test suite ran during this comparison. Query
capture and `tracemalloc` ran separately from latency samples; p95 uses nearest
rank. Allocation peaks are Python allocations, not RSS; SQL execution timings
exclude row fetching and ORM materialization. These are local integration
measurements, not production PostgreSQL/network latency estimates.

| Images / versions | REST queries before → after | p50 before → after | p95 before → after |
| ---: | ---: | ---: | ---: |
| 0 / 0 | 5 → 5 | 5.645 → 5.694 ms | 7.196 → 7.435 ms |
| 1 / 5 | 23 → 10 | 17.727 → 12.740 ms | 19.456 → 13.257 ms |
| 10 / 50 | 185 → 10 | 123.891 → 23.455 ms | 140.790 → 27.374 ms |
| 30 / 150 | 545 → 10 | 358.775 → 46.479 ms | 401.903 → 152.671 ms |

At 30 images this removes **98.2% of SQL statements**, reduces p50 by **87.0%**
and p95 by **62.0%**. Peak Python allocations fell from 1,770,312 to 1,689,458
bytes (4.6%). Detail reads used 23 → 10 statements and measured
18.163 → 13.134 ms p50 in the same 30-image database. Single-image allocation
peaks increased by about 15 KB from the batched selection/relation machinery;
the larger listing's peak decreased. Empty-list allocations were unchanged
within measurement noise (73,278 → 73,528 bytes).

Every complete list/detail response hash matched between versions at every
fixture size. Raw samples, query counts, allocation peaks and hashes are in
[`benchmarks/captured-image-reads/paired.json`](benchmarks/captured-image-reads/paired.json).

The committed benchmark reproduces the workload for an archived baseline and
the current backend in separate processes:

```bash
baseline_dir=$(mktemp -d)
git archive 40c4980 backend | tar -x -C "$baseline_dir"
backend/.venv/bin/python scripts/benchmark_image_versions.py \
  --backend-root "$baseline_dir/backend" --samples 51 --warmups 5 > /tmp/images-before.json
backend/.venv/bin/python scripts/benchmark_image_versions.py \
  --samples 51 --warmups 5 > /tmp/images-after.json
```

## Verification

Before changes, 99 relevant backend tests passed; the runner investigation's
154 existing Git tests and 14 subtests also passed.

New regression coverage exercises constant query counts at 1/10/30 lines and
1/8 versions per line; empty results; list/detail equivalence; ready-but-missing
runner references; failed/in-flight/deleted attempts; inactive lines; older
published definition selections; pending/duplicate/terminal workspace pins;
fresh reads after policy/state changes; publication during row loading; owner
and organization scoping; and REST authentication/permission rejection.

The broader SQLite runner/MCP/adapter-boundary run passed **1,018 tests** with
19 skips. Following final identity-grouping review, the focused
image/generation/query/boundary suite passed **73 tests**, including all **23 new
regression cases**. Those 23 cases also pass on PostgreSQL 16 in a separate
disposable local cluster. Django's system check, targeted Ruff lint/format
checks and `git diff --check` pass.

An expanded PostgreSQL run passed 61 tests and failed 23. The same existing
tests against the untouched starting revision passed 39 and failed the exact
same 23 test IDs (the 22-test difference was the new regression suite before
the final identity case). Failures concern existing nullable-outer-join
`FOR UPDATE` queries in recreation and capture-lock/concurrency assertions;
they are outside this read-only optimization. They were reproduced rather
than fixed in this change. The local PostgreSQL cluster is disposable and
does not use the running application's database.

Commands, counts and the matching PostgreSQL failure IDs are in
[`benchmarks/captured-image-reads/verification.json`](benchmarks/captured-image-reads/verification.json).

Before publishing, the change was rebased onto `origin/dev` at `f4baebd`
(workspace disk-usage reporting). The combined revision passed **129 tests**
covering image/version/lifecycle reads, the incoming disk-usage behavior, REST,
MCP and adapter boundaries. The incoming commit does not alter the measured
captured-image service or repository methods.
