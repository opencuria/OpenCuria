# Plugin catalogue query batching

## Investigation and selection

Started from `origin/dev` at `62de54ada5f22bbc0657bf9863964e2901ae818f` after
reading both AGENTS files, checking all local/remote branches, attempting an
include-untracked stash (the checkout was clean), fetching, and resetting
`dev`. Existing performance branches were already merged; recent streaming,
conversation-feed, and Git metadata optimizations were excluded.

The investigation covered backend, runner, webapp, mobile, and shared packages.
Candidates were ranked by measured or expected impact, path frequency,
confidence, complexity/risk, and architectural cost:

| Rank | Candidate | Evidence and expected impact | Risk / scope |
| --- | --- | --- | --- |
| 1 | Backend plugin catalogue N+1 | One plugin query plus four queries per definition. An authenticated 50-plugin REST request used 205 queries. Used by plugin settings and workspace plugin selection. | Local repository/service batching, strong query evidence, low correctness risk. |
| 2 | Runner untracked Git diffs | Complete `working_diff` with 50 untracked files used 55 guest commands and 122.416 ms local p50. Each file adds a sequential guest execution. | Common Git read with transport-sensitive latency; bounded concurrency/batching needs cancellation, output/resource and ordering care. |
| 3 | Backend chat fork prefix | Copying 50 messages with three parts each used 978 SQL statements and 201.286 ms p50 inside a rollback-only transaction. | Valuable on long chats but less frequent; bulk writes must preserve positions, display projections, ID remapping and atomicity. |
| 4 | Mobile transcript publication | Every streaming delta clones the changed message's parts and republishes cloned session/todo snapshots. | Needs device CPU/allocation evidence; immutable snapshots and React subscriptions must survive. |
| 5 | Shared Agent-S timeline derivation | `buildAgentStepViews` calls `agentStepStatus` for each agent part; that helper repeatedly scans the complete parts array. Both clients consume it. | Potential quadratic work in long computer-use turns; requires realistic streaming profiles and marker semantics checks. |
| 6 | Web streamed Markdown | Growing documents are reparsed, sanitized and traversed. Existing 50 ms coalescing already limits render frequency. | Browser profiling needed; incremental parsing carries syntax/media correctness risk. |

The first three were shortlisted before a single uniform Python
`secrets.choice` draw selected **plugin_catalogue** at
`2026-10-25T08:43:01.234126+00:00`. No redraw occurred. The exploratory
measurements used disposable databases/repositories; only the selected path
was optimized. Selection metadata and raw measurements are in
[`benchmarks/plugin-catalogue.json`](benchmarks/plugin-catalogue.json).

## Implementation and boundaries

`PluginService._serialize_plugins` bulk-loads skills, MCP definitions and
credential requirements with the existing repository methods, then obtains
org activation IDs once. Components are grouped by plugin ID while preserving
repository ordering. The pure `_serialize_plugin` formats the fully loaded
records without further reads. List, detail, create, update and activation
responses all use this implementation.

`OrgPluginActivationRepository.enabled_plugin_ids` accepts an optional plugin
ID restriction so singleton/detail responses also read only their own state.
The superseded singleton activation lookup was removed. An empty visible
catalogue skips component and activation reads.

The service uses **five queries** for any nonempty catalogue instead of
`1 + 4N`; the complete authenticated REST path uses **nine** instead of
`5 + 4N`. Existing global/org visibility, disabled/unpublished definitions,
org-scoped activation, nested field values and ordering remain intact.
Adapters and runtime execution are unchanged. All ORM access stays in
repositories; temporary grouping state belongs to one call. There is no
cache, retained state, new dependency, parallel execution or protocol change.

## Measurement method

`scripts/benchmark_plugin_catalogue.py` uses the authenticated Django HTTP test
client with real middleware, services, repositories, SQLite queries, schema
mapping and JSON serialization. It creates a disposable file-backed database
before loading settings and runs migrations. Fixtures have 0, 1, 10 or 50
visible plugins, each with two skills, two MCP servers and two credential
requirements. They mix global/org ownership, activation states, disabled and
unpublished definitions; a foreign plugin must stay hidden. IDs and exposed
timestamps are deterministic so complete response hashes can be compared.

Warm requests are timed separately from SQL capture and `tracemalloc` probes.
SQL timings have millisecond resolution and are not precise enough for these
local queries; query counts and full request latency are the primary metrics.
Python allocation peaks are not process RSS. These are local integration
measurements, without production PostgreSQL, network, runners or provider calls.

Python 3.12.3, Django 5.2.17, SQLite 3.45.1, Linux. Two sequential batches ran
51 requests per version/endpoint/case after five warmups, with version order
reversed in batch two and no competing test suite. The table combines the 102
samples per version using nearest-rank p95:

| Visible plugins | REST queries before → after | p50 before → after | p95 before → after |
| ---: | ---: | ---: | ---: |
| 0 | 5 → 5 | 7.186 → 6.366 ms | 8.766 → 8.106 ms |
| 1 | 9 → 9 | 9.343 → 9.229 ms | 10.722 → 10.398 ms |
| 10 | 45 → 9 | 31.991 → 14.276 ms | 53.870 → 16.545 ms |
| 50 | 205 → 9 | 132.752 → 34.651 ms | 199.471 → 50.489 ms |

At 50 plugins this eliminates **95.6% of SQL queries** and reduces median
request time by **73.9%**. Peak Python allocations were 2.348–2.351 MB before
and 2.315–2.317 MB after. Within each catalogue size, all complete
catalogue/detail response hashes matched across versions and both batches.

Singleton timing varied between process batches, so an additional paired
check alternated baseline/current requests against the same database,
middleware, REST adapter and models. It loaded the original service/repository
source under private module aliases and selected the adapter's service factory
outside the timing window. After five warmups, 101 request pairs reversed order
on every pair. The 50-plugin catalogue measured 133.525 → 34.965 ms p50 and
202.142 → 54.835 ms p95, again with identical response hashes. Detail reads
retained nine queries; paired medians across catalogue sizes increased by
0.258–0.480 ms. This small fixed batching/grouping overhead is the tradeoff
for sharing one loading/serialization path. There is no detail cost that grows
with catalogue size. Separate-process detail timings are retained in the raw
report instead of treating their noisy tails as a catalogue-size effect.

Reproduce the current implementation from the repository root:

```bash
backend/.venv/bin/python scripts/benchmark_plugin_catalogue.py --samples 51 --warmups 5
```

Pass `--backend-root /path/to/archived/backend` to run the identical script
against the baseline source using the same Python environment. The baseline
archive must contain the complete `backend/`, including migrations.

## Verification

Before implementation, plugin tests plus existing fork/edit tests passed
(`109 passed`); the existing runner Git tests passed (`123 passed`,
`14 subtests passed`). Three new visibility/freshness characterization tests
also passed against the unchanged service before batching.

The regression tests cover constant service query counts at 1, 12 and 50
plugins, the authenticated REST count, empty catalogues, ordering, component
association, foreign-org exclusion/not-found behavior, activation isolation,
and fresh reads after edits. Existing plugin API/runtime tests exercise
create/update/activation responses, permission checks, and error paths.

Final relevant checks:

- `pytest -q apps/plugins/tests apps/credentials/tests
  apps/runners/tests/test_services.py
  apps/runners/tests/test_workspace_credential_resolution.py
  apps/harness/tests/test_mcp_harness.py
  apps/harness/tests/test_mcp_hardening.py`: **296 passed, 2 skipped**.
  The skips require PostgreSQL nullable-join locking/deferred-trigger behavior;
  these workspace checks used SQLite. No pre-existing failures were found.
- Ruff lint with `backend/pyproject.toml` and format checks passed for both
  changed production modules, the regression tests and benchmark script.
- `git diff --check` passed; final review found no unrelated implementation,
  adapter ORM access, retained state or new dependency.
