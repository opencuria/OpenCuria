# Plugin runtime snapshot query batching

## Investigation and selection

Started from `origin/dev` at `c5b854a689e309ea09fd11d30c45b54778766241`
after reading both AGENTS files, checking local/remote branches, attempting
an include-untracked stash (the checkout was clean), fetching and resetting
`dev`. The existing performance branches were already merged. Recent
catalogue, history, streaming and Agent-S projection improvements were
examined to avoid repeating them.

The investigation covered backend, runner, webapp, mobile and shared packages.
Candidates were ranked by impact, frequency, confidence, implementation risk
and architectural cost:

| Rank | Candidate | Evidence / expected impact | Risk and scope |
| --- | --- | --- | --- |
| 1 | Backend per-run plugin runtime snapshot | `build_workspace_plugin_snapshot` performs three component queries per effective plugin. At 15 plugins, an exploratory real-SQL probe used 48 queries, 32.763 ms p50 and 37.587 ms p95. Every harness run prepares this snapshot before provider/MCP startup. | High confidence; reuse existing bulk repository methods; local change with strong correctness coverage. |
| 2 | Runner archive conversion blocks asyncio | `FileManager.upload_file` calls synchronous `convert_archive_to_tar`. A synthetic 2,000-file source archive (278,435 compressed bytes, 66,570,240 tar bytes) caused 361.766 ms median event-loop delay over ten conversion-only samples. | High confidence; thread offloading could improve other requests' responsiveness. Upload frequency is lower; cancellation and concurrent memory pressure need care. |
| 3 | Backend workspace plugin readiness | `PluginService.list_workspace_plugins` calls `_workspace_required_gaps` per plugin. A 15-plugin ordinary-credential probe used 19 queries, 14.957 ms p50 and 17.563 ms p95; OAuth adds requirement-specific reads. | High confidence; useful during workspace configuration, less frequent than run admission; preserve credential ownership and OAuth policy. |
| 4 | Runner Git snapshot round trips | `_git_repo_snapshot_no_log` awaits status, refs, remotes, default remote and merge-state guest commands sequentially. | Code evidence only; transport latency could dominate. Batching must preserve errors, containment and bounded SSH use. |
| 5 | Mobile streaming snapshot allocation | `prepareEventMessages` clones the changed message and all its parts for each delta; publication also copies sessions and todos. | Needs device CPU/allocation profiling; immutable snapshots and unchanged row identity are essential. |
| 6 | Shared descendant traversal | `collectDescendantSessionIds` repeatedly scans sessions until no new descendants appear; mobile calls it during snapshot publication. | Code evidence only; actual depth is bounded and typical trees may be small. Preserve ordering and cycle handling. |
| 7 | Web growing Markdown parsing | `HarnessMarkdown.vue` reparses/sanitizes/traverses the complete rendered text. Existing 50 ms stream coalescing already limits frequency. | Browser profiling needed; incremental parsing risks reference-link and media semantics. |

The top three were fixed before one uniform Python `secrets.choice` draw
selected **backend: per-run plugin runtime snapshot N+1**. No redraw occurred.
The exploratory SQL probes used 40 warm samples in a disposable file-backed
SQLite database with development query logging. Final measurements below use
`DEBUG=False` and include the complete harness preparation path.

## Implementation and boundaries

`apps/plugins/runtime.py::build_workspace_plugin_snapshot` loads skills, MCP
servers and credential requirements once each through the existing
`list_for_plugins` repository methods. It groups rows only for effective
plugin IDs and consumes each group while building the existing snapshot
dataclasses. Consuming groups releases ORM rows as projection proceeds.

For a nonempty effective set, snapshot queries change from `3 + 3N` to **6**.
Complete `HarnessService._prepare_mcp_snapshot_for_run` preparation, including
workspace reads and both credential-resolution passes, changes from `8 + 3N`
to **11** for the benchmark's attached ordinary credential. Empty visible
sets still use one snapshot query; visible but inactive sets use three.

Selection, per-plugin ordering, desktop resources, OAuth service binding and
credential policy remain unchanged. All queries stay inside repositories.
There is no cache, new dependency, asynchronous scheduling, retained state,
adapter change or runner/frontend business logic.

## Measurement method

`scripts/benchmark_plugin_runtime.py` uses real Django services, repositories,
SQLite queries and credential decryption. Each process migrates a disposable
file-backed database and creates deterministic mixed global/org effective
plugins, three 6,000-byte skills, one MCP definition and one required ordinary
credential per plugin. A shared synthetic encrypted token is attached to the
workspace. No live credential, database, provider, runner or network is used.

The script measures both the isolated snapshot and the complete synchronous
harness MCP preparation, with five warmups and 51 timed samples per size.
Snapshot serialization/hashing, SQL capture and Python allocation tracing
run outside timed regions. Identical deterministic snapshot hashes verify
output parity. Allocation peaks are Python allocations, not process RSS.
These local warm timings measure preparation overhead, not production
database latency or complete agent time to first token.

Reproduce from the repository root:

```bash
backend/.venv/bin/python scripts/benchmark_plugin_runtime.py
```

Use `--backend-root /path/to/archived/backend` to measure the same workload
against the baseline source in the same Python environment. The archive must
include the complete tracked `backend/`, including migrations.

## Results

Final runs were sequential, with no tests running concurrently, in the order
**baseline, optimized, optimized, baseline**. Baseline runs used the archived
starting commit and the same current benchmark script/Python environment.
The table pools 102 latency samples per variant; p95 uses the nearest-rank
percentile. Raw runs, selection metadata, environment versions and output
hashes are in [`benchmarks/plugin-runtime.json`](benchmarks/plugin-runtime.json).

Complete harness MCP preparation:

| Effective plugins | Queries before → after | p50 ms before → after | p95 ms before → after |
| ---: | ---: | ---: | ---: |
| 0 | 4 → 4 | 3.894 → 3.593 | 5.262 → 3.784 |
| 1 | 11 → 11 | 8.663 → 9.000 | 9.060 → 10.381 |
| 5 | 23 → 11 | 16.139 → 10.315 | 17.411 → 11.782 |
| 15 | 53 → 11 | 34.830 → 12.479 | 38.499 → 15.956 |
| 50 | 158 → 11 | 100.268 → 20.851 | 110.506 → 23.682 |

At 15 plugins, complete preparation p50 falls **64.2%**; at 50 it falls
**79.2%**. The isolated snapshot at 15 plugins changes from **48 to 6 queries**,
**28.370 to 6.217 ms p50** and **32.654 to 9.063 ms p95**. At 50 plugins,
snapshot queries change from **153 to 6**, p50 from **92.435 to 12.653 ms**
and p95 from **101.704 to 14.624 ms**. Every snapshot hash matches across all
four runs and both measurement scopes.

Small-workload and memory tradeoffs are retained in the report. Singleton
complete preparation p50 increases by **0.337 ms**, with unchanged query count;
the implementation keeps one bulk-loading path. Median traced preparation
peak allocation changes from **469,433.5 to 489,070 bytes** at 15 plugins and
from **1,298,675 to 1,413,251 bytes** at 50 (about **112 KiB** additional
temporary Python allocations at 50). Grouping is local to one call and is
released as snapshots are built and the call returns; no state persists
between runs. These are allocation peaks rather than retained-memory or RSS
measurements.

## Verification

Before implementation, the plugin suite passed (`83 passed`); the existing
MCP hardening, provider-turn integration and adapter-boundary checks passed
(`61 passed, 22 skipped`). Those skips require the unavailable pinned Codex
binary. Five new selection/freshness/security characterization cases also
passed against the original implementation. The new 15-plugin query guards
then failed as intended with **48 versus 6** snapshot queries and **53 versus
11** complete preparation queries, proving they detect the original issue.

After implementation, the relevant broader checks passed:

```bash
cd backend
.venv/bin/pytest -q -rs apps/plugins/tests apps/credentials/tests \
  apps/harness/tests/test_mcp_harness.py \
  apps/harness/tests/test_mcp_hardening.py \
  apps/harness/tests/test_provider_turn_runtime_e2e.py \
  apps/runners/tests/test_workspace_credential_resolution.py \
  apps/runners/tests/test_service_reads.py common/tests/test_adapter_boundaries.py
```

Result: **241 passed, 2 skipped**. The two skips require PostgreSQL nullable
join locking and deferred-trigger behavior; this environment used SQLite.
Existing deprecation/static-directory warnings remain. Coverage includes
activation/permission checks, cross-tenant rejection, OAuth requirements,
MCP setup failure and cancellation, real local HTTP provider flows, empty
components, fresh reads after edits/revocation and bounded query counts at
1, 5, 15 and 50 effective plugins. No asynchronous execution or event-order
code changed.

Ruff lint and formatting checks passed for the production module, regression
tests and benchmark script. `git diff --check` passed. Final review kept the
production change inside one runtime service function using existing
repository methods, with no unrelated implementation changes.
